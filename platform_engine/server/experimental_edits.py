"""Versioned, isolated edits of a working-assumption school candidate.

The v21 source model remains immutable. A small sidecar keeps physical event
assignments and explicit corrections; GET never upgrades or writes old records.
Expanded one-event demands reuse the independent validator, with the original
aggregate constraints checked as well. This module never publishes a version.
"""
from contextlib import closing
from copy import deepcopy
from datetime import date, timedelta
import json
import re
import sqlite3
from typing import Annotated, Literal, Union

from fastapi import HTTPException
from pydantic import Field, ValidationError, model_validator

from . import db
from . import experimental_school as experiments
from .scheduling_models import Assignment, Identifier, PlanningInput, StrictModel, Subject, canonical
from .scheduling_solver import validate_solution

HISTORY_LIMIT = 20
RECEIPT_LIMIT = 128
AUDIT_LIMIT = 200
ISSUE_LIMIT = 256
NAMESPACE = experiments.NAMESPACE


class Move(StrictModel):
    kind: Literal['move']
    eventId: Identifier
    slotId: Identifier
    roomId: Identifier | None = None


class Swap(StrictModel):
    kind: Literal['swap']
    eventId: Identifier
    otherEventId: Identifier


class Room(StrictModel):
    kind: Literal['room']
    eventId: Identifier
    roomId: Identifier


class Pin(StrictModel):
    kind: Literal['pin']
    eventId: Identifier
    pinned: bool


class Metadata(StrictModel):
    kind: Literal['metadata']
    eventId: Identifier
    subject: Subject | None = None
    teacherIds: list[Identifier] | None = Field(default=None, min_length=1, max_length=8)

    @model_validator(mode='after')
    def changes(self):
        if self.subject is None and self.teacherIds is None:
            raise ValueError('Укажите предмет или преподавателей выбранного занятия')
        if self.teacherIds and len(self.teacherIds) != len(set(self.teacherIds)):
            raise ValueError('Преподаватель указан несколько раз')
        return self


class Add(StrictModel):
    kind: Literal['add']
    subject: Subject
    teacherIds: list[Identifier] = Field(min_length=1, max_length=8)
    groupIds: list[Identifier] = Field(min_length=1, max_length=200)
    slotId: Identifier
    roomId: Identifier
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode='after')
    def distinct(self):
        if len(self.teacherIds) != len(set(self.teacherIds)) or len(self.groupIds) != len(set(self.groupIds)):
            raise ValueError('Повторяются преподаватели или группы')
        return self


class Delete(StrictModel):
    kind: Literal['delete']
    eventId: Identifier
    reason: str = Field(min_length=1, max_length=500)


class Restore(StrictModel):
    kind: Literal['restore']
    targetRevision: int = Field(ge=1, le=2**31-1)


# StrictModel validates every field before parsing; a plain literal union is
# compatible with that shared input guard (Pydantic forbids it on discriminators).
Operation = Union[Move, Swap, Room, Pin, Metadata, Add, Delete, Restore]


class ExperimentalEditInput(StrictModel):
    expectedRevision: int = Field(ge=1, le=2**31-1)
    expectedVersionHash: str = Field(pattern=r'^[a-f0-9]{64}$')
    requestId: str = Field(min_length=16, max_length=80, pattern=r'^[A-Za-z0-9_-]+$')
    operations: list[Operation] = Field(min_length=1, max_length=100)
    previewHash: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')

    @model_validator(mode='after')
    def isolated_restore(self):
        if any(isinstance(op, Restore) for op in self.operations) and len(self.operations) != 1:
            raise ValueError('Восстановление версии выполняется отдельным действием')
        return self


def _hash(value):
    return experiments._hash(value)


def _actor(profile):
    # HTTP's trusted legacy bridge exposes planners as admin. Agent/publication
    # helpers also receive the canonical planner profile; verify the same current
    # membership before applying that adapter, never a new role or permission.
    from .request_planning import canonical_manager
    canonical_profile = canonical_manager(profile)
    adapted = deepcopy(canonical_profile)
    if adapted.get('staffRole') == 'planner' and adapted.get('role') == 'planner':
        adapted['role'] = 'admin'
    experiments._actor(adapted)
    return {key: canonical_profile.get(key) for key in ('schoolId', 'userId', 'role', 'membershipId')}


def _load(connection, experiment_id, profile):
    _actor(profile)
    row = connection.execute('SELECT payload FROM demo_state WHERE id=1').fetchone()
    if row is None:
        raise HTTPException(404, 'Экспериментальный черновик не найден')
    state = json.loads(row[0])
    stored = experiments._stored(state, profile['schoolId'])
    item = next((i for i in stored['candidates'] if i['public']['id'] == experiment_id), None)
    if item is None:
        raise HTTPException(404, 'Экспериментальный черновик не найден')
    return state, stored, item


