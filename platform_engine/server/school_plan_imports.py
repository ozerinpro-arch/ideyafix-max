"""Transactional source import settings, separate from the mutable timetable."""
from contextlib import closing
from copy import deepcopy
import json

from fastapi import HTTPException
from pydantic import Field

from .db import table_exists


def calendar_hash(connection):
    """Bind manual source review to the exact persisted school calendar."""
    from .planning import _hash
    exists = table_exists(connection, 'demo_state')
    row = connection.execute('SELECT payload FROM demo_state WHERE id=1').fetchone() if exists else None
    calendar = (json.loads(row[0]) if row else {}).get('_schoolCalendar') or {}
    return _hash({key:calendar.get(key) for key in ('version','schoolId','revision','config','records')})


def source_readiness(connection, plan):
    exists = table_exists(connection, 'planning_source_settings')
    row = connection.execute('SELECT preview_json,resolutions_json FROM planning_source_settings WHERE id=1').fetchone() if exists else None
    if row is None:
        return []
    preview = json.loads(row[0])
    issues = []
    try:
        from .school_plan_sources import compile_source_plan, load_source_package
        from .scheduling_models import PlanningInput
        current = compile_source_plan(load_source_package(), _resolutions(json.loads(row[1]),preview), plan, plan['periodStart'])
        unresolved = current['unresolved']
        normalized = PlanningInput.model_validate(plan).model_dump(mode='json')
        normalized['confirmed'] = False
        if normalized != current['input']:
            issues.append({'code':'school_source_model_changed','message':'Сохранённая модель отличается от подтверждённых школьных источников. Восстановите связи и ограничения через «Школьные данные».','entityIds':[]})
        current_calendar = calendar_hash(connection)
        if current['resolvedConditions'] and (preview.get('calendarHash') != current_calendar
                or any(item.get('calendarHash') != current_calendar for item in current['resolvedConditions'])):
            issues.append({'code':'school_source_calendar_changed','message':'Календарь изменился после ручной проверки условий источников. Повторно проверьте их в «Школьных данных».','entityIds':[]})
    except (ValueError,TypeError,KeyError):
        return [{'code':'school_source_model_changed','message':'Связи школьных источников требуют повторной проверки. Откройте «Школьные данные».','entityIds':[]}]
    if unresolved:
        issues.append({'code':'school_sources_unresolved', 'message':f'В школьных источниках осталось уточнений: {len(unresolved)}. Откройте «Школьные данные».', 'entityIds':[]})
    return issues


def runtime_source_rules(connection, plan):
    """Apply only known source rules to the calendar's effective slots.

    Full source compilation belongs to the normative model. Recompiling here
    would restore demands deliberately removed by modules and finite dates.
    """
    exists = table_exists(connection, 'planning_source_settings')
    row = connection.execute('SELECT resolutions_json FROM planning_source_settings WHERE id=1').fetchone() if exists else None
    if row is None:
        return {'effectiveInput':deepcopy(plan),'ready':True,'blockers':[]}
    from .school_plan_sources import load_source_package
    from .school_source_rules import apply_source_rules
    try:
        applied = apply_source_rules(load_source_package(), json.loads(row[0]), plan, [])
        effective = applied['input']
        effective['confirmed'] = plan['confirmed']
        blockers = [{'code':item['code'],'message':item['message'],
                     'entityIds':item.get('demandIds',item.get('teacherIds',[]))}
                    for item in applied['unresolved'] if item['code'] == 'source_rule_empty_domain']
        return {'effectiveInput':effective,'ready':not blockers,'blockers':blockers}
    except (ValueError,TypeError,KeyError):
        return {'effectiveInput':deepcopy(plan),'ready':False,'blockers':[{
            'code':'school_source_runtime_changed','message':'Ограничения источников несовместимы с календарём выбранной недели. Повторите проверку.','entityIds':[]}]}


def runtime_source_provenance(connection, plan):
    """Recompile persisted mappings; an unmatched course gets no DO privilege.

    Source readiness remains a separate mandatory guard. This read adds no
    links from client fields and never changes the saved normative model.
    """
    exists = table_exists(connection, 'planning_source_settings')
    row = connection.execute('SELECT preview_json,resolutions_json FROM planning_source_settings WHERE id=1').fetchone() if exists else None
    if row is None:
        return []
    try:
        from .school_plan_sources import compile_source_plan, load_source_package
        from .scheduling_models import PlanningInput
        previous = json.loads(row[0])
        current = compile_source_plan(load_source_package(), _resolutions(json.loads(row[1]), previous), plan, plan['periodStart'])
        normalized = PlanningInput.model_validate(plan).model_dump(mode='json')
        normalized['confirmed'] = False
        if normalized != current['input']:
            return []
        return _source_links(current)
    except (ValueError, TypeError, KeyError):
        return []


