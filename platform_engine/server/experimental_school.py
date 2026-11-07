"""Isolated working-assumption candidates; never activates a normative plan.

The existing CP-SAT solver and independent validator compute an explicitly
authorised experiment. Its complete provenance, assumptions and projection live
in a separate namespace; this calculation activates no timetable or publication.
Experimental approval/publication is a separate explicit workflow.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import threading
import time

from fastapi import APIRouter, Depends, HTTPException, Path as APIPath
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing import Literal

from . import db
from .auth import require_roles
from .school_ops import _snapshot
from .school_plan_imports import _actor
from .school_plan_sources import load_source_package
from .school_source_registry import _bounded_draft
from .school_schedule_rules import validate_school_schedule_rules
from .scheduling_solver import MAX_SOLVE_SECONDS, solve_plan, validate_solution

NAMESPACE = '_schoolExperimentalDrafts'
MAX_RUNS = 8
# Full-school candidates include the model, provenance and editable projection.
# Leave room for other registries within the director snapshot's 32 MiB cap.
MAX_BYTES = 24 * 1024 * 1024
PROCESS = secrets.token_hex(16)
ACTIVE = {'queued', 'running'}
RESULTS = {'optimal', 'feasible'}
_lock = threading.RLock()
_executors = {}
_futures = {}
LEASE_SECONDS = 120
DEFAULT_LONG_CALCULATION_SECONDS = 600
HEARTBEAT_SECONDS = 5
SEED_PATH = Path(__file__).resolve().parent / 'assets' / 'school-experiment-seed-2026-10-26.json'


def _process_start(pid):
    """Linux start ticks prevent PID reuse; portable fallback is a short lease."""
    from pathlib import Path
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return fields[19]
    except (OSError, IndexError):
        return None


def _live_owner(item):
    try:
        stamp = (item['public'].get('calculation') or {}).get('heartbeatAt') or item['public']['updatedAt']
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(stamp.replace('Z','+00:00'))).total_seconds()
        if elapsed < 0 or elapsed > LEASE_SECONDS:
            return False
        pid = item.get('processPid')
        if type(pid) is not int or pid < 1:
            return item.get('process') == PROCESS
        if pid == os.getpid():
            return item.get('process') == PROCESS
        os.kill(pid, 0)
        birth = _process_start(pid)
        return not item.get('processStart') or birth == item['processStart']
    except (OSError, TypeError, ValueError, KeyError):
        return False


def _active(item):
    if item['public']['status'] not in ACTIVE or not _live_owner(item):
        return False
    future = _futures.get((str(db.database_path()), item['public']['id']))
    return not (item.get('process') == PROCESS and future is not None and future.done())


def _utc():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


class ExperimentalInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    periodStart: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    expectedDraftRevision: int = Field(ge=0, le=2**31-1)
    expectedPackageHash: str = Field(pattern=r'^[a-f0-9]{64}$')
    requestId: str = Field(min_length=16, max_length=80, pattern=r'^[A-Za-z0-9_-]+$')
    useWorkingAssumptions: Literal[True]
    # An omitted field retains the original request identity and 30s contract.
    # New clients explicitly choose their longer, immutable command budget.
    timeLimitSeconds: int = Field(default=30, ge=30, le=MAX_SOLVE_SECONDS)

    @field_validator('periodStart')
    @classmethod
    def monday(cls, value):
        target = date.fromisoformat(value)
        if target.weekday() != 0:
            raise ValueError('Укажите понедельник экспериментальной недели.')
        try:
            target + timedelta(days=5)
        except OverflowError:
            raise ValueError('Учебная неделя выходит за допустимый диапазон дат.') from None
        return value

    @field_validator('useWorkingAssumptions', mode='before')
    @classmethod
    def explicit_mode(cls, value):
        if type(value) is not bool or value is not True:
            raise ValueError('Эксперимент требует явного режима рабочих предположений.')
        return value


def _stored(state, school_id):
    value = state.get(NAMESPACE)
    if value is None:
        return {'version': 1, 'schoolId': school_id, 'candidates': []}
    if (not isinstance(value, dict) or value.get('version') != 1 or value.get('schoolId') != school_id
            or not isinstance(value.get('candidates'), list) or len(value['candidates']) > MAX_RUNS):
        raise HTTPException(503, 'Реестр экспериментальных черновиков требует проверки.')
    ids = []
    for item in value['candidates']:
        if (not isinstance(item, dict) or not isinstance(item.get('public'), dict)
                or item['public'].get('mode') != 'working_assumptions'
                or item['public'].get('schoolId') != school_id
                or not isinstance(item.get('commandHash'), str)
                or not isinstance(item.get('receipt'), dict)
                or not isinstance(item.get('input'), dict)
                or not isinstance(item['public'].get('sourceBinding'), dict)
                or item['public'].get('status') not in ACTIVE | RESULTS | {'unknown','infeasible','failed','stale'}
                or not re.fullmatch(r'exp_[a-f0-9]{32}', item['public'].get('id',''))):
            raise HTTPException(503, 'Экспериментальный черновик требует проверки.')
        ids.append(item['public'].get('id'))
        limit = item.get('timeLimitSeconds', 30)
        if type(limit) is not int or not 30 <= limit <= MAX_SOLVE_SECONDS:
            raise HTTPException(503, 'Повреждён предел времени экспериментального расчёта.')
        calculation = item['public'].get('calculation')
        if calculation is not None and not _valid_calculation(calculation, limit):
            raise HTTPException(503, 'Повреждено состояние экспериментального расчёта.')
    if any(not isinstance(i, str) for i in ids) or len(ids) != len(set(ids)):
        raise HTTPException(503, 'Повреждены идентификаторы экспериментальных черновиков.')
    return deepcopy(value)


def _valid_calculation(value, limit):
    try:
        finite_elapsed = isinstance(value, dict) and math.isfinite(value.get('elapsedSeconds', float('nan')))
    except (TypeError, ValueError, OverflowError):
        return False
    if (not isinstance(value, dict) or set(value) != {'timeLimitSeconds','elapsedSeconds','phase','startedAt','heartbeatAt'}
            or type(value['timeLimitSeconds']) is not int or value['timeLimitSeconds'] != limit
            or isinstance(value['elapsedSeconds'], bool) or not isinstance(value['elapsedSeconds'], (int,float))
            or not finite_elapsed or value['elapsedSeconds'] < 0
            or not isinstance(value['phase'], str) or not 1 <= len(value['phase']) <= 80):
        return False
    if (value['startedAt'] is None) != (value['heartbeatAt'] is None):
        return False
    if value['startedAt'] is None and (value['phase'] != 'queued' or value['elapsedSeconds'] != 0):
        return False
    try:
        for key in ('startedAt','heartbeatAt'):
            if value[key] is not None:
                if not isinstance(value[key], str) or len(value[key]) > 40 or datetime.fromisoformat(value[key].replace('Z','+00:00')).tzinfo is None:
                    return False
        return True
    except (ValueError, TypeError):
        return False


def _calculation(limit):
    return {'timeLimitSeconds':limit, 'elapsedSeconds':0.0, 'phase':'queued', 'startedAt':None, 'heartbeatAt':None}


def _command_payload(body):
    # A legacy retry must have exactly its historical fingerprint. Explicit
    # budgets, including an explicit30, are bound to their new request.
    return body.model_dump(exclude={'timeLimitSeconds'} if 'timeLimitSeconds' not in body.model_fields_set else set())


def _current(draft, package):
    bounded = _bounded_draft(draft)
    return {'draftRevision': bounded['revision'], 'draftHash': _hash(bounded), 'packageHash': _hash(package)}


def assert_sources_current(connection, binding, *, package_loader=None):
    """Bind the saved candidate to authoritative raw sources in this SQL snapshot."""
    from . import timetable
    try:
        if (not isinstance(binding,dict) or set(binding) != {'draftRevision','draftHash','packageHash'}
                or type(binding.get('draftRevision')) is not int or binding['draftRevision'] < 0
                or any(not isinstance(binding.get(key),str) or not re.fullmatch(r'[a-f0-9]{64}',binding[key])
                    for key in ('draftHash','packageHash'))):
            raise ValueError('Invalid source binding')
        exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='timetable_draft'").fetchone()
        draft = timetable._load(connection) if exists else timetable._empty()
        current = _current(draft,(package_loader or load_source_package)())
        if current != binding:
            raise ValueError('Source binding changed')
        return current
    except (HTTPException,ValueError,TypeError,KeyError,OSError,sqlite3.DatabaseError):
        raise HTTPException(409,'Исходная сетка или документы изменились либо требуют проверки. Нужен новый расчёт по актуальным источникам.') from None


def _build(package, draft, period_start):
    from .school_experiment_builder import build_experimental_plan
    return build_experimental_plan(package, draft, period_start)


def _calendar_effect(state, plan, profile, connection=None, provenance=()):
    """The same finite calendar overlay, including explicit unique roster links."""
    from .school_calendar import _calendar,calendar_effect_for_plan
    from .school_experiment_builder import project_arduino_source_window
    from .scheduling_models import canonical
    try:
        source_effect=project_arduino_source_window(load_source_package(),plan,provenance)
    except (HTTPException,OSError,ValueError,TypeError,KeyError):
        source_effect={'effectiveInput':deepcopy(plan),'diagnostics':[],
            'blockers':[{'code':'source_rules_unavailable','message':'Исходные условия недоступны или повреждены; актуальность правил не подтверждена.','entityIds':[]}]}
    plan=source_effect['effectiveInput']
    data=_calendar(state,profile['schoolId'])
    names=dict(data.get('teacherBindings',{}))
    def read_names(c):
        exists=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='planning_config'").fetchone()
        row=c.execute('SELECT input_json FROM planning_config WHERE id=1').fetchone() if exists else None
        if row:
            for teacher in json.loads(row[0])['teachers']:
                if teacher['id'] not in names:names[teacher['id']]=teacher['name']
    if connection is not None:read_names(connection)
    elif db.database_path().is_file():
        with closing(sqlite3.connect(db.database_path().as_uri()+'?mode=ro',uri=True)) as c:read_names(c)
    aliases={}
    for identifier,name in names.items():
        matched=[teacher['id'] for teacher in plan['teachers'] if canonical(teacher['name'])==canonical(name)]
        if len(matched)==1:aliases[identifier]=matched[0]
    effect=calendar_effect_for_plan(state,plan,school_id=profile['schoolId'],
        allow_assumed_calendar=True,teacher_aliases=aliases,source_provenance=provenance)
    effect['blockers']=[*source_effect['blockers'],*effect['blockers']]
    effect['diagnostics']=[*source_effect['diagnostics'],*effect['diagnostics']]
    effect['ready']=effect['ready'] and not source_effect['blockers']
    effect['calendarMappingHash']=_hash(aliases)
    return effect


def calendar_current(state,item,profile,connection=None):
    from .scheduling_models import PlanningInput
    normalized=PlanningInput.model_validate(item['input']).model_dump()
    saved=item['public'].get('calendarBinding')
    if saved is None:
        from .school_calendar import _calendar
        from .agent_rules import binding as standing_rules_binding
        calendar=_calendar(state,profile['schoolId'])
        effect=_calendar_effect(state,item['input'],profile,connection,provenance=item['public'].get('provenance',[]))
        return standing_rules_binding(state,profile['schoolId']) is None and calendar['config'] is None and not any(calendar['records'].values()) and effect['ready'] and _hash(effect['effectiveInput'])==_hash(normalized)
    effect=_calendar_effect(state,item['input'],profile,connection,provenance=item['public'].get('provenance',[]))
    return effect['ready'] and _hash(effect['effectiveInput'])==_hash(normalized) and effect['calendarBinding']==saved and effect['calendarMappingHash']==item['public'].get('calendarMappingHash')


def assert_calendar_current(state,item,profile,connection=None):
    if not calendar_current(state,item,profile,connection):
        raise HTTPException(409,'Правила источников, календарь или привязка педагога изменились после расчёта. Нужен новый кандидат по актуальным ограничениям.')


def _seeded_input(plan, binding, calendar_binding=None):
    """A previous computation is only a hint, bound to this exact entire model.

    No source fact is inferred from the witness. We independently check every
    assignment before it becomes a computational baseline for the same solver.
    Changed documents, dates, assumptions or school grid never reuse it.
    """
    untouched = {'warmStartUsed':False}
    try:
        if SEED_PATH.stat().st_size > 1024 * 1024:
            return plan, untouched
        seed = json.loads(SEED_PATH.read_text())
        if calendar_binding is not None and (not isinstance(seed,dict) or seed.get('calendarBinding')!=calendar_binding):
            return plan, untouched
        # Only exact defaults may be omitted to match the two previous schema
        # versions. Actual duties, weights, membership or travel/rest rules stay
        # in every projection, so an old seed cannot bypass changed constraints.
        # Every accepted representation still needs independent validation below.
        representations = [deepcopy(plan)]
        for omit_timing, omit_routes in ((True, False), (False, True), (True, True)):
            legacy = deepcopy(plan)
            if omit_timing:
                for teacher in legacy.get('teachers', []):
                    if teacher.get('presenceEvents') == []:
                        teacher.pop('presenceEvents')
                for key, default in (('gapWeight', 10), ('spanWeight', 1), ('dayWeight', 0)):
                    if legacy.get('preferences', {}).get(key) == default:
                        legacy['preferences'].pop(key)
            if omit_routes:
                for entity in [*legacy.get('teachers', []), *legacy.get('groups', [])]:
                    for key, default in (('requiredBreaks', []), ('minTransitionMinutes', 0)):
                        if entity.get(key) == default:
                            entity.pop(key)
                for group in legacy.get('groups', []):
                    if group.get('attendanceUnitIds') == []:
                        group.pop('attendanceUnitIds')
                if legacy.get('roomTransitions') == []:
                    legacy.pop('roomTransitions')
            representations.append(legacy)
        accepted_hashes = {_hash(value) for value in representations}
        if (not isinstance(seed, dict) or seed.get('kind') != 'computed_experimental_warmstart' or seed.get('notSourceFact') is not True
                or seed.get('normativeActivation') is not False or seed.get('publicationAllowed') is not False
                or seed.get('periodStart') != plan['periodStart'] or seed.get('inputHash') not in accepted_hashes
                or seed.get('packageHash') != binding['packageHash'] or seed.get('draftHash') != binding['draftHash']
                or not isinstance(seed.get('assignments'),list)
                or len(seed['assignments']) != sum(demand['weeklyCount'] for demand in plan['demands'])
                or seed.get('assignmentHash') != _hash(seed['assignments'])):
            return plan, untouched
        if not validate_solution(plan, seed['assignments'])['valid']:
            return plan, untouched
        result = deepcopy(plan)
        result['baseline'] = deepcopy(seed['assignments'])
        return result, {'warmStartUsed':True,'inputHash':seed['inputHash'],
            'effectiveInputHash':_hash(plan), 'legacyDefaultsNormalized':seed['inputHash'] != _hash(plan),
            'assignmentHash':seed['assignmentHash'],'notSourceFact':True,'independentlyValidated':True}
    except (OSError, ValueError, TypeError, KeyError):
        return plan, untouched


def _public(item, current, state=None, profile=None):
    value = deepcopy(item['public'])
    calculation = value.get('calculation')
    if calculation and value['status'] in ACTIVE and calculation['startedAt']:
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(calculation['startedAt'].replace('Z','+00:00'))).total_seconds()
        calculation['elapsedSeconds'] = round(max(calculation['elapsedSeconds'], elapsed, 0), 3)
    if value['status'] in ACTIVE and not _live_owner(item):
        value.update(status='stale', message='Процесс расчёта прерван. Автоматический повтор не выполнялся; создайте новый экспериментальный черновик.')
    elif (value['status'] in ACTIVE and (future := _futures.get((str(db.database_path()), value['id']))) is not None
          and future.done() and (future.cancelled() or future.exception() is not None)):
        value.update(status='failed', message='Расчёт прерван до сохранения результата. Автоматический повтор не выполнялся; исходная сетка сохранена.')
    elif value['sourceBinding'] != current:
        value.update(computedStatus=value['status'], status='stale', message='Исходная сетка или документы изменились. Этот эксперимент относится к прежней версии источников.')
    if state is not None and profile is not None and not calendar_current(state,item,profile):
        value.update(computedStatus=value['status'],status='stale',message='Правила источников, календарь или датированные ограничения изменились. Требуется новый экспериментальный расчёт.')
    value['canPublish'] = False
    value['normativeActivation'] = False
    return value


def collection(profile):
    _actor(profile)
    state, draft = _snapshot(raw_draft=True)
    package = load_source_package()
    binding = _current(draft, package)
    stored = _stored(state, profile['schoolId'])
    values = [_public(item, binding, state, profile) for item in reversed(stored['candidates'])]
    summaries = [{k: value[k] for k in ('id', 'periodStart', 'status', 'message', 'createdAt', 'updatedAt', 'mode')} for value in values]
    return {'schoolId': profile['schoolId'], 'draftRevision': binding['draftRevision'],
            'packageHash': binding['packageHash'], 'latest': values[0] if values else None, 'candidates': summaries}


def read_experiment(experiment_id, profile):
    _actor(profile)
    state, draft = _snapshot(raw_draft=True)
    stored = _stored(state, profile['schoolId'])
    item = next((i for i in stored['candidates'] if i['public']['id'] == experiment_id), None)
    if item is None:
        raise HTTPException(404, 'Экспериментальный черновик не найден.')
    return _public(item, _current(draft, load_source_package()), state, profile)


def _save(state, stored):
    if len(json.dumps(stored, ensure_ascii=False).encode()) > MAX_BYTES:
        raise HTTPException(429, 'Достигнут предел хранения экспериментальных черновиков.')
    future = {**state, NAMESPACE: stored}
    if len(json.dumps(future, ensure_ascii=False).encode()) > db.MAX_SNAPSHOT_BYTES:
        raise HTTPException(429, 'Достигнут предел общего хранилища школы. Существующие версии сохранены.')
    state[NAMESPACE] = stored


@contextmanager
def _edit_bound(database):
    """A worker keeps its admitted DB even if a test/operator changes env later."""
    from pathlib import Path
    from .workspace import verify_school_state
    with closing(sqlite3.connect(Path(database).as_uri() + '?mode=rw', uri=True, timeout=15)) as connection:
        connection.execute('BEGIN IMMEDIATE')
        try:
            state = json.loads(connection.execute('SELECT payload FROM demo_state WHERE id=1').fetchone()[0])
            verify_school_state(state)
            yield state
            connection.execute('UPDATE demo_state SET payload=? WHERE id=1', (json.dumps(state, ensure_ascii=False),))
            connection.commit()
        except BaseException:
            connection.rollback()
            raise


def _projection(plan, assignments, provenance, period_start):
    groups = {i['id']: i for i in plan['groups']}
    teachers = {i['id']: i['name'] for i in plan['teachers']}
    rooms = {i['id']: i['name'] for i in plan['rooms']}
    slots = {i['id']: i for i in plan['slots']}
    demand_by_event = {f"{d['id']}_{n}": d for d in plan['demands'] for n in range(d['weeklyCount'])}
    source_by_demand = {p['demandId']: p.get('sourceCells', []) for p in provenance}
    lessons = []
    for assignment in assignments:
        demand = demand_by_event[assignment['eventId']]
        slot = slots[assignment['slotId']]
        participants = [groups[g] for g in demand['groupIds']]
        for class_name in sorted({g['className'] for g in participants}):
            lessons.append({'id': 'exp_l_' + _hash([assignment['eventId'], class_name])[:32],
                'eventId': assignment['eventId'], 'className': class_name,
                'date': (date.fromisoformat(period_start) + timedelta(days=slot['day'])).isoformat(),
                'day': slot['day'], 'slot': slot['slot'], 'start': slot['start'], 'end': slot['end'],
                'subject': demand['subject'], 'teachers': [teachers[t] for t in demand['teacherIds']],
                'rooms': [rooms[assignment['roomId']]], 'groupLabels': [g['name'] for g in participants if g['className'] == class_name],
                'sourceCells': source_by_demand.get(demand['id'], []), 'assumed': True})
    classes = sorted({g['className'] for g in groups.values()}, key=lambda s: (int(''.join(c for c in s if c.isdigit()) or 0), s))
    return {'lessons': lessons, 'issues': [], 'classNames': classes, 'weekDays': sorted({s['day'] for s in slots.values()}),
        'weekLabel': f'{period_start} — {(date.fromisoformat(period_start) + timedelta(days=5)).isoformat()}',
        'eventCount': len(assignments), 'classParticipationCount': len(lessons)}


def _heartbeat(database, school_id, experiment_id, expected_process, progress, started, stop):
    """A real worker lease, independent of browser polling and solver threads."""
    while not stop.wait(HEARTBEAT_SECONDS):
        try:
            with _edit_bound(database) as state:
                stored = _stored(state, school_id)
                item = next((row for row in stored['candidates'] if row['public']['id'] == experiment_id), None)
                if item is None or item['process'] != expected_process or item['public']['status'] != 'running':
                    return
                item['public']['calculation'].update(heartbeatAt=_utc(),
                    elapsedSeconds=round(max(0, time.monotonic()-started), 3), phase=progress['phase'])
                item['public']['updatedAt'] = _utc()
                _save(state, stored)
        except (OSError, sqlite3.Error, HTTPException, KeyError, ValueError):
            # A transient writer conflict never starts another calculation. A
            # missing heartbeat still expires through the existing lease guard.
            continue


def _run_bound(experiment_id, profile, expected_process, database):
    # All input snapshots are private to this namespace; no active plan reads or writes.
    with _edit_bound(database) as state:
        stored = _stored(state, profile['schoolId'])
        item = next(i for i in stored['candidates'] if i['public']['id'] == experiment_id)
        if item['public']['status'] != 'queued' or item['process'] != expected_process:
            return
        limit = item.get('timeLimitSeconds', 30)
        stamp = _utc()
        calculation = _calculation(limit)
        calculation.update(startedAt=stamp, heartbeatAt=stamp, phase='preparing')
        item['public'].update(status='running', message='Размещаю всю исходную нагрузку по рабочим предположениям. Это не опубликованное расписание.',
            calculation=calculation, updatedAt=stamp)
        plan, provenance = deepcopy(item['input']), deepcopy(item['public']['provenance'])
        binding = deepcopy(item['public']['sourceBinding'])
        calculation_mode=item.get('calculationMode','generate')
        from .school_calendar import _calendar
        calendar=_calendar(state,profile['schoolId'])
        seed_calendar_binding=item['public'].get('calendarBinding') if calendar['config'] is not None or any(calendar['records'].values()) else None
        _save(state, stored)
    started = time.monotonic()
    progress = {'phase':'preparing'}
    stop = threading.Event()
    heartbeat = threading.Thread(target=_heartbeat,
        args=(database, profile['schoolId'], experiment_id, expected_process, progress, started, stop),
        name='school-calculation-heartbeat', daemon=True)
    heartbeat.start()
    try:
        computed_input, computation = _seeded_input(plan, binding, seed_calendar_binding)
        options = {}
        if limit > 30:
            def report(value):
                progress['phase'] = value['phase']
            options = {'search_workers':8, 'progress_callback':report,
                       'candidate_validator':lambda assignments: not validate_school_schedule_rules(plan, assignments, provenance)}
        result = solve_plan(computed_input, mode=calculation_mode, time_limit=limit, **options)
        progress['phase'] = 'independent_validation'
        valid = validate_solution(plan, result['assignments']) if result['status'] in RESULTS else result.get('validation')
        if result['status'] in RESULTS and (not valid or not valid.get('valid')):
            raise ValueError('Independent experiment validation rejected candidate')
        if result['status'] in RESULTS and validate_school_schedule_rules(plan, result['assignments'], provenance):
            raise ValueError('Independent school-rule validation rejected candidate')
        projection = _projection(plan, result['assignments'], provenance, plan['periodStart']) if result['status'] in RESULTS else None
        status = result['status'] if result['status'] in RESULTS | {'unknown','infeasible'} else 'failed'
        message = (f"Экспериментальный черновик готов: {len(result['assignments'])} занятий для всех классов. Проверьте предположения. Согласование и публикация выполняются отдельно."
                   if status in RESULTS else f'Полный проверенный вариант не найден за {limit} секунд. Невозможность расписания не доказана; можно выбрать больший предел поиска.'
                   if status == 'unknown' else 'При выбранных предположениях условия несовместимы. Посмотрите причины расчёта.'
                   if status == 'infeasible' else 'Расчёт не выполнен; проверьте диагностику.')
        update = {'status': status, 'message': message, 'projection': projection, 'validation': valid,
                  'diagnostics': result.get('diagnostics', []), 'metrics': result.get('metrics', {}),
                  'computation':computation, 'updatedAt': _utc(),
                  'control':result.get('control'),'comparison':result.get('comparison')}
    except Exception:
        update = {'status': 'failed', 'message': 'Экспериментальный расчёт остановлен. Исходная сетка и опубликованные версии сохранены.', 'updatedAt': _utc()}
    finally:
        stop.set()
        heartbeat.join(timeout=0.2)
    calculation.update(elapsedSeconds=round(max(0, time.monotonic()-started), 3),
        heartbeatAt=_utc(), phase='completed' if update['status'] in RESULTS else update['status'])
    update['calculation'] = calculation
    with _edit_bound(database) as state:
        stored = _stored(state, profile['schoolId'])
        item = next(i for i in stored['candidates'] if i['public']['id'] == experiment_id)
        if item['process'] == expected_process and item['public']['status'] == 'running':
            if not calendar_current(state,item,profile):
                update.update(computedStatus=update['status'],status='stale',message='Календарь изменился во время расчёта; кандидат требует нового запуска.')
            item['public'].update(update)
            _save(state, stored)


def _run(experiment_id, profile, expected_process, database):
    from .school_requests import bound_school
    with bound_school(profile['schoolId']):
        _run_bound(experiment_id, profile, expected_process, database)


def create_experiment(body, profile):
    _actor(profile)
    if not isinstance(body, ExperimentalInput):
        body = ExperimentalInput.model_validate(body)
    actor = [profile['schoolId'], profile['userId'], profile['role']]
    experiment_id = 'exp_' + _hash([*actor, body.requestId])[:32]
    fingerprint = _hash(_command_payload(body))
    with db.edit_state() as state:
        _actor(profile)
        stored = _stored(state, profile['schoolId'])
        existing = next((i for i in stored['candidates'] if i['public']['id'] == experiment_id), None)
        if existing:
            if existing['commandHash'] != fingerprint:
                raise HTTPException(409, 'Идентификатор черновика уже использован с другими параметрами.')
            return deepcopy(existing['receipt'])
        from .dated_timetable import guard_saved_calendar
        guard_saved_calendar(state, profile['schoolId'], body.periodStart)
        if len(stored['candidates']) >= MAX_RUNS:
            raise HTTPException(429, 'Достигнут предел восьми экспериментальных черновиков. Существующие версии сохранены.')
        if any(_active(i) for i in stored['candidates']):
            raise HTTPException(409, 'Экспериментальный расчёт уже выполняется. Откройте его текущий статус.')
        _, draft = _snapshot(raw_draft=True)
        package = load_source_package()
        binding = _current(draft, package)
        if body.expectedDraftRevision != binding['draftRevision'] or body.expectedPackageHash != binding['packageHash']:
            raise HTTPException(409, 'Документы или исходная сетка изменились. Обновите их перед расчётом.')
        try:
            built = _build(package, _bounded_draft(draft), body.periodStart)
        except (ValueError, OverflowError):
            raise HTTPException(422, 'Не удалось подготовить эксперимент по этим исходным данным. Проверьте выбранную неделю и источники.') from None
        effect=_calendar_effect(state,built['input'],profile,provenance=built['provenance'])
        if not effect['ready']:
            raise HTTPException(409,{'message':'Эксперимент не запущен: проверьте календарь и привязки ресурсов.','issues':effect['blockers']})
        built['input']=effect['effectiveInput']
        represented = {cell for entry in built['provenance'] + built.get('deferredLoads', []) for cell in entry.get('sourceCells', [])}
        source_cells = {cell['sourceCell'] for cell in package['cells']}
        if (built['coverage']['sourceCells'] != len(package['cells']) or built['coverage']['coveredCells'] != len(package['cells'])
                or represented != source_cells):
            raise HTTPException(422, 'Эксперимент не охватил всю исходную нагрузку; результат не сохранён.')
        stamp = _utc()
        public = {'id': experiment_id, 'schoolId': profile['schoolId'], 'mode': 'working_assumptions',
            'periodStart': body.periodStart, 'status': 'queued', 'message': 'Экспериментальный расчёт поставлен в очередь. Предположения не считаются подтверждёнными школой.',
            'assumptions': built['assumptions'], 'coverage': built['coverage'], 'provenance': built['provenance'],
            'warnings': built.get('warnings', []), 'projection': None, 'validation': None, 'diagnostics': [],
            'deferredLoads': built.get('deferredLoads', []), 'metadata': built.get('metadata', {}),
            'sourceBinding': binding, 'calendarBinding':effect['calendarBinding'],
            'calendarMappingHash':effect['calendarMappingHash'], 'createdAt': stamp, 'updatedAt': stamp, 'canPublish': False, 'normativeActivation': False,
            'calculation':_calculation(body.timeLimitSeconds)}
        item = {'public': public, 'input': built['input'], 'commandHash': fingerprint,
                'receipt': deepcopy(public), 'process': PROCESS, 'processPid':os.getpid(),
                'processStart':_process_start(os.getpid()), 'actor': actor, 'timeLimitSeconds':body.timeLimitSeconds}
        stored['candidates'].append(item)
        _save(state, stored)
    database = str(db.database_path().resolve())
    with _lock:
        executor = _executors.get(database)
        if executor is None:
            executor = _executors[database] = ThreadPoolExecutor(max_workers=1, thread_name_prefix='school-experiment')
        try:
            _futures[(database, experiment_id)] = executor.submit(_run, experiment_id, deepcopy(profile), PROCESS, database)
        except Exception:
            with _edit_bound(database) as state:
                stored = _stored(state, profile['schoolId'])
                item = next(i for i in stored['candidates'] if i['public']['id'] == experiment_id)
                item['public'].update(status='failed', message='Не удалось запустить фоновый расчёт. Создайте новый экспериментальный запрос.', updatedAt=_utc())
                _save(state, stored)
            raise HTTPException(503, 'Не удалось запустить фоновый расчёт; исходная сетка сохранена.') from None
    return deepcopy(public)


def recalculate_rules(proposal, profile, task_id):
    """Rebuild one selected model under durable rules, preserving its baseline.

    The selected candidate is deliberately left immutable and becomes stale.
    Current rules are projected onto fresh source input, never baked-in copies
    of an old availability overlay that might later have been archived.
    """
    _actor(profile)
    from .agent_rules import assert_dispatch_context
    from .experimental_edits import get_version, _current_snapshot, _version_hash
    from .scheduling_models import PlanningInput
    binding = proposal['binding']
    target = binding['targetExperimentId']
    version = get_version(target, profile)
    manual = version['_snapshot']
    if manual['metadataOverrides'] or manual['addedEvents'] or manual['deletedEvents']:
        raise HTTPException(409, 'Правило сохранено; пересчёт версии с ручным изменением нагрузки или педагогов требует проверки состава занятий.')
    identifier = 'exp_' + _hash(['standing_rule', profile['schoolId'], profile['userId'], task_id])[:32]
    fingerprint = _hash([proposal['hash'], binding['expectedVersionHash']])
    with db.edit_state() as state:
        _actor(profile)
        stored = _stored(state, profile['schoolId'])
        existing = next((item for item in stored['candidates'] if item['public']['id'] == identifier), None)
        if existing:
            if existing['commandHash'] != fingerprint:
                raise HTTPException(409, 'Пересчёт правила уже связан с другими условиями.')
            return deepcopy(existing['receipt'])
        assert_dispatch_context(state, proposal, profile, task_id)
        selected = next((item for item in stored['candidates'] if item['public']['id'] == target), None)
        if selected is None:
            raise HTTPException(409, 'Правило сохранено; исходная версия пересчёта отсутствует.')
        revision, snapshot, _, _ = _current_snapshot(selected)
        if (revision != binding['expectedRevision'] or
                _version_hash(selected, revision, snapshot) != binding['expectedVersionHash']):
            raise HTTPException(409, 'Правило сохранено; выбранная версия изменилась перед запуском пересчёта.')
        _, draft = _snapshot(raw_draft=True)
        package = load_source_package()
        if _current(draft, package) != binding['targetSourceBinding']:
            raise HTTPException(409, 'Правило сохранено; исходные документы или сетка изменились.')
        if len(stored['candidates']) >= MAX_RUNS:
            raise HTTPException(429, 'Правило сохранено; достигнут предел экспериментальных вариантов.')
        if any(_active(item) for item in stored['candidates']):
            raise HTTPException(409, 'Правило сохранено; другой расчёт ещё выполняется.')
        from .dated_timetable import guard_saved_calendar
        guard_saved_calendar(state, profile['schoolId'], version['periodStart'])
        try:
            built = _build(package, _bounded_draft(draft), version['periodStart'])
        except (ValueError, OverflowError):
            raise HTTPException(409, 'Правило сохранено; исходная нагрузка требует проверки перед пересчётом.') from None
        represented = {cell for row in built['provenance'] + built.get('deferredLoads', []) for cell in row.get('sourceCells', [])}
        if (represented != {row['sourceCell'] for row in package['cells']} or
                built['coverage']['sourceCells'] != len(package['cells']) or
                built['coverage']['coveredCells'] != len(package['cells'])):
            raise HTTPException(409, 'Правило сохранено; новая модель не охватила всю исходную нагрузку.')
        plan = deepcopy(built['input'])
        plan['baseline'] = deepcopy(version['physicalAssignments'])
        plan['pins'] = deepcopy(manual['pins'])
        effect = _calendar_effect(state, plan, profile, provenance=built['provenance'])
        if not effect['ready']:
            raise HTTPException(409, {'message': 'Правило сохранено; пересчёт требует проверки ограничений.', 'issues': effect['blockers']})
        try:
            plan = PlanningInput.model_validate(effect['effectiveInput']).model_dump()
        except ValueError:
            raise HTTPException(409, 'Правило сохранено; состав занятий выбранной версии требует проверки.') from None
        public = {'id': identifier, 'schoolId': profile['schoolId'], 'mode': 'working_assumptions',
            'periodStart': plan['periodStart'], 'status': 'queued',
            'message': 'Постоянное правило сохранено; пересчитываю выбранную неделю с его учётом.',
            'assumptions': deepcopy(built['assumptions']), 'coverage': deepcopy(built['coverage']),
            'provenance': deepcopy(built['provenance']), 'deferredLoads': deepcopy(built.get('deferredLoads', [])),
            'metadata': {**deepcopy(built.get('metadata', {})), 'standingRule': proposal['hash'], 'normativeActivation': False},
            'warnings': deepcopy(built.get('warnings', [])), 'projection': None, 'validation': None, 'diagnostics': [],
            'sourceBinding': deepcopy(version['sourceBinding']), 'calendarBinding': effect['calendarBinding'],
            'calendarMappingHash': effect['calendarMappingHash'], 'createdAt': _utc(), 'updatedAt': _utc(),
            'canPublish': False, 'normativeActivation': False,
            'calculation': _calculation(DEFAULT_LONG_CALCULATION_SECONDS)}
        item = {'public': public, 'input': plan, 'commandHash': fingerprint, 'receipt': deepcopy(public),
            'process': PROCESS, 'processPid': os.getpid(), 'processStart': _process_start(os.getpid()),
            'actor': [profile['schoolId'], profile['userId'], profile['role']], 'calculationMode': 'repair',
            'timeLimitSeconds': DEFAULT_LONG_CALCULATION_SECONDS}
        stored['candidates'].append(item)
        _save(state, stored)
    database = str(db.database_path().resolve())
    with _lock:
        executor = _executors.get(database)
        if executor is None:
            executor = _executors[database] = ThreadPoolExecutor(max_workers=1, thread_name_prefix='school-experiment')
        try:
            _futures[(database, identifier)] = executor.submit(_run, identifier, deepcopy(profile), PROCESS, database)
        except Exception:
            with _edit_bound(database) as state:
                stored = _stored(state, profile['schoolId'])
                item = next(item for item in stored['candidates'] if item['public']['id'] == identifier)
                item['public'].update(status='failed', message='Правило сохранено; не удалось запустить фоновый пересчёт.', updatedAt=_utc())
                _save(state, stored)
            raise HTTPException(503, 'Правило сохранено; фоновый пересчёт не запущен.') from None
    return deepcopy(public)


def recalculate_absence(proposal,profile,task_id):
    """A new isolated candidate from the exact selected revision, never deletion."""
    _actor(profile)
    from .experimental_edits import get_version
    from .scheduling_models import PlanningInput
    absence=proposal['absence']
    version=get_version(absence['targetExperimentId'],profile)
    manual=version['_snapshot']
    if manual['metadataOverrides'] or manual['addedEvents'] or manual['deletedEvents']:
        raise HTTPException(409,'Выбранная версия содержит изменения нагрузки или преподавателей. Отсутствие сохранено; сначала явно подготовьте согласованный нормативный состав пересчёта.')
    if version['revision']!=absence['expectedRevision'] or version['versionHash']!=proposal['binding']['expectedVersionHash']:
        raise HTTPException(409,'Выбранная версия изменилась; отсутствие сохранено, но пересчёт требует нового запроса.')
    identifier='exp_'+_hash(['teacher_absence',profile['schoolId'],profile['userId'],task_id])[:32]
    fingerprint=_hash([proposal['hash'],version['versionHash']])
    with db.edit_state() as state:
        _actor(profile)
        stored=_stored(state,profile['schoolId'])
        existing=next((item for item in stored['candidates'] if item['public']['id']==identifier),None)
        if existing:
            if existing['commandHash']!=fingerprint:raise HTTPException(409,'Пересчёт уже связан с другими условиями')
            return deepcopy(existing['receipt'])
        from .experimental_edits import _current_snapshot,_version_hash
        selected=next((item for item in stored['candidates'] if item['public']['id']==absence['targetExperimentId']),None)
        if selected is None:raise HTTPException(409,'Исходная версия пересчёта отсутствует.')
        selected_revision,selected_snapshot,_,_=_current_snapshot(selected)
        if selected_revision!=absence['expectedRevision'] or _version_hash(selected,selected_revision,selected_snapshot)!=proposal['binding']['expectedVersionHash']:
            raise HTTPException(409,'Выбранная версия изменилась перед запуском пересчёта.')
        _,actual_draft=_snapshot(raw_draft=True)
        if _current(actual_draft,load_source_package())!=version['sourceBinding']:
            raise HTTPException(409,'Исходная сетка или документы изменились перед запуском пересчёта.')
        if len(stored['candidates'])>=MAX_RUNS:raise HTTPException(429,'Достигнут предел экспериментальных черновиков; отсутствие сохранено.')
        if any(_active(item) for item in stored['candidates']):raise HTTPException(409,'Решатель эксперимента занят; отсутствие сохранено.')
        from .dated_timetable import guard_saved_calendar
        guard_saved_calendar(state,profile['schoolId'],version['periodStart'])
        plan=deepcopy(version['originalInput']);plan['baseline']=deepcopy(version['physicalAssignments']);plan['pins']=deepcopy(manual['pins'])
        effect=_calendar_effect(state,plan,profile,provenance=version['originalProvenance'])
        if not effect['ready']:raise HTTPException(409,{'message':'Пересчёт требует уточнения календаря.','issues':effect['blockers']})
        plan=PlanningInput.model_validate(effect['effectiveInput']).model_dump()
        public={'id':identifier,'schoolId':profile['schoolId'],'mode':'working_assumptions','periodStart':plan['periodStart'],
            'status':'queued','message':'Отсутствие сохранено; пересчитываю всю нагрузку выбранной версии без удаления занятий.',
            'assumptions':deepcopy(version['assumptions']),'coverage':deepcopy(version['coverage']),
            'provenance':deepcopy(version['originalProvenance']),'deferredLoads':deepcopy(version['deferredLoads']),
            'metadata':{'mode':'working_assumptions','normativeActivation':False,'teacherAbsence':deepcopy(absence)},
            'warnings':[],'projection':None,'validation':None,'diagnostics':[],
            'sourceBinding':deepcopy(version['sourceBinding']),'calendarBinding':effect['calendarBinding'],
            'calendarMappingHash':effect['calendarMappingHash'],'createdAt':_utc(),'updatedAt':_utc(),
            'canPublish':False,'normativeActivation':False,
            'calculation':_calculation(DEFAULT_LONG_CALCULATION_SECONDS)}
        item={'public':public,'input':plan,'commandHash':fingerprint,'receipt':deepcopy(public),
              'process':PROCESS,'processPid':os.getpid(),'processStart':_process_start(os.getpid()),
              'actor':[profile['schoolId'],profile['userId'],profile['role']],'calculationMode':'repair',
              'timeLimitSeconds':DEFAULT_LONG_CALCULATION_SECONDS}
        stored['candidates'].append(item);_save(state,stored)
    database=str(db.database_path().resolve())
    with _lock:
        executor=_executors.get(database)
        if executor is None:executor=_executors[database]=ThreadPoolExecutor(max_workers=1,thread_name_prefix='school-experiment')
        _futures[(database,identifier)]=executor.submit(_run,identifier,deepcopy(profile),PROCESS,database)
    return deepcopy(public)


class SafeExperimentalRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def safe(request):
            try:
                return await original(request)
            except RequestValidationError:
                raise HTTPException(422, 'Некорректные параметры экспериментального черновика.') from None
        return safe


router = APIRouter(prefix='/experimental-school', tags=['experimental-school'], route_class=SafeExperimentalRoute)


@router.get('')
def http_collection(profile=Depends(require_roles('admin', 'director'))):
    return collection(profile)


@router.get('/{experiment_id}')
def http_read(experiment_id: str = APIPath(pattern=r'^exp_[a-f0-9]{32}$'), profile=Depends(require_roles('admin', 'director'))):
    return read_experiment(experiment_id, profile)


@router.post('')
def http_create(body: ExperimentalInput, profile=Depends(require_roles('admin', 'director'))):
    return create_experiment(body, profile)