def _source(item):
    public = item['public']
    result = {key: deepcopy(public.get(key)) for key in ('id', 'schoolId', 'periodStart', 'sourceBinding',
        'assumptions', 'coverage', 'provenance', 'deferredLoads', 'metadata', 'createdAt')}
    for key in ('calendarBinding','calendarMappingHash'):
        if key in public:result[key]=deepcopy(public[key])
    return result


def _base_events(plan):
    return {f"{d['id']}_{n}": {'demand': d, 'occurrence': n, 'sourceDemandId': d['id']}
            for d in plan['demands'] for n in range(d['weeklyCount'])}


def _legacy_snapshot(item):
    """Recover all shared placements from a complete saved v21 projection."""
    try:
        plan = PlanningInput.model_validate(item['input']).model_dump()
        projection = item['public']['projection']
        if item['public']['status'] not in experiments.RESULTS or not isinstance(projection, dict):
            raise HTTPException(409, 'Редактирование доступно после получения полного варианта')
        if item['public']['periodStart'] != plan['periodStart']:
            raise ValueError('Inconsistent period')
        slots = {(s['day'], s['slot'], s['start'], s['end']): s['id'] for s in plan['slots']}
        rooms = {r['name']: r['id'] for r in plan['rooms']}
        assignments = {}
        for row in projection['lessons']:
            if len(row['rooms']) != 1:
                raise ValueError('Ambiguous physical room')
            value = {'eventId': row['eventId'], 'slotId': slots[(row['day'], row['slot'], row['start'], row['end'])],
                     'roomId': rooms[row['rooms'][0]]}
            if value['eventId'] in assignments and assignments[value['eventId']] != value:
                raise ValueError('Shared event placement drift')
            assignments[value['eventId']] = value
        if set(assignments) != set(_base_events(plan)):
            raise ValueError('Missing or extra physical event')
        ordered = sorted(assignments.values(), key=lambda a: a['eventId'])
        expected = experiments._projection(plan, ordered, item['public']['provenance'], plan['periodStart'])
        keys = ('id', 'eventId', 'className', 'day', 'slot', 'start', 'end', 'subject', 'teachers', 'rooms', 'groupLabels', 'sourceCells')
        normal = lambda rows: sorted([{k: r[k] for k in keys} for r in rows], key=lambda r: r['id'])
        if normal(expected['lessons']) != normal(projection['lessons']) or projection['eventCount'] != len(ordered):
            raise ValueError('Shared projection or metadata drift')
        return {'assignments': ordered, 'pins': deepcopy(plan['pins']), 'metadataOverrides': {},
                'addedEvents': [], 'deletedEvents': {}, 'audit': []}
    except HTTPException:
        raise
    except (KeyError, TypeError, ValueError, ValidationError):
        raise HTTPException(409, 'Сохранённый вариант неполон или повреждён; исходные данные не изменены') from None


def _current_snapshot(item):
    sidecar = item.get('editState')
    if sidecar is None:
        return 1, _legacy_snapshot(item), [], []
    if (not isinstance(sidecar, dict) or sidecar.get('version') != 1
            or type(sidecar.get('revision')) is not int or not 2 <= sidecar['revision'] <= 2**31-1
            or not isinstance(sidecar.get('snapshot'), dict) or not isinstance(sidecar.get('history'), list)
            or len(sidecar['history']) > HISTORY_LIMIT or not isinstance(sidecar.get('receipts'), list)
            or len(sidecar['receipts']) > RECEIPT_LIMIT):
        raise HTTPException(409, 'История экспериментального черновика требует проверки')
    history = sidecar['history']
    if (any(not isinstance(e, dict) or type(e.get('revision')) is not int
            or not 1 <= e['revision'] < sidecar['revision'] or not isinstance(e.get('snapshot'), dict)
            or not re.fullmatch(r'[a-f0-9]{64}', e.get('versionHash', ''))
            or not isinstance(e.get('updatedAt'), str) or not isinstance(e.get('summary'), dict) for e in history)
            or len({e['revision'] for e in history}) != len(history)
            or any(not isinstance(r, dict) or not isinstance(r.get('requestId'), str)
                or not isinstance(r.get('actorBinding'), dict) or not isinstance(r.get('response'), dict)
                or not re.fullmatch(r'[a-f0-9]{64}', r.get('fingerprint', '')) for r in sidecar['receipts'])):
        raise HTTPException(409, 'История или квитанции экспериментального черновика повреждены')
    return sidecar['revision'], deepcopy(sidecar['snapshot']), sidecar['history'], sidecar['receipts']


