"""Explicit manager-initiated request constraint over a copied confirmed plan.

No calendar/config mutations or inferred teacher identities. The same binding is
revalidated before proposal, approval and publication using the current domain DB.
"""
from copy import deepcopy
from datetime import date, datetime, timedelta
import hashlib
import json
import sqlite3
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from . import launch_identity, launch_runtime, school_requests
from .db import table_exists
from .scheduling_models import PlanningInput
from .scheduling_solver import validate_solution
from .school_publications import read_active_snapshot


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _block(code, message):
    raise HTTPException(409, {'code': code, 'message': message})


def today():
    return datetime.now(ZoneInfo('Europe/Moscow')).date()


def canonical_manager(profile):
    """Verify the actual profile; undo only the trusted legacy planner role adapter."""
    if (not isinstance(profile, dict) or profile.get('membershipVerified') is not True
            or any(not isinstance(profile.get(k),str) or not profile[k] for k in ('schoolId','userId','membershipId'))
            or type(profile.get('membershipRevision')) is not int or profile['membershipRevision'] < 0):
        raise HTTPException(403, 'Нет действующего школьного доступа')
    value = deepcopy(profile)
    if value.get('staffRole') == 'planner' and value.get('role') == 'admin':
        value['role'] = 'planner'
    if not launch_identity.membership_current(value):
        raise HTTPException(403, 'Доступ составителя расчёта не подтверждён')
    expected = launch_identity.resolve_membership(value['schoolId'], value['membershipId'])
    if not expected or any(value.get(k) != expected.get(k) for k in
            ('schoolId', 'userId', 'membershipId', 'membershipRevision', 'role', 'permissions')):
        raise HTTPException(403, 'Составитель не совпадает с авторитетным школьным профилем')
    return value


def _manager(profile):
    value = launch_runtime.actor(canonical_manager(profile), 'launch.manage')
    school_requests.require_bound_school(value['schoolId'])
    return value


def state_from_connection(connection):
    exists = table_exists(connection, 'demo_state')
    row = connection.execute('SELECT payload FROM demo_state WHERE id=1').fetchone() if exists else None
    return json.loads(row[0]) if row else {}


def _request(state, school_id, request_id):
    result = state.get('_schoolRequests', {}).get('schools', {}).get(school_id, {}).get('requests', {}).get(request_id)
    if not result or result.get('schoolId') != school_id:
        _block('school_request_not_found', 'Школьная заявка не найдена в текущем контуре')
    return result


def _fingerprint(item):
    # Derived UI reply/application metadata are separate from the reported facts.
    return digest({k: item.get(k) for k in ('id', 'schoolId', 'kind', 'text', 'receivedAt',
        'resolvedDate', 'slot', 'lessonIds', 'lesson', 'owner', 'ownerBinding', 'questions', 'messages', 'proposal')})


def _absence_facts(state, school_id):
    store = state.get('_approvedAbsences')
    if store is None:
        return {}
    if not isinstance(store, dict) or store.get('formatVersion') != 1:
        raise ValueError('Invalid approved absence ledger')
    facts = store['schools'].get(school_id, {}).get('facts', {})
    if not isinstance(facts, dict):
        raise ValueError('Invalid approved absence facts')
    return facts