def source_binding(connection):
    exists=table_exists(connection, 'planning_source_settings')
    row=connection.execute('SELECT revision FROM planning_source_settings WHERE id=1').fetchone() if exists else None
    if row is None:return None
    from .school_plan_sources import load_source_package
    from .planning import _hash
    return {'sourceRevision':row[0],'packageHash':_hash(load_source_package())}


def _source_links(previous):
    # A failed unit is absent from the new preview's successful provenance.
    # Keep its previously accepted ownership across partial imports; otherwise
    # a later shared import could add the same source hours a second time.
    previous = previous or {}
    accepted = {item['demandId']:item for item in previous.get('resolutions',{}).get('existingProvenance',[])}
    accepted.update({item['demandId']:item for item in previous.get('provenance',[])})
    return [{k:item[k] for k in ('demandId','sourceCells','subject','weeklyCount')}
        | {'teacherIds':item.get('teacherIds',[]), 'teacherOverrideApplied':item.get('teacherOverrideApplied',False)}
        for item in accepted.values()]


def _resolutions(value, previous):
    result = deepcopy(value)
    # Only server-persisted history is trusted; ignore client-supplied links.
    result['existingProvenance'] = _source_links(previous)
    return result


def _compile(package, resolutions, base, period):
    from .school_plan_sources import compile_source_plan
    try:
        return compile_source_plan(package,resolutions,base,period)
    except (ValueError,TypeError,KeyError):
        raise HTTPException(422,'Проверьте сопоставления: только известные классы, группы, педагоги, кабинеты и явные числовые часы. Сохранённые данные не изменены.') from None


def _actor(profile):
    from .planning import _actor as manager
    from .workspace import school_workspace
    from .school_requests import require_bound_school
    manager(profile)
    if not school_workspace() or profile.get('membershipVerified') is not True:
        raise HTTPException(403, 'Школьные источники доступны только подтверждённому составителю школы')
    from .request_planning import canonical_manager
    canonical_manager(profile)
    require_bound_school(profile.get('schoolId'))


def _saved(connection):
    row = connection.execute('SELECT * FROM planning_source_settings WHERE id=1').fetchone()
    if row is None:
        return {'revision':0, 'resolutions':{}, 'preview':None, 'packageId':None}
    return {'revision':row['revision'], 'packageId':row['package_id'], 'resolutions':json.loads(row['resolutions_json']), 'preview':json.loads(row['preview_json'])}


def read_sources(service, profile):
    from .school_plan_sources import load_source_package
    from .planning import _hash
    _actor(profile)
    with closing(service._connect()) as connection:
        package=load_source_package()
        saved, wrapper = _saved(connection), service._config(connection)
        from .school_plan_suggestions import source_mapping_suggestions
        draft = service._draft_snapshot(connection)
        suggestions = source_mapping_suggestions(package, wrapper['input'], saved['resolutions'],
            observed_classes=[row['className'] for row in draft['lessons']])
        suggestions['binding'] = {'configRevision':wrapper['revision'], 'draftRevision':draft['revision'], 'packageHash':_hash(package)}
        return {**saved, 'package':package,'packageHash':_hash(package),'calendarHash':calendar_hash(connection),
                'suggestions':suggestions}


def _guard_calendar(body, current, *, importing=False):
    conditions = body.resolutions.get('sourceConditions',{})
    if not isinstance(conditions,dict):
        raise HTTPException(422,'sourceConditions должен содержать объект подтверждений условий.')
    if ((body.expectedCalendarHash is not None or (importing and conditions))
            and body.expectedCalendarHash != current):
        raise HTTPException(409, {'code':'school_source_calendar_changed',
            'message':'Календарь изменился или не указан в ручной проверке источников. Обновите версию и повторите проверку условий.'})
    if importing and any(not isinstance(item,dict) or item.get('calendarHash') != current
            for item in conditions.values()):
        raise HTTPException(409, {'code':'school_source_calendar_changed',
            'message':'Ручная проверка каждого условия должна относиться к текущему календарю. Повторно подтвердите условия.'})