def _version_hash(item, revision, snapshot):
    # v21 accepts a valid input with omitted defaults. Its normalized meaning
    # must hash identically to the same model returned by read_version.
    original = PlanningInput.model_validate(item['input']).model_dump()
    return _hash({'source': _source(item), 'originalInput': original, 'revision': revision, 'snapshot': snapshot})


def _expanded(original, snapshot, source):
    """Per-event metadata without changing original physical identities."""
    original = PlanningInput.model_validate(original).model_dump()
    if source['periodStart'] != original['periodStart']:
        raise ValueError('Inconsistent period')
    base = _base_events(original)
    required = {'assignments', 'pins', 'metadataOverrides', 'addedEvents', 'deletedEvents', 'audit'}
    if (set(snapshot) != required or not isinstance(snapshot['metadataOverrides'], dict) or not isinstance(snapshot['deletedEvents'], dict)
            or any(not isinstance(snapshot[field], list) for field in ('assignments','pins','addedEvents','audit'))
            or any(len(snapshot[field]) > 2000 for field in ('assignments','pins','addedEvents','deletedEvents','metadataOverrides'))
            or len(snapshot['audit']) > AUDIT_LIMIT):
        raise ValueError('Invalid edit snapshot')
    deleted = set(snapshot['deletedEvents'])
    if not deleted <= base.keys() | {entry['eventId'] for entry in snapshot['addedEvents']}:
        raise ValueError('Unknown removed event')
    events = deepcopy(base)
    subjects = {canonical(d['subject']) for d in original['demands']}
    teachers = {t['id'] for t in original['teachers']}
    groups = {g['id'] for g in original['groups']}
    rooms = {r['id'] for r in original['rooms']}
    slots = {s['id'] for s in original['slots']}
    original_demand_ids = {d['id'] for d in original['demands']}
    known_provenance = {p['demandId']: p for p in source['provenance']}
    for added in snapshot['addedEvents']:
        ident = added['eventId']
        if ident in events or not re.fullmatch(r'manual_[a-f0-9]{32}', ident):
            raise ValueError('Duplicate added event')
        proof = added.get('makeupSource')
        if proof is not None:
            if (not isinstance(proof,dict) or set(proof) != {'sourceDemandId','sourceCells','sourceRows','sourceConditions'}
                    or not isinstance(proof.get('sourceDemandId'),str)
                    or proof['sourceDemandId'] not in original_demand_ids):
                raise ValueError('Invalid makeup source proof')
            expected = known_provenance.get(proof['sourceDemandId'])
            if expected is None or any(not isinstance(proof[field],list)
                    or proof[field] != expected.get(field,[]) for field in ('sourceCells','sourceRows','sourceConditions')):
                raise ValueError('Makeup source proof differs from immutable course evidence')
        events[ident] = {'demand': added['demand'], 'occurrence': 0, 'sourceDemandId': None,
            'sourceProof': deepcopy(proof)}
    if not set(snapshot['metadataOverrides']) <= events.keys():
        raise ValueError('Unknown override')
    active = {key: value for key, value in events.items() if key not in deleted}
    if not 1 <= len(active) <= 2000:
        raise ValueError('A draft must retain a bounded nonempty event set')
    physical = [Assignment.model_validate(a).model_dump() for a in snapshot['assignments']]
    if len(physical) != len({a['eventId'] for a in physical}) or {a['eventId'] for a in physical} != active.keys():
        raise ValueError('Missing or duplicate event assignment')
    if any(a['slotId'] not in slots or a['roomId'] not in rooms for a in physical):
        raise ValueError('Unknown assignment resource')
    pins = [Assignment.model_validate(a).model_dump() for a in snapshot['pins']]
    if len(pins) != len({p['eventId'] for p in pins}) or any(p['eventId'] not in events for p in pins):
        raise ValueError('Invalid pin')
    mapping = {physical_id: 'edit_' + _hash(physical_id)[:40] + '_0' for physical_id in active}
    provenance = {p['demandId']: p.get('sourceCells', []) for p in source['provenance']}
    source_rows = {p['demandId']: p.get('sourceRows', []) for p in source['provenance']}
    source_conditions = {p['demandId']: p.get('sourceConditions', []) for p in source['provenance']}
    model = deepcopy(original)
    model.update(demands=[], baseline=[], pins=[], eventChains=[])
    effective_provenance = []
    for physical_id, event in active.items():
        demand = deepcopy(event['demand'])
        override = snapshot['metadataOverrides'].get(physical_id, {})
        if not set(override) <= {'teacherIds', 'subject'}:
            raise ValueError('Unexpected metadata override')
        demand.update(override)
        if canonical(demand['subject']) not in subjects or not set(demand['teacherIds']) <= teachers or not set(demand['groupIds']) <= groups:
            raise ValueError('Unknown subject, teacher or group')
        # Subject corrections never remove the old course's equipment rules.
        tags = set(demand.get('requiredTags', []))
        for template in original['demands']:
            if canonical(template['subject']) == canonical(demand['subject']):
                tags.update(template.get('requiredTags', []))
        demand['requiredTags'] = sorted(tags)
        demand['id'] = mapping[physical_id][:-2]
        demand['weeklyCount'] = 1
        demand.pop('teachingDays', None)
        source_ids = demand.get('sourceLessonIds', [])
        demand['sourceLessonIds'] = [source_ids[event['occurrence']]] if source_ids else []
        demand['syncKey'] = ('edit_sync_' + _hash([demand['syncKey'], event['occurrence']])[:40]) if demand.get('syncKey') else ''
        model['demands'].append(demand)
        proof = event.get('sourceProof')
        effective_provenance.append({'demandId': demand['id'], 'eventId': physical_id,
            'originalDemandId': proof['sourceDemandId'] if proof else event['sourceDemandId'],
            'sourceCells': deepcopy(proof['sourceCells'] if proof else provenance.get(event['sourceDemandId'], [])),
            'sourceRows':deepcopy(proof['sourceRows'] if proof else source_rows.get(event['sourceDemandId'],[])),
            'sourceConditions':deepcopy(proof['sourceConditions'] if proof else source_conditions.get(event['sourceDemandId'],[])),
            'manualAdded': event['sourceDemandId'] is None})
        event['effectiveDemand'] = demand
    internal = [{**a, 'eventId': mapping[a['eventId']]} for a in physical]
    # Deleted pinned events remain an explicit validation error below, not a
    # silently discarded constraint. All surviving pins/chains are rewritten.
    model['pins'] = [{**p, 'eventId': mapping[p['eventId']]} for p in pins if p['eventId'] in mapping]
    for chain in original['eventChains']:
        surviving = [mapping[e] for e in chain['eventIds'] if e in mapping]
        if len(surviving) >= 2:
            model['eventChains'].append({**chain, 'eventIds': surviving})
    model = PlanningInput.model_validate(model).model_dump()
    return model, internal, mapping, active, effective_provenance