def _compact_absence_fact(state, item, school_id):
    """Validate a durable fact against both original request/audit anchors.

    Only mark_applied writes this ledger. Checksums detect a changed fact even
    after bounded calculation and command histories have been pruned; the two
    request anchors also reject a recomputed fact checksum.
    """
    fact = _absence_facts(state, school_id).get(item['id'])
    receipt = item.get('draftReceipt') or {}
    audit = item.get('audit') or []
    approval = audit[-1] if audit else {}
    anchors = (receipt.get('approvedAbsenceHash'), approval.get('approvedAbsenceHash'))
    if fact is None:
        if any(anchor is not None for anchor in anchors):
            raise ValueError('Missing anchored approved absence fact')
        return None  # A legacy record still requires the full domain proof.
    if not isinstance(fact, dict):
        raise ValueError('Invalid compact absence fact')
    checksum = digest({key:value for key,value in fact.items() if key != 'hash'})
    if (fact.get('formatVersion') != 1 or fact.get('hash') != checksum
            or anchors != (checksum, checksum)
            or fact.get('schoolId') != school_id or fact.get('requestId') != item['id']
            or item.get('schoolId') != school_id or item.get('kind') != 'unavailability'
            or item.get('status') != 'applied' or item.get('revision') != fact.get('requestRevision')
            or item.get('appliedPlanningRunId') != fact.get('runId')
            or item.get('resolvedDate') != fact.get('reportedDate')
            or _fingerprint(item) != fact.get('requestFingerprint')
            or digest(item.get('ownerBinding')) != fact.get('ownerBindingHash')
            or digest(audit[:-1]) != fact.get('requestAuditHash')
            or approval != {**fact.get('approvalAudit', {}), 'approvedAbsenceHash':checksum}
            or receipt != {'revision':fact.get('draftRevision'), 'runId':fact.get('runId'),
                'requestBindingHash':fact.get('requestBindingHash'), 'approvedAbsenceHash':checksum}):
        raise ValueError('Approved absence fact or request anchor changed')
    return fact


def _owner(item, manager):
    binding = item.get('ownerBinding') or {}
    owner = launch_runtime.resolve_member(manager, binding.get('membershipId'))
    if (owner.get('staffRole', owner.get('role')) != 'teacher'
            or any(owner.get(k) != binding.get(k) for k in ('schoolId','userId','membershipId','membershipRevision'))):
        _block('request_owner_changed', 'Текущая привязка автора заявки изменилась')
    return owner


def _owner_authority(owner):
    return digest({k:owner.get(k) for k in ('schoolId','userId','membershipId','membershipRevision',
        'role','staffRole','permissions','teacherRefs','calendarTeacherIds')})


def _grid(lessons):
    fields = ('id','className','day','slot','start','end','subject','teachers','rooms')
    return sorted([{k: sorted(row[k]) if k in {'teachers','rooms'} else row[k] for k in fields}
                   for row in lessons], key=lambda row: row['id'])


def _provenance_grid(lessons):
    """Compare public publication provenance to internal materialized rows."""
    sources = {row['id']: row for row in lessons}
    return [{**row,
             'planningEventId': sources[row['id']].get('planningEventId', sources[row['id']].get('_planningEventId')),
             'planningGroups': sorted(sources[row['id']].get('planningGroups', sources[row['id']].get('_planningGroups')) or [], key=lambda group:group['id'])}
            for row in _grid(lessons)]


def _generated_source(connection, row, school_id):
    """Derive identities only from one approved, independently verified result."""
    from .planning import PlanningService
    if (row['status'] != 'approved' or row['cancel_requested'] or not row['applied_task_id']
            or row['proposal_task_id'] != row['applied_task_id']):
        _block('request_source_mapping_required', 'Исходный расчёт не имеет точного подтверждения')
    base = json.loads(row['base_snapshot_json'])
    if base.get('calendarBinding', {}).get('schoolId') != school_id:
        _block('request_source_mapping_required', 'Исходный расчёт относится к другой школе')
    materialized = PlanningService._verified_materialization(row)
    if PlanningService._run_hash(row) != row['proposal_hash']:
        _block('request_source_mapping_required', 'Исходный расчёт отличается от согласованного')
    snapshot = json.loads(row['input_json'])
    seed = base.get('materializationRunId', row['id'])
    events = {'pe_' + hashlib.sha256((seed + '|' + f"{d['id']}_{n}").encode()).hexdigest()[:24]:
              {'eventId': f"{d['id']}_{n}", 'demandId': d['id']}
              for d in snapshot['demands'] for n in range(d['weeklyCount'])}
    mapping = {lesson['id']: deepcopy(events[lesson['_planningEventId']]) for lesson in materialized}
    proof = {'runId': row['id'], 'runHash': PlanningService._run_hash(row),
             'configRevision': row['config_revision'], 'draftRevision': row['draft_revision'] + 1,
             'materializationRunId': seed, 'mappingHash': digest(mapping),
             'sourceGridHash': digest(_provenance_grid(materialized))}
    return {'proof': proof, 'mapping': mapping, 'lessons': materialized}


