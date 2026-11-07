"""Internal published versions, audit and blocked delivery events atomically.

No gateway/worker/model/network calls. GET/preview never create schema. Only an
explicit, authorised publish action may add these new domain tables.
"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import db, launch_identity
from .auth import session_profile
from .school_calendar import calendar_effect_for_plan, assert_calendar_binding_current
from .scheduling_models import PlanningInput, planning_readiness
from .scheduling_solver import validate_solution
from .timetable import batch_hash, EditableLessonInput

MAX_PUBLICATIONS = 1000
HISTORY_LIMIT = 20
TABLE = 'school_publications'


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _actor(profile, permission):
    try:
        valid = launch_identity.membership_current(profile)
    except Exception:
        valid = False
    role = profile.get('staffRole', profile.get('role')) if isinstance(profile, dict) else None
    if (not valid or role not in {'director', 'planner'} or permission not in profile.get('permissions', [])
            or not isinstance(profile.get('schoolId'), str) or not profile['schoolId']):
        raise HTTPException(403, 'Нет действующего права публикации школьной версии')
    return profile


def _reader(profile: dict = Depends(session_profile)):
    return _actor(profile, 'publication.read')


def _publisher(profile: dict = Depends(session_profile)):
    return _actor(profile, 'publication.publish')


def _exists(connection, name):
    return db.table_exists(connection, name)


def _row(connection, query, args=()):
    cursor = connection.execute(query, args)
    value = cursor.fetchone()
    return dict(zip((column[0] for column in cursor.description), value)) if value is not None else None


def _rows(connection, query, args=()):
    cursor = connection.execute(query, args)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _read_connection():
    path = db.database_path()
    if not path.is_file():
        return None
    return sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=15)


def _active_row(school_id, connection):
    return _row(connection, 'SELECT * FROM school_publications WHERE school_id=? ORDER BY version DESC LIMIT 1', (school_id,)) if _exists(connection, TABLE) else None


def read_active_snapshot(school_id: str, connection=None):
    """Trusted own-view callers consume ONLY this immutable published snapshot.

    None means publication_required, never permission to read the draft.
    An explicitly supplied domain transaction is reused without side effects.
    """
    owned = connection is None
    connection = _read_connection() if owned else connection
    try:
        row = _active_row(school_id, connection)
        if row is None:
            return None
        value = json.loads(row['snapshot_json'])
        if (value.get('schoolId') != school_id or value.get('version') != row['version']
                or value.get('draftRevision') != row['draft_revision'] or not isinstance(value.get('lessons'), list)):
            raise HTTPException(409, 'Опубликованная версия повреждена; черновик не используется вместо неё')
        return deepcopy(value)
    finally:
        if owned and connection is not None:
            connection.close()



def teacher_snapshot_lessons(snapshot, teacher_refs):
    """One own card per physical event; keep class projections in the snapshot.

    Only already verified teacher refs may call this. No other teachers/source
    cells/group membership/assumption ledger is included in the own projection.
    """
    refs = set(teacher_refs)
    grouped = {}
    for lesson in snapshot['lessons']:
        if not refs.intersection(lesson.get('teachers', [])):
            continue
        key = lesson.get('planningEventId') or lesson['id']
        content = {k: deepcopy(lesson[k]) for k in ('day','slot','start','end','subject','rooms','teachers')}
        if key in grouped:
            if _json(grouped[key]['content']) != _json(content):
                raise HTTPException(409, 'Проекции общего опубликованного занятия расходятся.')
            grouped[key]['classNames'].add(lesson['className'])
        else:
            grouped[key] = {'content':content,'lesson':lesson,'classNames':{lesson['className']}}
    result = []
    for item in grouped.values():
        row = {k: deepcopy(item['lesson'][k]) for k in ('id','day','slot','start','end','subject','rooms')}
        classes = sorted(item['classNames'])
        row.update(className=', '.join(classes))
        if len(classes) > 1:
            row['classNames'] = classes
        result.append(row)
    return result


def _schema(connection):
    if getattr(connection, 'requires_provisioned_schema', False):
        if not all(connection.table_exists(name) for name in (
                'school_publications', 'school_publication_audit',
                'school_publication_outbox', 'school_publication_receipts')):
            raise HTTPException(503, 'Схема публикаций не подготовлена; нужен отдельный перенос данных')
        return
    # Do not use executescript: its implicit COMMIT would break DDL rollback.
    statements = [
        '''CREATE TABLE IF NOT EXISTS school_publications (
        id TEXT PRIMARY KEY, school_id TEXT NOT NULL, version INTEGER NOT NULL,
        request_id TEXT NOT NULL UNIQUE, draft_revision INTEGER NOT NULL, run_id TEXT NOT NULL,
        snapshot_json TEXT NOT NULL, evidence_json TEXT NOT NULL, created_at TEXT NOT NULL,
        actor_id TEXT NOT NULL, UNIQUE(school_id,version))''',
        '''CREATE TABLE IF NOT EXISTS school_publication_audit (
        id TEXT PRIMARY KEY, school_id TEXT NOT NULL, publication_id TEXT NOT NULL UNIQUE,
        actor_id TEXT NOT NULL, actor_role TEXT NOT NULL, action TEXT NOT NULL, created_at TEXT NOT NULL,
        evidence_json TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS school_publication_outbox (
        id TEXT PRIMARY KEY, school_id TEXT NOT NULL, publication_id TEXT NOT NULL UNIQUE,
        event_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS school_publication_receipts (
        request_id TEXT PRIMARY KEY, school_id TEXT NOT NULL, actor_id TEXT NOT NULL,
        actor_role TEXT NOT NULL, membership_id TEXT NOT NULL, request_hash TEXT NOT NULL,
        response_json TEXT NOT NULL)''',
    ]
    for statement in statements:
        connection.execute(statement)


def _canonical_grid(lessons):
    if not isinstance(lessons, list) or not 1 <= len(lessons) <= 2000:
        raise ValueError('Invalid grid')
    result, identities = [], set()
    for row in lessons:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str) or row['id'] in identities:
            raise ValueError('Invalid lesson ID')
        identities.add(row['id'])
        fields = EditableLessonInput.model_validate({key: row[key] for key in EditableLessonInput.model_fields}).model_dump()
        item = {'id': row['id'], **fields}
        event = row.get('_planningEventId', row.get('planningEventId'))
        groups = row.get('_planningGroups', row.get('planningGroups'))
        if event is not None or groups is not None:
            if not isinstance(event, str) or not isinstance(groups, list):
                raise ValueError('Invalid planning provenance')
            item.update(planningEventId=event, planningGroups=deepcopy(groups))
        result.append(item)
    return sorted(result, key=lambda row: row['id'])


def _diff(before, after):
    first, second = {row['id']: row for row in before}, {row['id']: row for row in after}
    return [{'lessonId': identity, 'before': first.get(identity), 'after': second.get(identity)}
            for identity in sorted(first.keys() | second.keys()) if first.get(identity) != second.get(identity)]


def _block(code, message):
    return {'code': code, 'message': message}


def _approval_actor(base, run, school_id):
    actor = base.get('actorBinding')
    if not isinstance(actor, dict):
        raise HTTPException(409, 'Старый расчёт не содержит проверенной привязки утвердившего сотрудника')
    role = actor.get('staffRole', actor.get('role'))
    # require_roles adapts the trusted planner to the legacy admin domain role.
    expected_role = 'admin' if actor.get('staffRole') == 'planner' else role
    domain_role = actor.get('domainRole', actor.get('role'))
    try:
        current = launch_identity.membership_current(actor)
    except Exception:
        current = False
    if (role not in {'director', 'planner'} or expected_role != domain_role
            or (actor.get('userId'), domain_role, actor.get('schoolId')) != (run['owner_id'], run['owner_role'], school_id)
            or 'schedule.manage' not in actor.get('permissions', [])
            or not current):
        raise HTTPException(409, 'Доступ утвердившего сотрудника изменился или не подтверждён; нужен новый расчёт')
    return actor


def _delivery_readiness():
    return {'ready': False, 'liveEligible': False, 'status': 'blocked_not_configured', 'blockers': [
        _block('recipient_required', 'Получатели ещё не определены и не подтверждены'),
        _block('terms_required', 'Условия использования канала ещё не приняты'),
        _block('channel_not_configured', 'Рабочая доставка MAX не включена')]}


class StrictInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class PreviewInput(StrictInput):
    runId: str = Field(min_length=1, max_length=80, pattern=r'^run_[A-Za-z0-9_-]+$')
    expectedActiveVersion: int = Field(ge=0, le=2**31 - 1)
    expectedDraftRevision: int = Field(ge=0, le=2**31 - 1)


class PublishInput(PreviewInput):
    previewHash: str = Field(pattern=r'^[a-f0-9]{64}$')
    requestId: str = Field(min_length=16, max_length=80, pattern=r'^[A-Za-z0-9_-]+$')


def _prepare(connection, body: PreviewInput, profile):
    """Rebuild all evidence from domain rows on one consistent read snapshot."""
    from .planning import PlanningService
    active = read_active_snapshot(profile['schoolId'], connection=connection)
    active_version = active['version'] if active else 0
    blockers = []
    if active_version != body.expectedActiveVersion:
        blockers.append(_block('active_version_changed', 'Активная версия уже изменилась; повторите предпросмотр'))
    required = ('timetable_draft', 'planning_config', 'planning_runs', 'timetable_command_receipts')
    if not all(_exists(connection, table) for table in required):
        return {'ready': False, 'blockers': blockers + [_block('approved_run_required', 'Нужен подтверждённый расчёт и его доменная квитанция')], 'activeVersion': active_version, 'previewHash': None, 'diff': []}
    draft_row = _row(connection, 'SELECT payload FROM timetable_draft WHERE id=1')
    config = _row(connection, 'SELECT * FROM planning_config WHERE id=1')
    run = _row(connection, 'SELECT * FROM planning_runs WHERE id=?', (body.runId,))
    if draft_row is None or config is None or run is None:
        return {'ready': False, 'blockers': blockers + [_block('approved_run_required', 'Не найдены текущий черновик, нормативный вход или расчёт')], 'activeVersion': active_version, 'previewHash': None, 'diff': []}
    try:
        draft, normative = json.loads(draft_row['payload']), json.loads(config['input_json'])
        base = json.loads(run['base_snapshot_json'])
        binding = base.get('calendarBinding')
        if not isinstance(binding, dict) or binding.get('schoolId') != profile['schoolId']:
            raise HTTPException(409, 'Расчёт не имеет проверенной школьной привязки календаря')
        approver = _approval_actor(base, run, profile['schoolId'])
        if run['status'] != 'approved' or run['cancel_requested'] or not run['applied_task_id'] or run['applied_task_id'] != run['proposal_task_id']:
            raise HTTPException(409, 'Расчёт не утверждён и не применён через проверенное согласование')
        if config['revision'] != run['config_revision']:
            raise HTTPException(409, 'Нормативные условия изменились после расчёта')
        if draft['revision'] != body.expectedDraftRevision:
            raise HTTPException(409, 'Черновик изменился после предпросмотра')
        normative = PlanningInput.model_validate(normative).model_dump()
        normative['periodStart'] = json.loads(run['input_json'])['periodStart']
        if not normative['confirmed']:
            raise HTTPException(409, 'Нормативный план не подтверждён')
        domain = _row(connection, 'SELECT payload FROM demo_state WHERE id=1') if _exists(connection, 'demo_state') else None
        state = json.loads(domain['payload']) if domain else {}
        effect = PlanningService._calendar_effect(connection,profile,normative,exclude_run_id=run['id'],require_configured=True)
        if effect is None:
            raise HTTPException(409,'Нет проверенного школьного календаря')
        effect['effectiveInput']['baseline']=json.loads(run['input_json'])['baseline']
        PlanningService._assert_run_calendar(connection,run)
        from .school_plan_imports import source_readiness
        if not effect['ready'] or not planning_readiness(effect['effectiveInput'])['ready'] or source_readiness(connection,normative):
            blockers.extend(effect['blockers'])
            raise HTTPException(409, 'Календарь не готов к публикации выбранной недели')
        assert_calendar_binding_current(binding, connection=connection)
        if binding != effect['calendarBinding']:
            raise HTTPException(409, 'Привязка календаря не соответствует текущим условиям')
        if base.get('requestBinding') is not None:
            from .request_planning import reapply_effect
            effect['effectiveInput'] = reapply_effect(connection, base['requestBinding'], effect['effectiveInput'],
                run_id=run['id'], allow_applied=True, applied_draft_revision=run['draft_revision']+1)
        snapshot, result = json.loads(run['input_json']), json.loads(run['result_json'])
        if (_hash(snapshot) != run['input_hash'] or _hash(base) != run['base_snapshot_hash']
                or base['revision'] != run['draft_revision'] or _hash(snapshot) != _hash(effect['effectiveInput'])
                or result.get('status') not in {'optimal', 'feasible'} or result.get('validation', {}).get('valid') is not True):
            raise HTTPException(409, 'Исходный снимок или проверенный результат расчёта изменён')
        validation = validate_solution(snapshot, result['assignments'])
        if validation['valid'] is not True:
            raise HTTPException(409, 'Кандидат не прошёл независимую проверку перед публикацией')
        materialized = PlanningService.materialize(run)
        old_diff, old_summary = PlanningService._draft_diff(base, materialized)
        if old_diff != json.loads(run['draft_diff_json']) or old_summary != json.loads(run['change_summary_json']):
            raise HTTPException(409, 'Полный список изменений расчёта изменён')
        run_hash = PlanningService._run_hash(run)
        if run_hash != run['proposal_hash']:
            raise HTTPException(409, 'Расчёт отличается от утверждённого предложения')
        receipt = _row(connection, 'SELECT * FROM timetable_command_receipts WHERE command_key=?', (run['applied_task_id'],))
        if (receipt is None or receipt['actor_id'] != run['owner_id'] or receipt['actor_role'] != run['owner_role']
                or receipt['command_hash'] != batch_hash(materialized, run['draft_revision'], 'Автоматический расчёт')):
            raise HTTPException(409, 'Нет точной доменной квитанции применения утверждённого расчёта')
        applied = json.loads(receipt['result'])
        if applied['revision'] != draft['revision'] or draft['revision'] != run['draft_revision'] + 1:
            raise HTTPException(409, 'После согласования черновик изменился; нужен новый проверенный расчёт')
        grid = _canonical_grid(materialized)
        if grid != _canonical_grid(draft['lessons']) or grid != _canonical_grid(applied['lessons']):
            raise HTTPException(409, 'Текущая сетка не совпадает со всем утверждённым кандидатом')
        before = _canonical_grid(active['lessons']) if active else []
        diff = _diff(before, grid)
        if active and active['draftRevision'] == draft['revision'] and active.get('runId') == run['id']:
            raise HTTPException(409, 'Эта подтверждённая версия уже активна')
        try:
            resolved = launch_identity.resolve_membership(profile['schoolId'], approver['membershipId'])
            display_name = resolved.get('name') if isinstance(resolved, dict) else None
        except Exception:
            display_name = None
        display_name = display_name.strip() if isinstance(display_name, str) and display_name.strip() else 'Уполномоченный сотрудник'
        evidence = {'runId': run['id'], 'runHash': run_hash, 'appliedTaskId': run['applied_task_id'],
                    'approvedBy': {'userId': run['owner_id'], 'role': approver.get('staffRole', approver['role']),
                                   'displayName': display_name,
                                   'domainRole': run['owner_role'], 'membershipId': approver['membershipId'],
                                   'membershipRevision': approver['membershipRevision']},
                    'configRevision': config['revision'], 'normativeInputHash': _hash(normative),
                    'calendarBinding': binding, 'effectiveInputHash': run['input_hash'], 'materializedHash': _hash(grid),
                    'validator': 'server.scheduling_solver.validate_solution',
                    'validatorSourceHash': hashlib.sha256(Path(__file__).with_name('scheduling_solver.py').read_bytes()).hexdigest(),
                    'independentValidation': validation, 'draftSourceName': draft.get('sourceName', ''),
                    'periodStart': snapshot['periodStart'], 'draftRevision': draft['revision']}
        if base.get('requestBinding') is not None:
            evidence['requestBinding'] = deepcopy(base['requestBinding'])
        content = {'schoolId': profile['schoolId'], 'actorId': profile['userId'], 'actorRole': profile.get('staffRole', profile.get('role')),
                   'activeVersion': active_version, 'evidence': evidence, 'lessons': grid, 'diff': diff}
        return {'ready': not blockers, 'blockers': blockers, 'activeVersion': active_version, 'previewHash': _hash(content) if not blockers else None,
                'diff': diff, 'lessons': grid, 'evidence': evidence, 'deliveryReadiness': _delivery_readiness()}
    except (HTTPException, ValidationError, KeyError, TypeError, ValueError) as error:
        if isinstance(error, HTTPException) and error.status_code == 503:
            raise
        message = error.detail if isinstance(error, HTTPException) and isinstance(error.detail, str) else 'Не удалось подтвердить целостность источников публикации'
        return {'ready': False, 'blockers': blockers + [_block('publication_evidence_required', message)],
                'activeVersion': active_version, 'previewHash': None, 'diff': []}


def preview_publication(body: PreviewInput, profile):
    _actor(profile, 'publication.read')
    connection = _read_connection()
    try:
        if connection is not None:
            connection.execute('BEGIN')
        return _prepare(connection, body, profile)
    finally:
        if connection is not None:
            connection.close()


def _insert_outbox(connection, publication_id, school_id, snapshot, stamp):
    event_id = 'pub_event_' + uuid4().hex
    payload = {'eventId': event_id, 'type': 'school_schedule_published', 'schoolId': school_id, 'publicationId': publication_id,
               'version': snapshot['version'], 'draftRevision': snapshot['draftRevision'], 'periodStart': snapshot['periodStart'],
               'recipientIds': [], **_delivery_readiness()}
    connection.execute('INSERT INTO school_publication_outbox VALUES(?,?,?,?,?,?,?)',
        (event_id, school_id, publication_id, 'school_schedule_published:' + publication_id, 'blocked_not_configured', _json(payload), stamp))
    return payload


def publish_publication(body: PublishInput, profile):
    _actor(profile, 'publication.publish')
    path = db.database_path()
    if not path.is_file():
        raise HTTPException(409, 'Нет утверждённого школьного расчёта для публикации')
    request_hash = _hash(body.model_dump(exclude={'requestId'}))
    actor_role = profile.get('staffRole', profile.get('role'))
    with closing(sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=15, isolation_level=None)) as connection:
        connection.execute('PRAGMA busy_timeout=15000')
        try:
            connection.execute('BEGIN IMMEDIATE')
            _actor(profile, 'publication.publish')
            # Current membership is always required, even for a retained receipt.
            if _exists(connection, 'school_publication_receipts'):
                previous = _row(connection, 'SELECT * FROM school_publication_receipts WHERE request_id=?', (body.requestId,))
                if previous:
                    if (previous['school_id'], previous['actor_id'], previous['actor_role'], previous['membership_id'], previous['request_hash']) != (profile['schoolId'], profile['userId'], actor_role, profile['membershipId'], request_hash):
                        raise HTTPException(409, 'Идентификатор публикации уже использован другим действием или сотрудником')
                    _actor(profile, 'publication.publish'); connection.commit()
                    return json.loads(previous['response_json'])
            preview = _prepare(connection, body, profile)
            if not preview['ready'] or preview['previewHash'] != body.previewHash:
                raise HTTPException(409, 'Публикация не прошла актуальную проверку. Повторите предпросмотр и разберите блокеры.')
            if _exists(connection, TABLE) and connection.execute('SELECT COUNT(*) FROM school_publications WHERE school_id=?', (profile['schoolId'],)).fetchone()[0] >= MAX_PUBLICATIONS:
                raise HTTPException(409, 'Достигнут предел опубликованных версий; требуется разбор оператором')
            _schema(connection)
            stamp, publication_id = datetime.now(timezone.utc).isoformat(), 'pub_' + uuid4().hex
            evidence = preview['evidence']; binding = evidence['calendarBinding']
            snapshot = {'publicationId': publication_id, 'schoolId': profile['schoolId'], 'version': preview['activeVersion'] + 1,
                        'draftRevision': body.expectedDraftRevision, 'runId': body.runId, 'periodStart': evidence['periodStart'],
                        'calendarRevision': binding['calendarRevision'], 'calendarHash': binding['inputHash'],
                        'lessons': preview['lessons'], 'publishedAt': stamp}
            connection.execute('INSERT INTO school_publications VALUES(?,?,?,?,?,?,?,?,?,?)',
                (publication_id, profile['schoolId'], snapshot['version'], body.requestId, body.expectedDraftRevision, body.runId,
                 _json(snapshot), _json(evidence), stamp, profile['userId']))
            connection.execute('INSERT INTO school_publication_audit VALUES(?,?,?,?,?,?,?,?)',
                (uuid4().hex, profile['schoolId'], publication_id, profile['userId'], actor_role, 'published_internal', stamp, _json(evidence)))
            outbox = _insert_outbox(connection, publication_id, profile['schoolId'], snapshot, stamp)
            response = {'publication': snapshot, 'outbox': outbox, 'deliveryReadiness': _delivery_readiness(),
                        'notice': 'Внутренняя школьная версия опубликована. Внешние сообщения не отправлялись; доставка заблокирована.'}
            connection.execute('INSERT INTO school_publication_receipts VALUES(?,?,?,?,?,?,?)',
                (body.requestId, profile['schoolId'], profile['userId'], actor_role, profile['membershipId'], request_hash, _json(response)))
            _actor(profile, 'publication.publish')
            current_run = _row(connection, 'SELECT * FROM planning_runs WHERE id=?', (body.runId,))
            _approval_actor(json.loads(current_run['base_snapshot_json']), current_run, profile['schoolId'])
            connection.commit(); return response
        except BaseException:
            connection.rollback(); raise


def publication_status(profile, *, storage=None):
    _actor(profile, 'publication.read')
    connection = storage.read_connection() if storage is not None else _read_connection()
    try:
        if connection is not None:
            connection.execute('BEGIN')
        active = read_active_snapshot(profile['schoolId'], connection=connection) if connection is not None else None
        history = _rows(connection, 'SELECT id,version,draft_revision,run_id,created_at,actor_id FROM school_publications WHERE school_id=? ORDER BY version DESC LIMIT ?', (profile['schoolId'], HISTORY_LIMIT)) if _exists(connection, TABLE) else []
        count = connection.execute('SELECT COUNT(*) FROM school_publications WHERE school_id=?', (profile['schoolId'],)).fetchone()[0] if _exists(connection, TABLE) else 0
        draft = _row(connection, 'SELECT payload FROM timetable_draft WHERE id=1') if _exists(connection, 'timetable_draft') else None
        draft_revision = json.loads(draft['payload'])['revision'] if draft else 0
        candidates = []
        if _exists(connection, 'planning_runs'):
            for row in _rows(connection, "SELECT id,status,base_snapshot_json,config_revision,draft_revision,created_at FROM planning_runs WHERE status='approved' ORDER BY created_at DESC LIMIT 20"):
                try:
                    if json.loads(row['base_snapshot_json']).get('calendarBinding', {}).get('schoolId') == profile['schoolId']:
                        candidates.append({'runId': row['id'], 'status': row['status'], 'configRevision': row['config_revision'],
                            'draftRevision': row['draft_revision'] + 1, 'createdAt': row['created_at'],
                            'periodStart': json.loads(row['base_snapshot_json'])['calendarBinding']['weekStart']})
                except (ValueError, TypeError, KeyError, AttributeError):
                    continue
        readiness = _prepare(connection, PreviewInput(runId=candidates[0]['runId'], expectedActiveVersion=active['version'] if active else 0, expectedDraftRevision=draft_revision), profile) if candidates else {'ready': False, 'blockers': [_block('approved_run_required', 'Нужен утверждённый нормативный расчёт, совпадающий с текущей сеткой')]}
        return {'active': active, 'activeVersion': active['version'] if active else 0, 'history': history, 'totalCount': count,
                'historyLimit': HISTORY_LIMIT, 'draftRevision': draft_revision, 'approvedRuns': candidates,
                'readiness': {'ready': readiness['ready'], 'blockers': readiness['blockers']},
                'canManage': 'publication.publish' in profile.get('permissions', []), 'deliveryReadiness': _delivery_readiness()}
    finally:
        if connection is not None:
            connection.close()


router = APIRouter(prefix='/api/school/publications', tags=['internal school publications'])


@router.get('')
def get_publications(profile=Depends(_reader)):
    return publication_status(profile)


@router.post('/preview')
def preview(body: PreviewInput, profile=Depends(_reader)):
    return preview_publication(body, profile)


@router.post('/publish')
def publish(body: PublishInput, profile=Depends(_publisher)):
    return publish_publication(body, profile)