def _raw_validation(original, snapshot, source, expanded):
    model, internal, mapping, active, _ = expanded
    result = validate_solution(model, internal)
    issues = deepcopy(result['issues'])
    from .school_schedule_rules import validate_school_schedule_rules
    issues.extend(validate_school_schedule_rules(model,internal,expanded[4]))
    by_event = {a['eventId']: a for a in snapshot['assignments']}
    slots = {s['id']: s for s in original['slots']}
    def add(code, message, ids):
        issues.append({'code': code, 'message': message, 'entityIds': list(ids)})
    # Flattening must not weaken the original weekly demand's daily cap.
    for demand in original['demands']:
        event_ids = [f"{demand['id']}_{n}" for n in range(demand['weeklyCount']) if f"{demand['id']}_{n}" in active]
        days = {}
        for event_id in event_ids:
            days.setdefault(slots[by_event[event_id]['slotId']]['day'], []).append(event_id)
        for entries in days.values():
            if len(entries) > demand['maxPerDay']:
                add('demand_daily', 'Превышено исходное дневное число занятий одной нагрузки.', entries)
        # A fully retained course keeps its confirmed distribution. Explicit
        # cancellation may leave a partial week; it must not make all the
        # remaining events impossible or silently create replacement lessons.
        if (demand.get('teachingDays') is not None and len(event_ids) == demand['weeklyCount']
                and len(days) != demand['teachingDays']):
            add('demand_teaching_days', 'Нарушено подтверждённое число учебных дней одной нагрузки.', event_ids)
    for chain in original['eventChains']:
        count = sum(e in active for e in chain['eventIds'])
        if 0 < count < len(chain['eventIds']):
            add('event_chain_removed', 'Удалена часть обязательной связки занятий.', chain['eventIds'])
    synced = {}
    for demand in original['demands']:
        if demand.get('syncKey'):
            for n in range(demand['weeklyCount']):
                synced.setdefault((demand['syncKey'], n), []).append(f"{demand['id']}_{n}")
    for entries in synced.values():
        if 0 < sum(e in active for e in entries) < len(entries):
            add('sync_removed', 'Удалена часть исходной синхронной группы занятий.', entries)
    for pin in snapshot['pins']:
        if pin['eventId'] not in active:
            add('pin', 'Удалено закреплённое занятие; сначала явно снимите закрепление.', [pin['eventId']])
    reverse = {value: key for key, value in mapping.items()}
    groups = {g['id']: g for g in original['groups']}
    base_ids = set(_base_events(original))
    public = []
    for issue in issues[:ISSUE_LIMIT]:
        identifiers = [reverse.get(i, i) for i in issue.get('entityIds', [])]
        event_ids = sorted({i for i in identifiers if i in active or i in base_ids})
        lesson_ids = sorted({'exp_l_' + _hash([ident, groups[g]['className']])[:32]
            for ident in event_ids if ident in active for g in active[ident]['effectiveDemand']['groupIds']})
        public.append({'code': issue['code'], 'message': issue['message'], 'severity': 'error',
                       'entityIds': identifiers[:2000], 'eventIds': event_ids, 'lessonIds': lesson_ids})
    if len(issues) > ISSUE_LIMIT:
        public.append({'code': 'issues_truncated', 'message': 'Список конфликтов сокращён; версия не может быть согласована.',
                       'severity': 'error', 'entityIds': [], 'eventIds': [], 'lessonIds': []})
    return {'valid': result['valid'] and not issues, 'issues': public}