def _find_generated_source(connection, plan, draft, school_id):
    from .planning import PlanningService
    config = PlanningService._config(connection)
    cursor = connection.execute("SELECT * FROM planning_runs WHERE status='approved' AND config_revision=? AND draft_revision=? AND applied_task_id IS NOT NULL AND cancel_requested=0",
        (config['revision'], draft['revision'] - 1))
    candidates = [dict(zip((column[0] for column in cursor.description), row)) for row in cursor.fetchall()]
    stored = connection.execute('SELECT payload FROM timetable_draft WHERE id=1').fetchone()
    actual = json.loads(stored[0]) if stored else {'lessons': []}
    found = []
    for row in candidates:
        try:
            source = _generated_source(connection, row, school_id)
            if _provenance_grid(source['lessons']) == _provenance_grid(actual['lessons']):
                found.append(source)
        except (HTTPException, ValueError, KeyError, TypeError):
            continue
    if len(found) != 1:
        _block('request_source_mapping_required', 'Нужно полное sourceLessonIds или единственный точно применённый школьный расчёт')
    return found[0]


def _verify_generated_source(connection, proof, school_id, published):
    from .planning import PlanningService
    if not isinstance(proof, dict):
        _block('request_source_mapping_required', 'Нет точного доказательства соответствия событий')
    cursor = connection.execute('SELECT * FROM planning_runs WHERE id=?', (proof.get('runId'),))
    fetched = cursor.fetchone()
    row = dict(zip((column[0] for column in cursor.description), fetched)) if fetched is not None else None
    if row is None or PlanningService._config(connection)['revision'] != proof.get('configRevision'):
        _block('request_source_mapping_required', 'Исходный расчёт или его нормативы изменились')
    source = _generated_source(connection, row, school_id)
    if source['proof'] != proof or _provenance_grid(source['lessons']) != _provenance_grid(published['lessons']):
        _block('request_source_mapping_required', 'Сопоставление событий или исходная опубликованная сетка изменились')
    return source


def _assert_baseline(plan, draft, source=None):
    from .planning import PlanningService
    events = {f"{d['id']}_{n}" for d in plan['demands'] for n in range(d['weeklyCount'])}
    if {a['eventId'] for a in plan['baseline']} != events:
        _block('request_complete_baseline_required', 'Для ремонта нужен полный подтверждённый baseline всех занятий')
    source_ids = [sid for d in plan['demands'] for sid in d.get('sourceLessonIds', [])]
    if source is None and (len(source_ids) != len(draft['lessons']) or set(source_ids) != {row['id'] for row in draft['lessons']}):
        _block('request_source_mapping_required', 'Нужно полное однозначное сопоставление sourceLessonIds текущей сетке')
    if not validate_solution(plan, plan['baseline'])['valid']:
        _block('request_baseline_invalid', 'Исходный baseline не прошёл проверку подтверждённого плана и календаря')
    materialized = PlanningService.materialize({'id':'baseline_validation',
        'base_snapshot_json':json.dumps({'materializationRunId':source['proof']['materializationRunId']} if source else {}), 'input_json':json.dumps(plan),
        'result_json':json.dumps({'assignments':plan['baseline']})})
    if _grid(materialized) != _grid(draft['lessons']):
        _block('request_baseline_draft_mismatch', 'Baseline не соответствует точным занятиям текущего черновика')


