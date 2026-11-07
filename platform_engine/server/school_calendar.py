"""Versioned calendar in a separate domain JSON key; no timetable writes.

Router integration and planning/agent guards are deliberately owned by root.
Calendar readiness does not certify normative school facts or a solver result.
"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import sqlite3
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import ValidationError

from . import db
from .auth import session_profile
from .calendar_models import (CalendarConfig, ConfigWrite, MODELS, MAX_OCCURRENCES, MAX_REVISION,
                              PreviewInput, RecordWrite, ResolveDateInput, Revision, iso_date)
from .workspace import empty_school_state, school_workspace, verify_school_state

KEY = '_schoolCalendar'
KINDS = tuple(MODELS)
SERVER_FIELDS = {'id', 'archived', 'createdAt', 'updatedAt'}
MAX_RECORDS = 256
MAX_EVENTS = 4096


def _empty(school_id):
    return {'version': 1, 'schoolId': school_id, 'revision': 0, 'config': None,
            'records': {kind: [] for kind in KINDS}, 'history': []}


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _identity(profile):
    role = profile.get('staffRole', profile.get('role'))
    school_id, user_id = profile.get('schoolId'), profile.get('userId')
    if role not in {'director', 'planner', 'teacher'} or not isinstance(school_id, str) or not school_id or len(school_id) > 80 or not isinstance(user_id, str) or not user_id:
        raise HTTPException(403, 'Календарь требует проверенную школьную роль и школу')
    return school_id, role


def _reader(profile: dict = Depends(session_profile)):
    if not school_workspace():
        raise HTTPException(404, 'Раздел недоступен')
    _identity(profile)
    return profile


def _writer(profile: dict = Depends(_reader)):
    if _identity(profile)[1] not in {'director', 'planner'}:
        raise HTTPException(403, 'Календарь изменяет директор или составитель')
    return profile


def _read_state(connection=None):
    if connection is not None:
        exists = db.table_exists(connection, 'demo_state')
        row = connection.execute('SELECT payload FROM demo_state WHERE id=1').fetchone() if exists else None
        state = json.loads(row[0]) if row else empty_school_state()
    else:
        path = db.database_path()
        if not path.exists():
            return empty_school_state()
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as conn:
            exists = db.table_exists(conn, 'demo_state')
            row = conn.execute('SELECT payload FROM demo_state WHERE id=1').fetchone() if exists else None
        state = json.loads(row[0]) if row else empty_school_state()
    verify_school_state(state)
    return state


def _calendar(state, school_id):
    data = state.get(KEY)
    if data is None:
        return _empty(school_id)
    if not isinstance(data, dict) or data.get('version') != 1 or type(data.get('revision')) is not int or not 0 <= data['revision'] <= MAX_REVISION:
        raise HTTPException(409, 'Формат календаря повреждён; замена не выполнялась')
    if data.get('schoolId') != school_id:
        raise HTTPException(403, 'Календарь принадлежит другой школе')
    try:
        if data['config'] is not None:
            CalendarConfig.model_validate(data['config'])
        for kind in KINDS:
            for record in data['records'][kind]:
                MODELS[kind].model_validate({key: value for key, value in record.items() if key not in SERVER_FIELDS})
                if not isinstance(record['id'], str) or type(record['archived']) is not bool:
                    raise ValueError()
        if not isinstance(data['history'], list):
            raise ValueError()
        links=data.get('teacherBindings',{})
        receipts=data.get('absenceReceipts',[])
        if not isinstance(links,dict) or len(links)>200 or any(not isinstance(k,str) or not isinstance(v,str) or not 1<=len(k)<=160 or not 1<=len(v)<=160 for k,v in links.items()):raise ValueError()
        if not isinstance(receipts,list) or len(receipts)>128:raise ValueError()
        verified_links=set()
        from .teacher_absences import proposal_hash,digest
        for receipt in receipts:
            if not isinstance(receipt,dict) or set(receipt)!={'taskId','actor','hash','response','proposal'}:raise ValueError()
            proposal=receipt['proposal'];response=receipt['response'];summary=proposal['absence']
            if (proposal.get('kind')!='teacher_absence' or proposal_hash(proposal)!=receipt['hash']
                    or proposal.get('hash')!=receipt['hash'] or summary.get('hash')!=receipt['hash']
                    or response.get('absence')!=summary or receipt['actor'].get('schoolId')!=school_id
                    or proposal['binding'].get('schoolId')!=school_id
                    or response.get('recordId')!='absence_'+digest([receipt['actor'],receipt['taskId'],receipt['hash']])[:32]
                    or type(response.get('calendarRevision')) is not int or not 0<response['calendarRevision']<=data['revision']):raise ValueError()
            verified_links.add((summary['teacherId'],summary['teacherName']))
        if any((identifier,name) not in verified_links for identifier,name in links.items()):raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError, ValidationError):
        raise HTTPException(409, 'Формат календаря повреждён; замена не выполнялась') from None
    return deepcopy(data)


def _cas(data, revision):
    if data['revision'] != revision:
        raise HTTPException(409, 'Календарь уже изменился. Обновите версию, сохранив свой ввод.')
    if revision >= MAX_REVISION:
        raise HTTPException(409, 'Достигнут предел версий календаря')


def _parse(kind, value):
    if kind not in MODELS:
        raise HTTPException(404, 'Неизвестный вид календарной записи')
    try:
        return MODELS[kind].model_validate(value).model_dump()
    except (ValidationError, ValueError, OverflowError):
        raise HTTPException(422, 'Некорректные данные календаря') from None


def _found(data, kind, identity):
    if kind not in MODELS:
        raise HTTPException(404, 'Неизвестный вид календарной записи')
    record = next((row for row in data['records'][kind] if row['id'] == identity), None)
    if record is None:
        raise HTTPException(404, 'Запись календаря не найдена')
    return record


def _dates(start, end):
    current, last = iso_date(start), iso_date(end)
    while current <= last:
        yield current
        if current == last:
            break
        current += timedelta(days=1)


def _events(kind, record):
    if kind == 'weeks':
        return []
    if kind == 'days':
        return [{'date': record['date'], 'kind': record['status'], 'targetId': '', 'startTime': None, 'endTime': None,
                 'recordId': record.get('id'), 'confirmed': record['confirmed'], 'source': record['source']}]
    events = []
    for day in _dates(record['startDate'], record['endDate']):
        if kind == 'rules' and day.weekday() not in record['weekdays']:
            continue
        events.append({'date': day.isoformat(), 'kind': record['kind'], 'targetId': record['targetId'],
                       'startTime': record['startTime'], 'endTime': record['endTime'], 'recordId': record.get('id'),
                       'confirmed': record['confirmed'], 'source': record['source']})
        if len(events) > MAX_OCCURRENCES:
            raise HTTPException(422, 'Превышен предел повторений календаря')
    if not events:
        raise HTTPException(422, 'У правила нет вхождений в выбранном диапазоне')
    return events


def _validate_calendar(data):
    config = data['config']
    if config is None:
        # An explicitly approved absolute absence is independent of the still
        # unknown teaching calendar. It grants neither school days nor a zone.
        only_absences = (not any(data['records'][kind] for kind in ('weeks','days','rules'))
            and all(row['kind']=='teacher_unavailable' and row['confirmed'] for row in data['records']['exceptions']))
        if any(data['records'][kind] for kind in KINDS) and not only_absences:
            raise HTTPException(409, 'Сначала задайте период и часовой пояс')
    start, end = (config['periodStart'], config['periodEnd']) if config else (None,None)
    seen_weeks, grouped, count = set(), {}, 0
    for kind in KINDS:
        if len(data['records'][kind]) > MAX_RECORDS:
            raise HTTPException(409, 'Достигнут предел календарных записей')
        for record in data['records'][kind]:
            if record['archived']:
                continue
            first = record.get('weekStart', record.get('date', record.get('startDate')))
            last = (iso_date(record['weekStart']) + timedelta(days=6)).isoformat() if kind == 'weeks' else record.get('date', record.get('endDate'))
            if config and not start <= first <= last <= end:
                raise HTTPException(422, 'Календарная запись выходит за заданный период')
            if kind == 'weeks':
                if first in seen_weeks:
                    raise HTTPException(409, 'Неделя уже заведена')
                seen_weeks.add(first)
            for event in _events(kind, record):
                count += 1
                if count > MAX_EVENTS:
                    raise HTTPException(422, 'Превышен общий предел календарных вхождений')
                scope = event['kind'] if event['kind'].endswith('_unavailable') else 'school_day'
                key = (event['date'], scope, event['targetId'])
                grouped.setdefault(key, []).append((event['startTime'] or '00:00', event['endTime'] or '24:00'))
    for intervals in grouped.values():
        intervals.sort()
        previous_end = ''
        for begin, finish in intervals:
            if begin < previous_end:
                raise HTTPException(409, 'Пересекающиеся календарные записи: проверьте день, ресурс и время')
            previous_end = finish


def _commit(state, data, profile, action, identity=''):
    _validate_calendar(data)
    data['revision'] += 1
    stamp = datetime.now(timezone.utc).isoformat()
    data['history'].insert(0, {'id': uuid4().hex, 'at': stamp, 'actor': profile['userId'], 'action': action, 'recordId': identity})
    del data['history'][200:]
    state[KEY] = data


def _preview_hash(revision, record, identity=None):
    return _hash({'revision': revision, 'record': record, 'recordId': identity})


def _check_preview(data, kind, value, digest, identity=None):
    if kind == 'rules' and digest != _preview_hash(data['revision'], value, identity):
        raise HTTPException(409, 'Сначала просмотрите повторения для текущей версии календаря')


def _teacher_view(data, profile):
    if _identity(profile)[1] != 'teacher':
        return {**deepcopy(data), 'canWrite': True}
    refs = _own_calendar_ids(profile)
    result = {**deepcopy(data), 'canWrite': False, 'history': [], 'bindingReady': bool(refs),
              'bindingBlockers': [] if refs else [{'code': 'teacher_calendar_binding_required', 'message': 'Нужна явная привязка к ID преподавателя нормативной модели'}]}
    result.pop('absenceReceipts',None)
    result.pop('teacherBindings',None)
    # Teacher sees structural dates and own availability only, never sources,
    # notes, another teacher/room identity, audit actors or private declarations.
    if result['config']:
        result['config'] = {key: value for key, value in result['config'].items() if key not in {'source', 'note'}}
    for kind in KINDS:
        result['records'][kind] = [{key: value for key, value in row.items() if key not in {'source', 'note'}}
                                  for row in result['records'][kind]
                                  if kind in {'weeks', 'days'} or row['kind'] in {'school', 'closed'} or
                                  (row['kind'] == 'teacher_unavailable' and row['targetId'] in refs)]
    return result


def _own_calendar_ids(profile):
    # teacherRefs currently names timetable labels, not normative teacher IDs.
    # Never infer this identity from a display name or grant that fallback.
    refs = profile.get('calendarTeacherIds', [])
    if not isinstance(refs, list) or not all(isinstance(ref, str) and ref for ref in refs):
        raise HTTPException(403, 'Некорректная проверенная привязка календаря')
    return refs


def effective_calendar_snapshot(week_start: str, *, school_id: str, connection=None, state=None, teacher_ids=None, room_ids=None, teacher_aliases=None):
    """Root may consume this finite, hash-bound input inside a domain transaction.

    Unavailability refs require exact normative roster mapping. This function
    never mutates PlanningInput, draft, source files or any other domain key.
    """
    try:
        first = iso_date(week_start)
        if first.weekday() != 0:
            raise ValueError()
        last = first + timedelta(days=6)
    except (ValueError, OverflowError):
        raise HTTPException(422, 'Выберите понедельник недели') from None
    state = _read_state(connection) if state is None else state
    data = _calendar(state, school_id)
    config, blockers = data['config'], []
    def block(code, message):
        if not any(item['code'] == code for item in blockers):
            blockers.append({'code': code, 'message': message})
    if config is None:
        block('calendar_not_configured', 'Выберите период и часовой пояс календаря')
    elif not config['periodStart'] <= week_start <= last.isoformat() <= config['periodEnd']:
        block('week_outside_period', 'Неделя выходит за календарный период')
    if config and not config['confirmed']:
        block('calendar_unconfirmed', 'Подтвердите источник и параметры календаря')
    week = next((row for row in data['records']['weeks'] if not row['archived'] and row['weekStart'] == week_start), None)
    if week is None:
        block('week_missing', 'Задайте учебные дни выбранной недели')
    elif not week['confirmed']:
        block('week_unconfirmed', 'Учебные дни недели ещё не подтверждены')
    events = [event for kind in ('days', 'exceptions', 'rules') for row in data['records'][kind] if not row['archived']
              for event in _events(kind, row) if week_start <= event['date'] <= last.isoformat()]
    if any(not event['confirmed'] for event in events):
        block('records_unconfirmed', 'Есть неподтверждённые календарные исключения')
    days, unavailable = [], []
    for offset in range(7):
        current = (first + timedelta(days=offset)).isoformat()
        override = next((event for event in events if event['date'] == current and event['kind'] in {'school', 'closed'}), None)
        status = override['kind'] if override else ('school' if offset in week['schoolDays'] else 'closed') if week else 'unknown'
        days.append({'date': current, 'weekday': offset, 'status': status})
        if offset > 5 and status == 'school':
            block('unsupported_school_day', 'Решатель поддерживает учебные дни с понедельника по субботу; воскресенье требует отдельного правила')
    for event in events:
        if event['kind'] not in {'teacher_unavailable', 'room_unavailable'}:
            continue
        unavailable.append({key: event[key] for key in ('date', 'kind', 'targetId', 'startTime', 'endTime')})
        known = teacher_ids if event['kind'] == 'teacher_unavailable' else room_ids
        if known is None:
            block('resource_mapping_required', 'Перед расчётом сопоставьте ресурсы с подтверждённой нормативной моделью')
        elif event['targetId'] not in known and not (event['kind']=='teacher_unavailable' and (teacher_aliases or {}).get(event['targetId']) in known):
            block('unknown_resource', 'Календарный ресурс отсутствует в нормативной модели')
    if config and config['requiresTravel']:
        block('travel_not_supported', 'Переходы между корпусами ещё не проверяются решателем; расчёт заблокирован')
    payload = {'contractVersion': 1, 'schoolId': school_id, 'calendarRevision': data['revision'], 'weekStart': week_start,
               'timezone': config['timezone'] if config else None, 'config': config,
               'week': week, 'events': events, 'days': days, 'unavailable': unavailable,
               'teacherBindings':data.get('teacherBindings',{})}
    from .agent_rules import binding as standing_rules_binding
    rules_binding = standing_rules_binding(state, school_id)
    if rules_binding is not None:
        # Preserve all legacy hashes until a rule ledger actually exists. Its
        # revision also invalidates proposals when an archived rule has no
        # remaining effect on the selected week.
        payload['standingRules'] = rules_binding
    return {'contractVersion': 1, 'calendarRevision': data['revision'], 'weekStart': week_start,
            'timezone': payload['timezone'], 'weekType': week.get('type', '') if week else '',
            'bells': deepcopy(config.get('bells', [])) if config else [],
            'days': days, 'unavailable': unavailable, 'inputHash': _hash(payload),
            'readiness': {'ready': not blockers, 'blockers': blockers},
            'notice': 'Календарный снимок не применён к расписанию. Нормативные данные и результат требуют отдельной проверки.'}


def assert_calendar_snapshot_current(revision: int, input_hash: str, week_start: str, *, school_id: str, connection=None):
    """Call inside the same transaction as final apply (after receipt replay).

    Bind revision+hash+week in the run/proposal. A calendar change always makes
    that proposal stale, even when its old dates happen to produce equal days.
    """
    snapshot = effective_calendar_snapshot(week_start, school_id=school_id, connection=connection)
    if type(revision) is not int or snapshot['calendarRevision'] != revision or snapshot['inputHash'] != input_hash:
        raise HTTPException(409, 'Календарь изменился после подготовки предложения. Требуется новый снимок и согласование.')
    return snapshot


def calendar_effect_for_plan(state: dict, plan: dict, *, school_id: str, require_configured: bool = False, allow_assumed_calendar: bool = False, teacher_aliases=None, source_provenance=()):
    """Pure finite overlay for an already authorised normative weekly plan.

    Caller must honour ready=False; effectiveInput never grants confirmation or
    relaxes an existing rule. No SQL, checkpoint, model or timetable writes.
    """
    from .scheduling_models import PlanningInput, canonical
    try:
        value = PlanningInput.model_validate(plan).model_dump()
    except (ValidationError, ValueError):
        raise HTTPException(422, 'Нормативный план не соответствует проверяемой схеме') from None
    data = _calendar(state,school_id)
    aliases = dict(teacher_aliases or {})
    for identifier,name in data.get('teacherBindings',{}).items():
        matched=[row['id'] for row in value['teachers'] if canonical(row['name'])==canonical(name)]
        if len(matched)==1:aliases[identifier]=matched[0]
    snapshot = effective_calendar_snapshot(value['periodStart'], school_id=school_id, state=state,
        teacher_ids={row['id'] for row in value['teachers']}, room_ids={row['id'] for row in value['rooms']}, teacher_aliases=aliases)
    binding = {key: snapshot[key] for key in ('calendarRevision', 'inputHash', 'weekStart')}
    binding['schoolId'] = school_id
    configured = _calendar(state, school_id)['config'] is not None
    blockers = deepcopy(snapshot['readiness']['blockers']) if configured or require_configured or allow_assumed_calendar else []
    if allow_assumed_calendar and not configured:
        blockers=[item for item in blockers if item['code'] not in {'calendar_not_configured','week_missing'}]
    diagnostics = []
    def result(effective):
        return {'ready': not blockers, 'blockers': blockers, 'effectiveInput': effective,
                'calendarBinding': binding, 'diagnostics': diagnostics}
    def block(code, message, identities):
        blockers.append({'code': code, 'message': message, 'entityIds': identities})
    if blockers:
        return result(value)
    if any(demand['weekTypes'] for demand in value['demands']) and not snapshot['weekType']:
        block('week_type_required', 'Укажите подтверждённый тип выбранной недели для цикличной нагрузки.',
              [demand['id'] for demand in value['demands'] if demand['weekTypes']])
        return result(value)
    if configured or not allow_assumed_calendar:
        value['weekType'] = snapshot['weekType']
    # Confirmed bells replace slot intervals before availability is mapped.
    # A partial schedule cannot silently leave old slot times in effect.
    bells = {(bell['day'], bell['slot']): bell for bell in snapshot['bells']}
    if bells:
        missing = [slot['id'] for slot in value['slots'] if (slot['day'], slot['slot']) not in bells]
        if missing:
            block('bell_missing', 'В подтверждённом расписании звонков нет времени для части слотов.', missing)
            return result(value)
        for slot in value['slots']:
            bell = bells[(slot['day'], slot['slot'])]
            if (slot['start'], slot['end']) != (bell['start'], bell['end']):
                diagnostics.append({'code': 'calendar_bell_applied', 'message': 'Время слота взято из подтверждённых звонков.', 'entityIds': [slot['id']]})
            slot.update(start=bell['start'], end=bell['end'])
    first = iso_date(value['periodStart'])
    last = (first + timedelta(days=5)).isoformat()
    removed_events, retained = set(), []
    for demand in value['demands']:
        reason = None
        if demand['weekTypes'] and canonical(snapshot['weekType']) not in {canonical(kind) for kind in demand['weekTypes']}:
            reason = 'Нагрузка относится к другому типу недели.'
        elif ((demand['activeFrom'] and demand['activeFrom'] > last)
              or (demand['activeUntil'] and demand['activeUntil'] < value['periodStart'])):
            reason = 'Нагрузка не действует в датах выбранной недели.'
        if reason:
            removed_events.update(f"{demand['id']}_{occurrence}" for occurrence in range(demand['weeklyCount']))
            diagnostics.append({'code': 'inactive_demand', 'message': reason, 'entityIds': [demand['id']]})
            continue
        if demand['activeFrom'] or demand['activeUntil']:
            eligible = [slot for slot in value['slots'] if not demand['allowedSlotIds'] or slot['id'] in demand['allowedSlotIds']]
            active = [slot['id'] for slot in eligible
                      if (not demand['activeFrom'] or demand['activeFrom'] <= (first + timedelta(days=slot['day'])).isoformat())
                      and (not demand['activeUntil'] or (first + timedelta(days=slot['day'])).isoformat() <= demand['activeUntil'])]
            if not active:
                block('demand_no_active_slots', 'Для действующей нагрузки нет слотов в её точных датах. Уточните часы или календарь.', [demand['id']])
            elif len(active) != len(eligible):
                demand['allowedSlotIds'] = sorted(active)
                diagnostics.append({'code': 'partial_week_load', 'message': 'Слоты ограничены датами действия; заданное число часов сохранено без округления.', 'entityIds': [demand['id']]})
        retained.append(demand)
    for chain in value['eventChains']:
        removed = removed_events.intersection(chain['eventIds'])
        if removed and len(removed) != len(chain['eventIds']):
            block('partial_active_chain', 'В жёсткой связке активна только часть занятий. Уточните период всей связки.', [chain['id'], *sorted(removed)])
    if blockers:
        # Keep a valid original shape on failure; callers must honour ready=False.
        return result(PlanningInput.model_validate(plan).model_dump())
    value['demands'] = retained
    for key in ('baseline', 'pins'):
        excluded = [item['eventId'] for item in value[key] if item['eventId'] in removed_events]
        value[key] = [item for item in value[key] if item['eventId'] not in removed_events]
        if excluded:
            diagnostics.append({'code': 'inactive_' + key, 'message': 'Назначения неактивной нагрузки исключены из выбранной недели.', 'entityIds': excluded})
    excluded_chains = [chain['id'] for chain in value['eventChains'] if removed_events.intersection(chain['eventIds'])]
    value['eventChains'] = [chain for chain in value['eventChains'] if chain['id'] not in excluded_chains]
    if excluded_chains:
        diagnostics.append({'code': 'inactive_chains', 'message': 'Связки полностью неактивных занятий исключены из выбранной недели.', 'entityIds': excluded_chains})
    closed = {row['weekday'] for row in snapshot['days'] if row['status'] == 'closed'}
    closed_slots = {slot['id'] for slot in value['slots'] if slot['day'] in closed}
    for group in value['groups']:
        group['unavailableSlotIds'] = sorted(set(group['unavailableSlotIds']) | closed_slots)
    for unavailable in snapshot['unavailable']:
        collection = 'teachers' if unavailable['kind'] == 'teacher_unavailable' else 'rooms'
        target=unavailable['targetId']
        if collection=='teachers' and target not in {row['id'] for row in value['teachers']}:target=aliases.get(target)
        resource = next(row for row in value[collection] if row['id'] == target)
        weekday = iso_date(unavailable['date']).weekday()
        begin, end = unavailable['startTime'] or '00:00', unavailable['endTime'] or '24:00'
        if collection=='teachers' and any(event['day']==weekday and event['start']<end and begin<event['end'] for event in resource['presenceEvents']):
            block('absence_presence_overlap','Обязательное очное событие педагога пересекается с подтверждённым отсутствием. Уточните обязанность или отсутствие.',[resource['id']])
        affected = {slot['id'] for slot in value['slots'] if slot['day'] == weekday and slot['start'] < end and begin < slot['end']}
        resource['unavailableSlotIds'] = sorted(set(resource['unavailableSlotIds']) | affected)
    # Explicit school policy is an overlay, never a global/demo solver limit.
    from .school_schedule_rules import saturday_eligibility
    for teacher in value['teachers']:
        teacher['maxDaily']=min(10,teacher['maxDaily'])
    for group in value['groups']:
        if group['maxDaily'] is not None:group['maxDaily']=min(10,group['maxDaily'])
        else:group['maxDaily']=10
    provenance={entry['demandId']:entry for entry in source_provenance}
    for demand in value['demands']:
        if saturday_eligibility(demand,provenance.get(demand['id']))['allowed']:continue
        eligible=[s for s in value['slots'] if not demand['allowedSlotIds'] or s['id'] in demand['allowedSlotIds']]
        retained=[s['id'] for s in eligible if s['day']!=5]
        if len(retained)!=len(eligible):
            if not retained:
                block('school_saturday_category','В субботу разрешены только подтверждённые ДО или СПЕЦ; у этой нагрузки не осталось допустимых дней.',[demand['id']])
            else:demand['allowedSlotIds']=sorted(retained)
    # Validate the final shape independently of the overlay implementation.
    from .agent_rules import effect as standing_rules_effect
    standing = standing_rules_effect(state, value, school_id)
    blockers.extend(standing['blockers'])
    diagnostics.extend(standing['diagnostics'])
    effective = PlanningInput.model_validate(standing['effectiveInput']).model_dump()
    return result(effective)


def assert_calendar_binding_current(binding: dict, *, connection=None):
    """Planning applies this guard in its final write transaction after replay."""
    if (not isinstance(binding, dict) or set(binding) != {'calendarRevision', 'inputHash', 'weekStart', 'schoolId'}
            or not isinstance(binding.get('schoolId'), str) or not isinstance(binding.get('inputHash'), str)):
        raise HTTPException(409, 'Не сохранена проверенная привязка календаря')
    return assert_calendar_snapshot_current(binding['calendarRevision'], binding['inputHash'], binding['weekStart'],
        school_id=binding['schoolId'], connection=connection)


router = APIRouter(prefix='/api/school/calendar', tags=['school calendar'])


@router.get('')
def get_calendar(profile=Depends(_reader)):
    return _teacher_view(_calendar(_read_state(), _identity(profile)[0]), profile)


@router.patch('/config')
def save_config(body: ConfigWrite, profile=Depends(_writer)):
    with db.edit_state() as state:
        data = _calendar(state, _identity(profile)[0]); _cas(data, body.expectedRevision)
        data['config'] = body.config.model_dump()
        _commit(state, data, profile, 'config')
        return _teacher_view(data, profile)


@router.post('/records/{kind}')
def create_record(kind: str, body: RecordWrite, profile=Depends(_writer)):
    value = _parse(kind, body.record)
    with db.edit_state() as state:
        data = _calendar(state, _identity(profile)[0]); _cas(data, body.expectedRevision)
        _check_preview(data, kind, value, body.previewHash)
        stamp = datetime.now(timezone.utc).isoformat()
        record = {**value, 'id': uuid4().hex, 'archived': False, 'createdAt': stamp, 'updatedAt': stamp}
        data['records'][kind].append(record); _commit(state, data, profile, 'create:' + kind, record['id'])
        return {'revision': data['revision'], 'record': deepcopy(record)}


@router.patch('/records/{kind}/{identity}')
def update_record(kind: str, identity: str, body: RecordWrite, profile=Depends(_writer)):
    with db.edit_state() as state:
        data = _calendar(state, _identity(profile)[0]); _cas(data, body.expectedRevision)
        record = _found(data, kind, identity)
        if record['archived']:
            raise HTTPException(409, 'Сначала восстановите запись')
        if not body.record or set(body.record) & SERVER_FIELDS:
            raise HTTPException(422, 'Некорректные поля календарной записи')
        value = _parse(kind, {**{k: v for k, v in record.items() if k not in SERVER_FIELDS}, **body.record})
        _check_preview(data, kind, value, body.previewHash, identity)
        record.update(value, updatedAt=datetime.now(timezone.utc).isoformat())
        _commit(state, data, profile, 'edit:' + kind, identity)
        return {'revision': data['revision'], 'record': deepcopy(record)}


@router.post('/records/{kind}/{identity}/{action}')
def archive_record(kind: str, identity: str, action: str, body: Revision, profile=Depends(_writer)):
    if action not in {'archive', 'restore'}:
        raise HTTPException(404, 'Неизвестное действие')
    with db.edit_state() as state:
        data = _calendar(state, _identity(profile)[0]); _cas(data, body.expectedRevision)
        record = _found(data, kind, identity)
        archived = action == 'archive'
        if record['archived'] == archived:
            raise HTTPException(409, 'Запись уже находится в этом состоянии')
        if not archived:
            value = {k: v for k, v in record.items() if k not in SERVER_FIELDS}
            _check_preview(data, kind, value, body.previewHash, identity)
        record.update(archived=archived, updatedAt=datetime.now(timezone.utc).isoformat())
        _commit(state, data, profile, action + ':' + kind, identity)
        return {'revision': data['revision'], 'record': deepcopy(record)}


@router.post('/preview')
def preview_rule(body: PreviewInput, profile=Depends(_writer)):
    data = _calendar(_read_state(), _identity(profile)[0]); _cas(data, body.expectedRevision)
    value = body.record.model_dump()
    identity = body.recordId
    if identity:
        _found(data, 'rules', identity)
        data['records']['rules'] = [row for row in data['records']['rules'] if row['id'] != identity]
    events = _events('rules', value)
    candidate = {**value, 'id': identity or 'preview', 'archived': False}
    data['records']['rules'].append(candidate); _validate_calendar(data)
    return {'calendarRevision': data['revision'], 'occurrences': events, 'occurrenceCount': len(events),
            'previewHash': _preview_hash(data['revision'], value, identity)}


@router.get('/effective')
def get_effective(weekStart: str = Query(min_length=10, max_length=10), profile=Depends(_reader)):
    school_id, role = _identity(profile)
    snapshot = effective_calendar_snapshot(weekStart, school_id=school_id)
    if role == 'teacher':
        refs = _own_calendar_ids(profile)
        snapshot['unavailable'] = [row for row in snapshot['unavailable'] if row['kind'] == 'teacher_unavailable' and row['targetId'] in refs]
        snapshot['readiness'] = {'ready': False, 'blockers': [{'code': 'teacher_read_only', 'message': 'Личный просмотр календаря; расчёт доступен составителю'}]}
        if not refs:
            snapshot['readiness']['blockers'].append({'code': 'teacher_calendar_binding_required', 'message': 'Нужна явная привязка к ID преподавателя нормативной модели'})
    return snapshot


@router.post('/resolve-date')
def resolve_date(body: ResolveDateInput, profile=Depends(_reader)):
    data = _calendar(_read_state(), _identity(profile)[0])
    if not data['config']:
        raise HTTPException(409, 'Сначала выберите часовой пояс и календарный период')
    try:
        stamp = datetime.fromisoformat(body.messageTimestamp.replace('Z', '+00:00'))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError()
        expression = body.expression.casefold()
        if expression in {'today', 'сегодня', 'tomorrow', 'завтра'}:
            resolved = stamp.astimezone(ZoneInfo(data['config']['timezone'])).date()
            if expression in {'tomorrow', 'завтра'}:
                resolved += timedelta(days=1)
        else:
            resolved = iso_date(body.expression)
    except (ValueError, OverflowError):
        raise HTTPException(422, 'Укажите исходный timestamp с часовым поясом и поддержанную дату') from None
    value = resolved.isoformat()
    return {'date': value, 'messageTimestamp': body.messageTimestamp, 'timezone': data['config']['timezone'],
            'calendarRevision': data['revision'], 'withinPeriod': data['config']['periodStart'] <= value <= data['config']['periodEnd']}