def _canonical_projection(expanded, snapshot):
    model, assignments, mapping, events, provenance = expanded
    projection = experiments._projection(model, assignments, provenance, model['periodStart'])
    reverse = {internal: physical for physical, internal in mapping.items()}
    pins = {p['eventId'] for p in snapshot['pins']}
    for row in projection['lessons']:
        row['eventId'] = reverse[row['eventId']]
        row['id'] = 'exp_l_' + _hash([row['eventId'], row['className']])[:32]
        row['pinned'] = row['eventId'] in pins
    return projection


def _assemble(item, revision, snapshot, *, history=None):
    source = _source(item)
    original = PlanningInput.model_validate(item['input']).model_dump()
    expanded = _expanded(original, snapshot, source)
    model, assignments, mapping, events, provenance = expanded
    projection = _canonical_projection(expanded, snapshot)
    positions = {a['eventId']: a for a in snapshot['assignments']}
    pins = {p['eventId'] for p in snapshot['pins']}
    validation = _raw_validation(original, snapshot, source, expanded)
    coverage = deepcopy(source['coverage'])
    coverage.update(eventCount=len(events), currentEventCount=len(events), originalEventCount=len(_base_events(original)),
        manuallyAddedEventIds=[i['eventId'] for i in snapshot['addedEvents'] if i['eventId'] in events],
        manuallyDeletedEventIds=sorted(snapshot['deletedEvents']), manualLoadChanged=bool(snapshot['addedEvents'] or snapshot['deletedEvents']),
        sourceCoverageRetained=True)
    assumptions = deepcopy(source['assumptions'])
    if snapshot['audit']:
        assumptions.append({'id':'manual_experimental_corrections', 'category':'manual_corrections',
            'label':'Ручные изменения экспериментальной версии', 'value':{'revision':revision, 'operations':deepcopy(snapshot['audit'])},
            'basis':'Явные операции составителя; исправления не считаются подтверждением исходных школьных предположений.',
            'sourceCells':[], 'status':'working_assumption'})
    value = {'id':source['id'], 'experimentId':source['id'], 'schoolId':source['schoolId'], 'mode':'working_assumptions',
        'periodStart':source['periodStart'], 'revision':revision, 'versionHash':_version_hash(item, revision, snapshot),
        'input':model, 'assignments':assignments, 'physicalAssignments':deepcopy(snapshot['assignments']),
        'originalInput':original, 'eventIdMap':mapping, 'projection':projection, 'validation':validation,
        'assumptions':assumptions, 'provenance':provenance, 'originalProvenance':source['provenance'],
        'deferredLoads':source['deferredLoads'], 'coverage':coverage, 'sourceBinding':source['sourceBinding'],
        'manualCorrections':bool(snapshot['audit']), 'manualCorrectionAudit':deepcopy(snapshot['audit']),
        'eventDetails':[{'eventId':ident, 'subject':event['effectiveDemand']['subject'],
            'teacherIds':deepcopy(event['effectiveDemand']['teacherIds']), 'groupIds':deepcopy(event['effectiveDemand']['groupIds']),
            'slotId':positions[ident]['slotId'], 'roomId':positions[ident]['roomId'], 'pinned':ident in pins}
            for ident,event in sorted(events.items())],
        'pinnedEventIds':sorted(pins), 'approval':deepcopy(item.get('approval')), 'canEdit':True,
        'canUndo':any(entry['revision']<revision for entry in history or []),
        'historyLimit':HISTORY_LIMIT, 'history':deepcopy(history or []),
        '_source':source, '_snapshot':deepcopy(snapshot)}
    return value