def build_effect(connection, request_id, effective_plan, draft, manager_profile):
    manager = _manager(manager_profile)
    state = state_from_connection(connection)
    from .school_calendar import calendar_effect_for_plan
    if not calendar_effect_for_plan(state, effective_plan, school_id=manager['schoolId'], require_configured=True)['ready']:
        _block('request_calendar_required', 'Для подбора по заявке нужен подтверждённый календарь выбранной недели')
    item = _request(state, manager['schoolId'], request_id)
    if item.get('kind') != 'unavailability' or item.get('status') != 'submitted':
        _block('request_not_ready_for_repair', 'Нужна заявка недоступности с полученными уточнениями и статусом submitted')
    if any(q.get('state') == 'open' for q in item.get('questions', [])):
        _block('request_answers_required', 'Сначала нужны ответы на все открытые вопросы заявки')
    owner = _owner(item, manager)
    try:
        reported = date.fromisoformat(item['resolvedDate'])
        start = date.fromisoformat(effective_plan['periodStart'])
    except (ValueError, TypeError, KeyError):
        _block('request_fixed_date_required', 'У заявки должна быть одна подтверждённая календарная дата')
    if not start <= reported <= start + timedelta(days=6):
        _block('request_date_outside_plan', 'Дата заявки не относится к выбранной неделе плана')
    if reported < today():
        _block('request_date_expired', 'Дата недоступности уже прошла; нужно новое подтверждённое обращение')
    plan = PlanningInput.model_validate(deepcopy(effective_plan)).model_dump()
    if not plan['confirmed']:
        _block('request_confirmed_plan_required', 'Нормативный план не подтверждён')
    published = read_active_snapshot(manager['schoolId'], connection)
    if not published or published.get('periodStart') != plan['periodStart']:
        _block('request_own_publication_required', 'Нужна действующая публикация той же недели для автора заявки')
    # Older explicit drafts retain their strict sourceLessonIds contract. A
    # generated grid has durable event provenance instead of guessed source IDs.
    source_ids = [sid for demand in plan['demands'] for sid in demand.get('sourceLessonIds', [])]
    explicit = len(source_ids) == len(draft['lessons']) and set(source_ids) == {row['id'] for row in draft['lessons']}
    source = None if explicit else _find_generated_source(connection, plan, draft, manager['schoolId'])
    if source is not None:
        _verify_generated_source(connection, source['proof'], manager['schoolId'], published)
    _assert_baseline(plan, draft, source)
    refs = owner.get('teacherRefs') or []
    teacher_ids = owner.get('calendarTeacherIds') or []
    if not refs or not teacher_ids or not set(teacher_ids) <= {t['id'] for t in plan['teachers']}:
        _block('request_teacher_mapping_required', 'Не подтверждены явные calendarTeacherIds автора в нормативном плане')
    lesson_ids = item.get('lessonIds') or []
    if not lesson_ids or len(lesson_ids) != len(set(lesson_ids)):
        _block('request_published_lessons_required', 'Укажите конкретные собственные опубликованные уроки без повторов')
    published_rows = {row['id']: row for row in published['lessons']}
    added = {}
    for lesson_id in lesson_ids:
        lesson = published_rows.get(lesson_id)
        if not lesson or not set(refs) & set(lesson['teachers']):
            _block('request_foreign_published_lesson', 'В заявке указан не собственный опубликованный урок')
        when = start + timedelta(days=lesson['day'])
        if when != reported or (item.get('slot') is not None and item['slot'] != lesson['slot']):
            _block('request_lesson_date_mismatch', 'Дата или номер заявки не совпадают с собственным опубликованным уроком')
        mapped = [d for d in plan['demands'] if (source['mapping'].get(lesson_id, {}).get('demandId') == d['id']
            if source else lesson_id in d.get('sourceLessonIds', []))]
        if len(mapped) != 1:
            _block('request_source_mapping_required', 'Нет однозначного sourceLessonIds для затронутого урока')
        affected_teachers = set(mapped[0]['teacherIds']) & set(teacher_ids)
        if len(affected_teachers) != 1:
            _block('request_teacher_mapping_ambiguous', 'Нет однозначного явного нормативного преподавателя для этого урока')
        slots = [s for s in plan['slots'] if (s['day'],s['slot'],s['start'],s['end']) ==
                 (reported.weekday(),lesson['slot'],lesson['start'],lesson['end'])]
        if len(slots) != 1:
            _block('request_slot_mapping_required', 'Фактический интервал урока не сопоставлен нормативному слоту')
        # Mark every overlapping resource slot, not just one slot-number alias.
        affected_slots = {s['id'] for s in plan['slots'] if s['day'] == reported.weekday()
            and s['start'] < lesson['end'] and lesson['start'] < s['end']}
        teacher_id = next(iter(affected_teachers))
        added.setdefault(teacher_id, set()).update(affected_slots)
    for teacher in plan['teachers']:
        teacher['unavailableSlotIds'] = sorted(set(teacher['unavailableSlotIds']) | added.get(teacher['id'], set()))
    result = PlanningInput.model_validate(plan).model_dump()
    binding = {'formatVersion':1, 'schoolId':manager['schoolId'], 'requestId':item['id'],
        'requestRevision':item['revision'], 'requestFingerprint':_fingerprint(item),
        'requestAuditHash':digest(item.get('audit', [])), 'ownerBinding':deepcopy(item['ownerBinding']),
        'ownerAuthorityHash':_owner_authority(owner),
        'publicationVersion':published['version'], 'publicationHash':digest(published),
        'reportedDate':reported.isoformat(), 'lessonIds':sorted(lesson_ids),
        'effects':{key:sorted(value) for key,value in sorted(added.items())},
        'baseEffectiveInputHash':digest(effective_plan), 'effectiveInputHash':digest(result)}
    if source is not None:
        binding['sourceMappingProof'] = source['proof']
    return {'effectiveInput':result, 'requestBinding':binding,
            **({'materializationRunId':source['proof']['materializationRunId']} if source else {})}