def preview_sources(service, body, profile):
    from .school_plan_sources import load_source_package, compile_source_plan
    from .planning import template_input, _hash
    _actor(profile)
    with closing(service._connect()) as connection:
        saved = _saved(connection)
        wrapper = service._config(connection)
        if (saved['revision'] != body.expectedSourceRevision or wrapper['revision'] != body.expectedConfigRevision
                or service._draft_revision(connection) != body.expectedDraftRevision):
            raise HTTPException(409, 'Источники, условия или черновик изменились. Обновите версии, сохранив ввод.')
        base = wrapper['input'] or template_input()['input']
        package=load_source_package()
        if body.expectedPackageHash is not None and body.expectedPackageHash!=_hash(package):
            raise HTTPException(409,'Пакет школьных источников обновлён. Обновите документы, сохранив ввод.')
        current_calendar = calendar_hash(connection)
        _guard_calendar(body,current_calendar)
        compiled = _compile(package, _resolutions(body.resolutions,saved['preview']), base, body.periodStart)
        from .school_plan_suggestions import source_mapping_suggestions
        draft = service._draft_snapshot(connection)
        suggestions = source_mapping_suggestions(package, base, body.resolutions,
            observed_classes=[row['className'] for row in draft['lessons']], unresolved=compiled['unresolved'])
        suggestions['binding'] = {'configRevision':wrapper['revision'], 'draftRevision':draft['revision'], 'packageHash':_hash(package)}
        return {**compiled, 'calendarHash':current_calendar, 'suggestions':suggestions}


def save_sources(service, body, profile):
    from .school_plan_sources import load_source_package, compile_source_plan
    from .planning import template_input, _json, _utc, _actor as manager, _hash
    _actor(profile)
    actor = manager(profile)
    fingerprint = _hash(body.model_dump())
    with service._locked(), closing(service._connect()) as connection:
        try:
            connection.execute('BEGIN IMMEDIATE')
            if body.requestId:
                receipt = connection.execute('SELECT fingerprint,result_json FROM planning_source_receipts WHERE request_id=? AND owner_id=? AND owner_role=?',(body.requestId,*actor)).fetchone()
                if receipt:
                    if receipt['fingerprint'] != fingerprint:
                        raise HTTPException(409,'Этот идентификатор импорта уже использован с другими сопоставлениями')
                    connection.commit()
                    return json.loads(receipt['result_json'])
            saved, wrapper = _saved(connection), service._config(connection)
            if (saved['revision'] != body.expectedSourceRevision or wrapper['revision'] != body.expectedConfigRevision
                    or service._draft_revision(connection) != body.expectedDraftRevision):
                raise HTTPException(409, 'Источники, условия или черновик изменились. Обновите версии, сохранив ввод.')
            if max(saved['revision'],wrapper['revision']) >= 2**31-1:
                raise HTTPException(409, 'Достигнут предел версий')
            package = load_source_package()
            if body.expectedPackageHash is not None and body.expectedPackageHash!=_hash(package):
                raise HTTPException(409,'Пакет школьных источников обновлён. Обновите документы, сохранив ввод.')
            current_calendar = calendar_hash(connection)
            _guard_calendar(body,current_calendar,importing=True)
            resolutions = _resolutions(body.resolutions,saved['preview'])
            preview = _compile(package, resolutions, wrapper['input'] or template_input()['input'], body.periodStart)
            preview['calendarHash'] = current_calendar
            preview['input']['confirmed'] = False
            source_ids = sorted(p['demandId'] for p in _source_links(preview))
            preview['sourceDemandIds'] = source_ids
            package_id = preview['packageId']
            connection.execute('INSERT INTO planning_source_settings VALUES(1,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,package_id=excluded.package_id,resolutions_json=excluded.resolutions_json,preview_json=excluded.preview_json,updated_at=excluded.updated_at',
                (saved['revision']+1,package_id,_json(preview['resolutions']),_json(preview),_utc()))
            connection.execute('INSERT INTO planning_config VALUES(1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,base_draft_revision=excluded.base_draft_revision,input_json=excluded.input_json,updated_at=excluded.updated_at',
                (wrapper['revision']+1,body.expectedDraftRevision,_json(preview['input']),_utc()))
            connection.execute("UPDATE planning_runs SET status='stale',message=?,updated_at=? WHERE status IN ('optimal','feasible')", ('Школьные источники изменились. Требуется новый расчёт.',_utc()))
            result = {**_saved(connection), 'calendarHash':current_calendar, 'config':service._wrapper(connection,profile)}
            if body.requestId:
                if connection.execute('SELECT COUNT(*) FROM planning_source_receipts').fetchone()[0]>=1000:
                    raise HTTPException(429,'Достигнут предел квитанций школьного импорта')
                connection.execute('INSERT INTO planning_source_receipts VALUES(?,?,?,?,?)',(body.requestId,*actor,fingerprint,_json(result)))
            connection.commit()
            return result
        except BaseException:
            connection.rollback()
            raise