def validate_version(version):
    if version.get('sourcesCurrent') is False:
        return {'valid':False,'issues':[{'code':'sources_changed','severity':'error','message':'Исходная сетка или документы изменились после расчёта. Нужен новый кандидат.','entityIds':[],'eventIds':[],'lessonIds':[]}]}
    if version.get('calendarCurrent') is False:
        return {'valid':False,'issues':[{'code':'calendar_changed','severity':'error','message':'Календарь изменился после расчёта; эта версия не подтверждена.','entityIds':[],'eventIds':[],'lessonIds':[]}]}
    """Canonical validation for manual preview, approval and publication."""
    try:
        item = {'input':version['originalInput'], 'public':version['_source']}
        if _version_hash(item, version['revision'], version['_snapshot']) != version['versionHash']:
            raise ValueError('Version changed')
        expanded = _expanded(version['originalInput'], version['_snapshot'], version['_source'])
        if _hash(expanded[0]) != _hash(version['input']) or _hash(expanded[1]) != _hash(version['assignments']):
            raise ValueError('Effective model changed')
        if _hash(_canonical_projection(expanded, version['_snapshot'])) != _hash(version['projection']):
            raise ValueError('Shared projection changed')
        return _raw_validation(version['originalInput'], version['_snapshot'], version['_source'], expanded)
    except (KeyError, TypeError, ValueError, ValidationError):
        return {'valid':False, 'issues':[{'code':'invalid_version', 'severity':'error',
            'message':'Версия или модель экспериментального расписания повреждена.', 'entityIds':[], 'eventIds':[], 'lessonIds':[]}]}


def _history_public(item, revision, snapshot, history):
    values = [{'revision':entry['revision'], 'versionHash':entry['versionHash'], 'updatedAt':entry['updatedAt'],
               'summary':deepcopy(entry['summary'])} for entry in history]
    values.append({'revision':revision, 'versionHash':_version_hash(item, revision, snapshot),
                   'updatedAt':item.get('editState', {}).get('updatedAt', item['public']['updatedAt']), 'summary':{'current':True}})
    return sorted(values, key=lambda i:i['revision'], reverse=True)


def read_version(connection, experiment_id, profile):
    """Read in a supplied publication transaction; no schema/init/GET writes."""
    state, _, item = _load(connection, experiment_id, profile)
    try:
        revision, snapshot, history, _ = _current_snapshot(item)
        result=_assemble(item, revision, snapshot, history=_history_public(item, revision, snapshot, history))
        try:
            experiments.assert_sources_current(connection,item['public']['sourceBinding'])
            result['sourcesCurrent']=True
        except HTTPException as error:
            if error.status_code!=409:raise
            result['sourcesCurrent']=False
        result['calendarCurrent']=experiments.calendar_current(state,item,profile,connection)
        result['canEdit']=result['sourcesCurrent'] and result['calendarCurrent']
        if not result['canEdit']:result['canUndo']=False
        if not result['sourcesCurrent']:
            result['validation']=validate_version(result)
        elif not result['calendarCurrent']:
            result['validation']=validate_version(result)
        return result
    except HTTPException:
        raise
    except (KeyError, TypeError, ValueError, ValidationError):
        raise HTTPException(409, 'Сохранённая версия экспериментального черновика требует проверки') from None


def get_version(experiment_id, profile):
    _actor(profile)
    path = db.database_path()
    if not path.is_file():
        raise HTTPException(404, 'Экспериментальный черновик не найден')
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=15)) as connection:
        connection.execute('BEGIN')
        return read_version(connection, experiment_id, profile)


def materialized_lessons(version):
    """Canonical shared class rows for immutable teacher/student publications."""
    if not validate_version(version)['valid']:
        raise HTTPException(422, 'Версия содержит обязательные конфликты и не может быть опубликована')
    expanded = _expanded(version['originalInput'], version['_snapshot'], version['_source'])
    _, _, _, events, _ = expanded
    groups = {g['id']:g for g in version['originalInput']['groups']}
    result = []
    for row in _canonical_projection(expanded, version['_snapshot'])['lessons']:
        event = events[row['eventId']]['effectiveDemand']
        result.append({key:deepcopy(row[key]) for key in ('id','className','day','slot','start','end','subject','teachers','rooms')})
        result[-1].update(_planningEventId=row['eventId'], _planningGroups=[{key:groups[g].get(key,'') for key in ('id','className','kind','partition')}
            for g in event['groupIds'] if groups[g]['className']==row['className']])
    return result