def assert_binding_current(connection, binding, *, run_id=None, allow_applied=False, applied_draft_revision=None):
    if not isinstance(binding, dict) or binding.get('formatVersion') != 1:
        _block('request_binding_required', 'Нет точного происхождения ограничения школьной заявки')
    school_requests.require_bound_school(binding['schoolId'])
    state = state_from_connection(connection)
    item = _request(state, binding['schoolId'], binding['requestId'])
    owner = launch_identity.resolve_membership(binding['schoolId'], binding['ownerBinding']['membershipId'])
    if (not owner or not launch_identity.membership_current(owner)
            or owner.get('staffRole',owner.get('role')) != 'teacher'
            or _owner_authority(owner) != binding.get('ownerAuthorityHash')
            or any(owner.get(k) != binding['ownerBinding'].get(k) for k in ('schoolId','userId','membershipId','membershipRevision'))):
        _block('request_owner_changed', 'Доступ автора исходной заявки изменился или не подтверждён')
    if date.fromisoformat(binding['reportedDate']) < today():
        _block('request_date_expired', 'Дата обращения прошла')
    if _fingerprint(item) != binding['requestFingerprint']:
        _block('request_changed', 'Сведения или ответы исходной заявки изменились после расчёта')
    if item.get('status') == 'submitted' and item['revision'] == binding['requestRevision']:
        if digest(item.get('audit', [])) != binding['requestAuditHash']:
            _block('request_changed', 'История исходной заявки изменилась после расчёта')
    elif allow_applied and item.get('status') == 'applied' and item['revision'] == binding['requestRevision']+1:
        receipt = item.get('draftReceipt') or {}
        if (item.get('appliedPlanningRunId') != run_id or receipt.get('runId') != run_id
                or receipt.get('requestBindingHash') != digest(binding)
                or (applied_draft_revision is not None and receipt.get('revision') != applied_draft_revision)
                or digest(item.get('audit', [])[:-1]) != binding['requestAuditHash']
                or not item.get('audit') or item['audit'][-1].get('action') != 'planning_approved'):
            _block('request_application_mismatch', 'Нет точной квитанции применения заявки этим расчётом')
        try:
            _compact_absence_fact(state, item, binding['schoolId'])
        except (ValueError, TypeError, KeyError, AttributeError):
            _block('prior_absence_evidence_required', 'Подтверждение согласованной недоступности изменилось')
    else:
        _block('request_changed', 'Заявка завершена, отозвана или её ревизия изменилась')
    published = read_active_snapshot(binding['schoolId'], connection)
    if not published or published['version'] != binding['publicationVersion'] or digest(published) != binding['publicationHash']:
        _block('request_publication_changed', 'Публикация, на которую ссылается заявка, изменилась')
    if binding.get('sourceMappingProof') is not None:
        _verify_generated_source(connection, binding['sourceMappingProof'], binding['schoolId'], published)
    return item


def reapply_effect(connection, binding, base_effective, *, run_id=None, allow_applied=False, applied_draft_revision=None):
    assert_binding_current(connection, binding, run_id=run_id, allow_applied=allow_applied,
        applied_draft_revision=applied_draft_revision)
    if digest(base_effective) != binding['baseEffectiveInputHash']:
        _block('request_base_plan_changed', 'Нормативные или календарные условия ограничения изменились')
    result = deepcopy(base_effective)
    teachers = {t['id']:t for t in result['teachers']}
    for ident, slots in binding['effects'].items():
        if ident not in teachers or not set(slots) <= {s['id'] for s in result['slots']}:
            _block('request_effect_changed', 'Сопоставление ограничения изменилось')
        teachers[ident]['unavailableSlotIds'] = sorted(set(teachers[ident]['unavailableSlotIds']) | set(slots))
    result = PlanningInput.model_validate(result).model_dump()
    if digest(result) != binding['effectiveInputHash']:
        _block('request_effect_changed', 'Вход решателя отличается от точного ограничения заявки')
    return result


def mark_applied(connection, binding, run_id, draft_revision, manager_profile):
    """Commit the finite absence fact in the draft approval transaction only.

    apply_batch invokes this guard before saving the draft and inserting its
    receipt. Any later CAS/save/receipt failure rolls the fact back as well.
    """
    from .planning import PlanningService
    manager = _manager(manager_profile)
    assert_binding_current(connection, binding)
    if manager['schoolId'] != binding['schoolId']:
        _block('request_school_changed', 'Заявка относится к другой школе')
    state = state_from_connection(connection)
    item = _request(state, binding['schoolId'], binding['requestId'])
    cursor = connection.execute('SELECT * FROM planning_runs WHERE id=?', (run_id,))
    fetched = cursor.fetchone()
    if fetched is None:
        _block('prior_absence_evidence_required', 'Нет согласованного расчёта для подтверждения недоступности')
    row = dict(zip((column[0] for column in cursor.description), fetched))
    base = json.loads(row['base_snapshot_json'])
    if (row['status'] != 'approved' or row['cancel_requested'] or not row['applied_task_id']
            or row['applied_task_id'] != row['proposal_task_id']
            or row['draft_revision'] + 1 != draft_revision
            or base.get('requestBinding') != binding
            or PlanningService._run_hash(row) != row['proposal_hash']):
        _block('prior_absence_evidence_required', 'Расчёт не подтверждает точное применение недоступности')
    materialized = PlanningService._verified_materialization(row)
    audit_entry = {'action':'planning_approved', 'runId':run_id,
        'membershipId':manager['membershipId'], 'at':datetime.now(ZoneInfo('Europe/Moscow')).isoformat()}
    try:
        if item['id'] in _absence_facts(state, binding['schoolId']):
            raise ValueError('Approved absence facts are immutable')
        fact = _absence_fact(row, binding, json.loads(row['input_json']), materialized,
            item['revision']+1, audit_entry)
    except (ValueError, TypeError, KeyError, AttributeError):
        _block('prior_absence_evidence_required', 'Нельзя изменить или переопределить подтверждённый факт недоступности')
    item.update(status='applied', revision=item['revision']+1, appliedPlanningRunId=run_id,
        draftReceipt={'revision':draft_revision, 'runId':run_id, 'requestBindingHash':digest(binding),
            'approvedAbsenceHash':fact['hash']},
        serverReply='Согласованное изменение заявки сохранено в черновик. Публикация и уведомления не выполнялись.')
    item['audit'].append({**audit_entry, 'approvedAbsenceHash':fact['hash']})
    store = state.setdefault('_approvedAbsences', {'formatVersion':1, 'schools':{}})
    store['schools'].setdefault(binding['schoolId'], {'facts':{}})['facts'][item['id']] = fact
    connection.execute('UPDATE demo_state SET payload=? WHERE id=1', (json.dumps(state,ensure_ascii=False),))