def _apply_operations(item, revision, snapshot, body):
    snapshot = deepcopy(snapshot)
    if isinstance(body.operations[0], Restore):
        target = body.operations[0].targetRevision
        history = item.get('editState', {}).get('history', [])
        found = next((entry for entry in history if entry['revision'] == target), None)
        if found is None:
            raise HTTPException(409, 'Версия для восстановления отсутствует в доступной истории')
        if found['versionHash'] != _version_hash(item, target, found['snapshot']):
            raise HTTPException(409, 'Снимок предыдущей версии повреждён; восстановление не выполнено')
        snapshot = deepcopy(found['snapshot'])
    else:
        model = PlanningInput.model_validate(item['input']).model_dump()
        slots, rooms = {s['id'] for s in model['slots']}, {r['id'] for r in model['rooms']}
        teachers, groups = {t['id'] for t in model['teachers']}, {g['id'] for g in model['groups']}
        known_subjects = {canonical(d['subject']) for d in model['demands']}
        positions = {a['eventId']:a for a in snapshot['assignments']}
        pins = {p['eventId']:p for p in snapshot['pins']}
        for index, op in enumerate(body.operations):
            removed_unpin = isinstance(op, Pin) and not op.pinned and op.eventId in pins
            if not isinstance(op, Add) and op.eventId not in positions and not removed_unpin:
                raise HTTPException(422, 'Занятие отсутствует в выбранной версии')
            if isinstance(op, Move):
                if op.slotId not in slots or (op.roomId is not None and op.roomId not in rooms):
                    raise HTTPException(422, 'Выберите известный слот и кабинет')
                positions[op.eventId]['slotId'] = op.slotId
                if op.roomId is not None:positions[op.eventId]['roomId'] = op.roomId
            elif isinstance(op, Swap):
                if op.otherEventId not in positions or op.eventId == op.otherEventId:
                    raise HTTPException(422, 'Для обмена нужны два разных существующих занятия')
                first, second = positions[op.eventId], positions[op.otherEventId]
                first['slotId'], second['slotId'] = second['slotId'], first['slotId']
            elif isinstance(op, Room):
                if op.roomId not in rooms:raise HTTPException(422, 'Кабинет отсутствует в справочнике')
                positions[op.eventId]['roomId'] = op.roomId
            elif isinstance(op, Pin):
                if op.pinned:pins[op.eventId] = deepcopy(positions[op.eventId])
                else:pins.pop(op.eventId, None)
            elif isinstance(op, Metadata):
                if (op.teacherIds is not None and not set(op.teacherIds) <= teachers) or (op.subject is not None and canonical(op.subject) not in known_subjects):
                    raise HTTPException(422, 'Используйте существующих преподавателей и предметы')
                snapshot['metadataOverrides'].setdefault(op.eventId, {}).update(op.model_dump(exclude_none=True, exclude={'kind','eventId'}))
            elif isinstance(op, Add):
                if (not set(op.teacherIds) <= teachers or not set(op.groupIds) <= groups or op.slotId not in slots
                        or op.roomId not in rooms or canonical(op.subject) not in known_subjects):
                    raise HTTPException(422, 'Дополнительный урок должен использовать существующие справочники')
                event_id = 'manual_' + _hash([body.requestId,index,revision,op.model_dump()])[:32]
                tags = sorted({tag for d in model['demands'] if canonical(d['subject'])==canonical(op.subject) for tag in d.get('requiredTags',[])})
                demand = {'id':'manual_template', 'subject':op.subject, 'teacherIds':op.teacherIds, 'groupIds':op.groupIds,
                    'weeklyCount':1, 'roomIds':sorted(rooms), 'requiredTags':tags, 'maxPerDay':1, 'allowedSlotIds':[]}
                from .lesson_makeups import trusted_addition_source
                proof = trusted_addition_source(op)
                added = {'eventId':event_id, 'demand':demand, 'reason':op.reason}
                if proof is not None:added['makeupSource'] = deepcopy(proof)
                snapshot['addedEvents'].append(added)
                positions[event_id] = {'eventId':event_id, 'slotId':op.slotId, 'roomId':op.roomId}
            elif isinstance(op, Delete):
                snapshot['deletedEvents'][op.eventId] = {'reason':op.reason, 'revision':revision+1}
                positions.pop(op.eventId)
        snapshot['assignments'] = sorted(positions.values(), key=lambda a:a['eventId'])
        snapshot['pins'] = sorted(pins.values(), key=lambda p:p['eventId'])
    snapshot['audit'] = [*snapshot['audit'], {'revision':revision+1, 'requestId':body.requestId,
        'operations':[op.model_dump(exclude_none=True) for op in body.operations]}][-AUDIT_LIMIT:]
    return snapshot