def _absence_fact(row, binding, snapshot, materialized, request_revision, audit_entry):
    """Compact immutable evidence, derived only from a verified approved result."""
    from .planning import PlanningService
    from .timetable import batch_hash, canonical
    reported = date.fromisoformat(binding['reportedDate'])
    start = date.fromisoformat(snapshot['periodStart'])
    if not start <= reported <= start + timedelta(days=6):
        raise ValueError('Absence date does not belong to the approved week')
    teachers = {teacher['id']:teacher for teacher in snapshot['teachers']}
    slots = {slot['id']:slot for slot in snapshot['slots']}
    effects = {}
    for teacher_id, unavailable in binding['effects'].items():
        if teacher_id not in teachers or not unavailable or len(unavailable) != len(set(unavailable)):
            raise ValueError('Invalid absence resource mapping')
        intervals = []
        for slot_id in sorted(unavailable):
            slot = slots[slot_id]
            if slot['day'] != reported.weekday():
                raise ValueError('Invalid absence interval date')
            intervals.append({key:slot[key] for key in ('id','day','start','end')})
        effects[teacher_id] = {'identityHash':digest(canonical(teachers[teacher_id]['name'])), 'intervals':intervals}
    if not effects:
        raise ValueError('Empty approved absence')
    fact = {'formatVersion':1, 'schoolId':binding['schoolId'], 'requestId':binding['requestId'],
        'requestRevision':request_revision, 'requestFingerprint':binding['requestFingerprint'],
        'requestAuditHash':binding['requestAuditHash'], 'ownerBindingHash':digest(binding['ownerBinding']),
        'periodStart':snapshot['periodStart'], 'reportedDate':binding['reportedDate'],
        'runId':row['id'], 'runHash':PlanningService._run_hash(row), 'requestBindingHash':digest(binding),
        'draftRevision':row['draft_revision']+1, 'commandKey':row['applied_task_id'],
        'commandHash':batch_hash(materialized,row['draft_revision'],'Автоматический расчёт'),
        'approvalAudit':deepcopy(audit_entry), 'effects':effects}
    fact['hash'] = digest(fact)
    return fact


def _legacy_absence_fact(connection, item, school_id, period_start):
    """Read-only legacy proof. Never create durable approval evidence on preview."""
    from .planning import PlanningService
    from .timetable import batch_hash
    source_id = item['appliedPlanningRunId']
    cursor = connection.execute('SELECT * FROM planning_runs WHERE id=?', (source_id,))
    fetched = cursor.fetchone()
    if fetched is None:
        raise ValueError('Missing legacy approved run')
    row = dict(zip((column[0] for column in cursor.description), fetched))
    base = json.loads(row['base_snapshot_json'])
    binding = base.get('requestBinding') or {}
    snapshot = json.loads(row['input_json'])
    receipt = item.get('draftReceipt') or {}
    audit = item.get('audit') or []
    if (row['status'] != 'approved' or row['cancel_requested'] or not row['applied_task_id']
            or row['applied_task_id'] != row['proposal_task_id']
            or PlanningService._run_hash(row) != row['proposal_hash']
            or snapshot['periodStart'] != period_start
            or binding.get('schoolId') != school_id or binding.get('requestId') != item['id']
            or binding.get('reportedDate') != item['resolvedDate']
            or binding.get('ownerBinding') != item.get('ownerBinding')
            or _fingerprint(item) != binding.get('requestFingerprint')
            or item['revision'] != binding.get('requestRevision', -2) + 1
            or receipt.get('runId') != source_id or receipt.get('revision') != row['draft_revision'] + 1
            or receipt.get('requestBindingHash') != digest(binding)
            or not audit or audit[-1].get('action') != 'planning_approved' or audit[-1].get('runId') != source_id
            or digest(audit[:-1]) != binding.get('requestAuditHash')):
        raise ValueError('Invalid legacy approved absence evidence')
    materialized = PlanningService._verified_materialization(row)
    domain = connection.execute('SELECT command_hash,actor_id,actor_role,result FROM timetable_command_receipts WHERE command_key=?', (row['applied_task_id'],)).fetchone()
    if (domain is None or tuple(domain[:3]) != (batch_hash(materialized,row['draft_revision'],'Автоматический расчёт'),row['owner_id'],row['owner_role'])):
        raise ValueError('Missing exact legacy absence receipt')
    committed = json.loads(domain[3])
    if committed.get('revision') != row['draft_revision']+1 or _grid(committed['lessons']) != _grid(materialized):
        raise ValueError('Legacy receipt grid mismatch')
    return _absence_fact(row, binding, snapshot, materialized, item['revision'], audit[-1])


def _absence_effects(fact, plan):
    from .timetable import canonical
    reported = date.fromisoformat(fact['reportedDate'])
    start = date.fromisoformat(plan['periodStart'])
    if fact['periodStart'] != plan['periodStart'] or not start <= reported <= start + timedelta(days=6):
        raise ValueError('Approved absence belongs to another week')
    teachers = {teacher['id']:teacher for teacher in plan['teachers']}
    slots = {slot['id']:slot for slot in plan['slots']}
    effects = {}
    if not fact['effects']:
        raise ValueError('Empty approved absence effects')
    for teacher_id, value in fact['effects'].items():
        if (teacher_id not in teachers or digest(canonical(teachers[teacher_id]['name'])) != value['identityHash']
                or not value['intervals']):
            raise ValueError('Teacher mapping changed')
        blocked = set()
        for interval in value['intervals']:
            current = slots.get(interval['id'])
            if (not current or interval['day'] != reported.weekday()
                    or any(current[field] != interval[field] for field in ('day','start','end'))):
                raise ValueError('Absence interval mapping changed')
            blocked.update(slot['id'] for slot in slots.values() if slot['day'] == interval['day']
                and slot['start'] < interval['end'] and interval['start'] < slot['end'])
        effects[teacher_id] = sorted(blocked)
    return effects


def prior_absence_effect(connection, plan, profile, *, exclude_run_id=None):
    """Reapply finite approved facts without retaining unbounded solver history.

    Compact facts are anchored in the immutable request receipt and audit. Older
    approvals without that fact still require the full original run/receipt proof.
    Reading a preview never writes or upgrades historical evidence.
    """
    result = deepcopy(plan)
    school_id = profile['schoolId']
    start = date.fromisoformat(plan['periodStart'])
    teachers = {teacher['id']:teacher for teacher in result['teachers']}
    state = state_from_connection(connection)
    requests = state.get('_schoolRequests', {}).get('schools', {}).get(school_id, {}).get('requests', {})
    try:
        facts = _absence_facts(state, school_id)
        for request_id, fact in facts.items():
            if request_id not in requests:
                raise ValueError('Missing request for anchored absence fact')
            # Validate integrity before selecting a week: changing both stored
            # dates must not make a corrupted fact disappear from the old week.
            _compact_absence_fact(state, requests[request_id], school_id)
    except (ValueError, TypeError, KeyError, AttributeError):
        _block('prior_absence_evidence_required', 'Нарушена целостность подтверждённых недоступностей')
    evidence = []
    for item in sorted(requests.values(), key=lambda value:value.get('id', '')):
        try:
            compact = facts.get(item['id'])
            relevant_fact = compact is not None and compact.get('periodStart') == plan['periodStart']
            if not relevant_fact and (item.get('kind') != 'unavailability' or item.get('status') != 'applied'):
                continue
            reported = date.fromisoformat(item['resolvedDate'])
            if not relevant_fact and not start <= reported <= start + timedelta(days=6):
                continue
            fact = _compact_absence_fact(state, item, school_id)
            source_id = item['appliedPlanningRunId']
            if source_id == exclude_run_id:
                continue
            if fact is None:
                fact = _legacy_absence_fact(connection, item, school_id, plan['periodStart'])
            effects = _absence_effects(fact, result)
            for teacher_id, blocked in effects.items():
                teachers[teacher_id]['unavailableSlotIds'] = sorted(set(teachers[teacher_id]['unavailableSlotIds']) | set(blocked))
            evidence.append({'requestId':item['id'], 'requestRevision':item['revision'],
                'runId':source_id, 'runHash':fact['runHash'], 'requestBindingHash':fact['requestBindingHash'],
                'effectsHash':digest(effects), **({'approvedAbsenceHash':fact['hash']} if compact is not None else {})})
        except (ValueError, TypeError, KeyError, AttributeError, HTTPException, sqlite3.Error):
            _block('prior_absence_evidence_required', 'Ранее согласованная недоступность требует проверки: несовместимы нормативы, интервалы или доказательства применения')
    binding = {'formatVersion':1, 'schoolId':school_id, 'periodStart':plan['periodStart'], 'effects':evidence}
    binding['hash'] = digest(binding)
    return {'effectiveInput':result, 'priorAbsenceBinding':binding}