def _proposal(item, body, profile):
    revision, snapshot, history, _ = _current_snapshot(item)
    before = _assemble(item, revision, snapshot)
    if body.expectedRevision != revision or body.expectedVersionHash != before['versionHash']:
        raise HTTPException(409, 'Экспериментальный черновик изменился; обновите выбранную версию')
    if revision >= 2**31-1:raise HTTPException(409, 'Достигнут предел номера версии')
    changed = _apply_operations(item, revision, snapshot, body)
    try:
        after = _assemble(item, revision+1, changed)
    except (KeyError, TypeError, ValueError, ValidationError):
        raise HTTPException(422, 'Правки потеряли занятия или содержат неизвестные ссылки; версия не сохранена') from None
    first, second = ({r['id']:r for r in v['projection']['lessons']} for v in (before, after))
    changes = [{'lessonId':identity, 'before':first.get(identity), 'after':second.get(identity)}
               for identity in sorted(first.keys() | second.keys()) if first.get(identity) != second.get(identity)]
    normalized = body.model_dump(exclude={'previewHash'})
    preview_hash = _hash({'actor':_actor(profile), 'experimentId':before['id'], 'body':normalized,
                         'versionHash':after['versionHash'], 'changes':changes})
    summary = {'added':sum(c['before'] is None for c in changes), 'removed':sum(c['after'] is None for c in changes),
               'changed':sum(c['before'] is not None and c['after'] is not None for c in changes),
               'physicalEventCount':len(after['physicalAssignments'])}
    response = {'experimentId':before['id'], 'expectedRevision':revision, 'expectedVersionHash':before['versionHash'],
        'revision':revision+1, 'versionHash':after['versionHash'], 'previewHash':preview_hash, 'changes':changes,
        'validation':after['validation'], 'issues':after['validation']['issues'], 'coverage':after['coverage'], 'summary':summary}
    return response, changed, snapshot, history


def preview_edit(experiment_id, body, profile):
    body = ExperimentalEditInput.model_validate(body)
    _actor(profile)
    path = db.database_path()
    if not path.is_file():raise HTTPException(404, 'Экспериментальный черновик не найден')
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=15)) as connection:
        connection.execute('BEGIN')
        state, _, item = _load(connection, experiment_id, profile)
        experiments.assert_sources_current(connection,item['public']['sourceBinding'])
        experiments.assert_calendar_current(state,item,profile,connection)
        return _proposal(item, body, profile)[0]


def apply_edit(experiment_id, body, profile):
    body = ExperimentalEditInput.model_validate(body)
    actor = _actor(profile)
    if body.previewHash is None:raise HTTPException(422, 'Сначала проверьте точный предпросмотр правок')
    fingerprint = _hash({'experimentId':experiment_id, 'actor':actor, 'body':body.model_dump()})
    path = db.database_path()
    if not path.is_file():raise HTTPException(404, 'Экспериментальный черновик не найден')
    with closing(sqlite3.connect(path.as_uri()+'?mode=rw', uri=True, timeout=15)) as connection:
        connection.execute('BEGIN IMMEDIATE')
        try:
            state, stored, item = _load(connection, experiment_id, profile)
            revision, _, _, receipts = _current_snapshot(item)
            previous = next((r for r in receipts if r['requestId']==body.requestId and r['actorBinding']==actor), None)
            if previous:
                if previous['fingerprint'] != fingerprint:raise HTTPException(409, 'Идентификатор правки уже использован иначе')
                connection.rollback()
                return deepcopy(previous['response'])
            if len(receipts) >= RECEIPT_LIMIT:raise HTTPException(429, 'Достигнут предел квитанций правок; существующие версии сохранены')
            experiments.assert_sources_current(connection,item['public']['sourceBinding'])
            experiments.assert_calendar_current(state,item,profile,connection)
            preview, changed, old_snapshot, history = _proposal(item, body, profile)
            if body.previewHash != preview['previewHash']:raise HTTPException(409, 'Предпросмотр правок изменился; проверьте его заново')
            stamp = experiments._utc()
            old_hash = _version_hash(item, revision, old_snapshot)
            history = [*history, {'revision':revision, 'versionHash':old_hash, 'snapshot':old_snapshot,
                'updatedAt':item.get('editState', {}).get('updatedAt', item['public']['updatedAt']),
                'actorBinding':actor, 'summary':preview['summary']}][-HISTORY_LIMIT:]
            # Compact immutable receipts deliberately exclude the full GET view,
            # model, 1044 assumptions, projection, diff and validation issue list.
            response = {'experimentId':experiment_id, 'requestId':body.requestId, 'revision':revision+1,
                'versionHash':preview['versionHash'], 'previewHash':preview['previewHash'], 'updatedAt':stamp,
                'validationValid':preview['validation']['valid'], 'issueCount':len(preview['issues']), 'summary':preview['summary']}
            from .lesson_makeups import record_editor_change
            record_editor_change(state,profile['schoolId'],experiment_id,profile,
                _assemble(item,revision,old_snapshot),_assemble(item,revision+1,changed),body.operations)
            item['editState'] = {'version':1, 'revision':revision+1, 'snapshot':changed, 'history':history,
                'updatedAt':stamp, 'receipts':[*receipts, {'requestId':body.requestId, 'actorBinding':actor,
                    'fingerprint':fingerprint, 'response':deepcopy(response)}]}
            item['approval'] = None
            experiments._save(state, stored)
            connection.execute('UPDATE demo_state SET payload=? WHERE id=1', (json.dumps(state, ensure_ascii=False),))
            connection.commit()
            return response
        except BaseException:
            connection.rollback()
            raise
