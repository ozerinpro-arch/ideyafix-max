"""School-local memory of explicit physical lesson cancellations and makeups.

No absence silently cancels a lesson. The editor calls record_editor_change in
its own transaction. One shared event creates one debt. Planning a makeup never
asserts that it was conducted. A solver suggestion is read-only until approval.
"""
from contextvars import ContextVar
from copy import deepcopy
from datetime import date, timedelta
import hashlib
import json
import re

from fastapi import APIRouter, Depends, HTTPException, Path as APIPath
from pydantic import Field, ValidationError
from . import db
from .auth import session_profile
from .scheduling_models import StrictModel

KEY = '_lessonMakeups'
MAX_RECORDS = 2000
MAX_RECEIPTS = 256
_context = ContextVar('lesson_makeup_commit', default=None)
from .experimental_school import SafeExperimentalRoute
router = APIRouter(tags=['cancelled lessons'],route_class=SafeExperimentalRoute)


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def _actor(profile):
    from .experimental_edits import _actor as manager
    return manager(profile)


def _ledger(state, school):
    value = state.get(KEY)
    if value is None:
        return {'version':1,'schoolId':school,'revision':0,'records':[],'receipts':[]}
    if (not isinstance(value,dict) or value.get('version')!=1 or value.get('schoolId')!=school
            or type(value.get('revision'))is not int or not 0<=value['revision']<2**31
            or not isinstance(value.get('records'),list) or len(value['records'])>MAX_RECORDS
            or not isinstance(value.get('receipts'),list) or len(value['receipts'])>MAX_RECEIPTS):
        raise HTTPException(409,'Реестр отмен требует проверки; записи не изменены')
    ids=set()
    for record in value['records']:
        if (not isinstance(record,dict) or not re.fullmatch(r'cancel_[a-f0-9]{32}',record.get('id',''))
                or record['id']in ids or record.get('status')not in {'pending','scheduled','rescinded'}
                or not isinstance(record.get('origin'),dict)or not isinstance(record.get('demand'),dict)
                or not isinstance(record.get('subject'),str)):
            raise HTTPException(409,'Реестр отмен повреждён; записи не изменены')
        ids.add(record['id'])
    if len(json.dumps(value,ensure_ascii=False).encode())>6*1024*1024:
        raise HTTPException(409,'Реестр отмен превысил допустимый объём')
    return deepcopy(value)


def _details(version):
    return {event['eventId']:event for event in version['eventDetails']}


def _when(version,event):
    slot=next(s for s in version['input']['slots']if s['id']==event['slotId'])
    return (date.fromisoformat(version['periodStart'])+timedelta(days=slot['day'])).isoformat()


def _course(record,event):
    return (record['subject']==event['subject'] and sorted(record['demand']['teacherIds'])==sorted(event['teacherIds'])
            and sorted(record['demand']['groupIds'])==sorted(event['groupIds']))


def _source_course(version,event_id,demand):
    proof=next((p for p in version['provenance']if p.get('eventId')==event_id),{})
    source_id=proof.get('originalDemandId')
    original=next((d for d in version['originalInput']['demands']if d['id']==source_id),demand)
    chains=[c['eventIds']for c in version['originalInput'].get('eventChains',[])if event_id in c['eventIds']]
    return {'sourceDemandId':source_id,'sourceCells':deepcopy(proof.get('sourceCells',[])),
        'sourceRows':deepcopy(proof.get('sourceRows',[])),'sourceConditions':deepcopy(proof.get('sourceConditions',[])),
        'maxPerDay':original['maxPerDay'],'syncKey':original.get('syncKey',''),'chains':chains}


def trusted_addition_source(operation):
    """Server-only scope, never an extra field accepted from an Add request."""
    context=_context.get()
    op=operation.model_dump()if hasattr(operation,'model_dump')else operation
    if not context or context.get('operation')!=op or not context.get('trustedRecord'):
        return None
    course=context['trustedRecord']['course']
    if course.get('sourceDemandId')is None:return None
    return {key:deepcopy(course.get(key,[] if key!='sourceDemandId' else None))
            for key in ('sourceCells','sourceRows','sourceConditions','sourceDemandId')}


def _course_counts(record,version,ledger):
    """Original siblings and linked makeups share the original daily ceiling."""
    course=record.get('course',{})
    source_id=course.get('sourceDemandId')
    ids={p['eventId']for p in version['provenance']if source_id is not None and p.get('originalDemandId')==source_id}
    for other in ledger['records']:
        same_source=(source_id is not None and other.get('course',{}).get('sourceDemandId')==source_id)
        same_manual=(source_id is None and other['origin']['eventId']==record['origin']['eventId']
                     and other['origin']['experimentId']==record['origin']['experimentId'])
        if same_source or same_manual:
            ids.update(link['eventId']for link in other.get('makeupLinks',[])if link['experimentId']==version['id'])
    slots={s['id']:s for s in version['input']['slots']};counts={day:0 for day in range(6)}
    for event in version['eventDetails']:
        if event['eventId']in ids:counts[slots[event['slotId']]['day']]+=1
    return counts


def _check_scheduled(record,version,ledger):
    planned=record.get('planned')
    if not planned or planned['experimentId']!=version['id']:return
    event=_details(version).get(planned['eventId'])
    if event is None:return
    if _when(version,event)<=record['origin']['date']:
        raise HTTPException(409,'Допзанятие должно идти после отменённого занятия')
    demand=record['demand']
    if demand.get('allowedSlotIds') and event['slotId']not in demand['allowedSlotIds']:
        raise HTTPException(409,'Перенос допзанятия нарушает исходные допустимые дни курса')
    if demand.get('roomIds') and event['roomId']not in demand['roomIds']:
        raise HTTPException(409,'Кабинет допзанятия не разрешён исходным курсом')
    limit=record.get('course',{}).get('maxPerDay',demand['maxPerDay'])
    if any(count>limit for count in _course_counts(record,version,ledger).values()):
        raise HTTPException(409,'Превышено исходное дневное число занятий этой нагрузки с учётом допзанятий')


def record_editor_change(state, school, experiment_id, profile, before, after, operations):
    """Called inside the existing BEGIN IMMEDIATE, before the editor commits."""
    actor=_actor(profile)
    ledger=_ledger(state,school)
    old,new=_details(before),_details(after)
    changed=False
    initial_status={r['id']:r['status']for r in ledger['records']}
    # Cancelling a scheduled makeup restores the same debt; it is not a second
    # cancellation of the original course. Moving it retains the link.
    for record in ledger['records']:
        planned=record.get('planned')
        links=record.setdefault('makeupLinks',deepcopy([planned]if planned else []))
        active=[link for link in links if link['experimentId']==experiment_id and link['eventId']in new and _course(record,new[link['eventId']])]
        if len(active)>1:raise HTTPException(409,'Восстановление создаёт несколько компенсаций одной отмены')
        origin_here=record['origin']['experimentId']==experiment_id
        if origin_here and record['origin']['eventId']in new:
            if active or planned and planned['experimentId']!=experiment_id and record['status']=='scheduled':
                raise HTTPException(409,'Сначала отмените запланированную компенсацию, затем восстановите исходное занятие')
            record.update(status='rescinded',planned=None);changed=True
        elif active:
            if record['status']=='scheduled' and planned and planned['experimentId']!=experiment_id:
                raise HTTPException(409,'Компенсация уже запланирована в другом варианте. Сначала отмените её, затем восстановите прежнюю версию.')
            link=deepcopy(active[0]);link.update(date=_when(after,new[link['eventId']]),revision=after['revision'],versionHash=after['versionHash'])
            record.update(status='scheduled',planned=link);changed=True
        elif planned and planned['experimentId']==experiment_id or origin_here and record['status']=='rescinded':
            record.update(status='pending',planned=None);changed=True
    planned_ids={link['eventId']for record in ledger['records']for link in record.get('makeupLinks',[])if link['experimentId']==experiment_id}
    for operation in operations:
        op=operation.model_dump(exclude_none=True)if hasattr(operation,'model_dump')else operation
        if op.get('kind')!='delete' or op['eventId']in planned_ids:
            continue
        event=old.get(op['eventId'])
        if event is None or op['eventId']in new:
            raise HTTPException(409,'Отменяемое занятие изменилось')
        identity='cancel_'+digest([school,experiment_id,op['eventId']])[:32]
        prior=next((r for r in ledger['records']if r['id']==identity),None)
        if prior and initial_status[identity]!='rescinded':
            raise HTTPException(409,'Эта отмена уже учтена')
        old_links=deepcopy(prior.get('makeupLinks',[]))if prior else []
        if prior:ledger['records'].remove(prior)
        if len(ledger['records'])>=MAX_RECORDS:
            raise HTTPException(429,'Достигнут предел реестра отмен')
        internal=before['eventIdMap'][event['eventId']]
        demand=next(d for d in before['input']['demands']if d['id']+'_0'==internal)
        def names(kind,ids):return [(r.get('className',r['id']) if kind=='groups' else r.get('name',r['id']))for r in before['input'][kind]if r['id']in ids]
        ledger['records'].append({'id':identity,'status':'pending','subject':event['subject'],
            'teacherNames':names('teachers',event['teacherIds']),'classNames':sorted(set(names('groups',event['groupIds']))),
            'demand':deepcopy(demand),'resources':{kind:[deepcopy(x)for x in before['input'][kind]if x['id']in demand[field]]for kind,field in [('teachers','teacherIds'),('groups','groupIds')]},
            'course':_source_course(before,event['eventId'],demand),'makeupLinks':old_links,
            'slots':deepcopy(before['input']['slots']),'origin':{'experimentId':experiment_id,'eventId':event['eventId'],
            'date':_when(before,event),'revision':before['revision'],'versionHash':before['versionHash'],
            'sourceBinding':deepcopy(before['sourceBinding'])},'reason':op['reason'],'createdBy':actor,
            'planned':None,'academicLessons':1})
        changed=True
    context=_context.get()
    if context:
        if ledger['revision']!=context['expectedLedgerRevision']:
            raise HTTPException(409,'Реестр отмен изменился; проверьте предложение повторно')
        record=next((r for r in ledger['records']if r['id']==context['recordId']),None)
        additions=[event for key,event in new.items()if key not in old]
        if record is None or record['status']!='pending' or len(additions)!=1 or not _course(record,additions[0]):
            raise HTTPException(409,'Компенсация не соответствует ожидающей отмене')
        event=additions[0]
        if _when(after,event)<=record['origin']['date']:
            raise HTTPException(409,'Дополнительное занятие должно идти после отменённого')
        if not after['validation']['valid']:
            raise HTTPException(409,'Компенсация нарушает ограничения; версия не сохранена')
        record.update(status='scheduled',planned={'experimentId':experiment_id,'eventId':event['eventId'],
            'date':_when(after,event),'revision':after['revision'],'versionHash':after['versionHash']})
        record.setdefault('makeupLinks',[]).append(deepcopy(record['planned']))
        changed=True
    if not changed:return
    for record in ledger['records']:
        if record['status']=='scheduled':_check_scheduled(record,after,ledger)
    ledger['revision']+=1
    if context:
        if len(ledger['receipts'])>=MAX_RECEIPTS:raise HTTPException(429,'Достигнут предел квитанций компенсаций')
        ledger['receipts'].append({'requestId':context['requestId'],'actor':actor,'fingerprint':context['fingerprint'],
            'response':{'experimentId':experiment_id,'revision':after['revision'],'versionHash':after['versionHash'],
                'ledgerRevision':ledger['revision'],'recordId':context['recordId'],'status':'scheduled',
                'message':'Допзанятие запланировано в черновике. Проведение и публикация не подтверждены.'}})
    state[KEY]=ledger
    _ledger(state,school)


def collection(experiment_id,profile):
    actor=_actor(profile)
    from .experimental_edits import get_version
    version=get_version(experiment_id,profile)
    ledger=_ledger(db.read_state(),actor['schoolId'])
    records=[{key:deepcopy(value)for key,value in record.items()if key not in {'demand','resources','slots','createdBy','course','makeupLinks'}}for record in ledger['records']]
    return {'revision':ledger['revision'],'records':records,'pending':sum(r['status']=='pending'for r in records),
        'scheduled':sum(r['status']=='scheduled'for r in records),'experimentId':experiment_id,
        'expectedRevision':version['revision'],'expectedVersionHash':version['versionHash'],
        'notice':'Одна запись — одно физическое занятие. Запланированное допзанятие ещё не считается проведённым.'}


class SuggestInput(StrictModel):
    expectedRevision:int=Field(ge=1,le=2**31-1)
    expectedVersionHash:str=Field(pattern=r'^[a-f0-9]{64}$')
    expectedLedgerRevision:int=Field(ge=0,le=2**31-1)
    recordId:str=Field(pattern=r'^cancel_[a-f0-9]{32}$')
    requestId:str=Field(min_length=16,max_length=64,pattern=r'^[A-Za-z0-9_-]+$')


class ApplyInput(SuggestInput):
    operation:dict
    previewHash:str=Field(pattern=r'^[a-f0-9]{64}$')


def _bound(experiment_id,body,profile):
    actor=_actor(profile)
    from .experimental_edits import get_version
    version=get_version(experiment_id,profile)
    if version['revision']!=body.expectedRevision or version['versionHash']!=body.expectedVersionHash:
        raise HTTPException(409,'Версия расписания изменилась')
    if not version['validation']['valid']:
        raise HTTPException(409,'Сначала исправьте конфликты выбранной сетки')
    ledger=_ledger(db.read_state(),actor['schoolId'])
    if ledger['revision']!=body.expectedLedgerRevision:raise HTTPException(409,'Реестр отмен изменился')
    record=next((r for r in ledger['records']if r['id']==body.recordId),None)
    if record is None:raise HTTPException(404,'Отмена не найдена')
    if record['status']!='pending':raise HTTPException(409,'Для этой отмены допзанятие уже запланировано или отмена отозвана')
    if not isinstance(record.get('course'),dict):
        raise HTTPException(409,'Исходная нагрузка отмены требует повторной проверки')
    if record['origin']['sourceBinding'].get('packageHash')!=version['sourceBinding'].get('packageHash'):
        raise HTTPException(409,'Школьные источники изменились после отмены; требуется повторная проверка курса')
    if record['course'].get('syncKey') or record['course'].get('chains'):
        raise HTTPException(409,'Это часть связанного занятия. Компенсацию нужно согласовать для всей связки вместе')
    # Reuse stable known IDs only with the same resource meaning. No automatic
    # matching by names or substitution of another teacher/group is permitted.
    for kind in ('teachers','groups'):
        target={x['id']:x for x in version['input'][kind]}
        for source in record['resources'][kind]:
            if source['id']not in target or any(source.get(k)!=target[source['id']].get(k)for k in ('name','className','kind','partition','size','attendanceUnitIds')):
                raise HTTPException(409,'Состав педагогов или групп изменился; требуется явное сопоставление')
    return actor,version,ledger,record


def _edit_body(body,operation):
    return {'expectedRevision':body.expectedRevision,'expectedVersionHash':body.expectedVersionHash,
        'requestId':'makeup_'+body.requestId,'operations':[operation]}


def _preview(experiment_id,body,operation,profile):
    actor,version,ledger,record=_bound(experiment_id,body,profile)
    from .experimental_edits import Add,preview_edit
    try:operation=Add.model_validate(operation).model_dump()
    except ValidationError:raise HTTPException(422,'Некорректное предложение допзанятия')from None
    if (operation['subject']!=record['subject'] or sorted(operation['teacherIds'])!=sorted(record['demand']['teacherIds'])
            or sorted(operation['groupIds'])!=sorted(record['demand']['groupIds'])):
        raise HTTPException(422,'Подмена предмета, педагога или группы компенсации запрещена')
    slot=next((s for s in version['input']['slots']if s['id']==operation['slotId']),None)
    if slot is None or (date.fromisoformat(version['periodStart'])+timedelta(days=slot['day'])).isoformat()<=record['origin']['date']:
        raise HTTPException(422,'Выберите дату после отменённого занятия')
    rooms=record['demand'].get('roomIds',[])
    if rooms and operation['roomId']not in rooms:
        raise HTTPException(422,'Кабинет не разрешён исходным курсом')
    allowed=record['demand'].get('allowedSlotIds',[])
    if allowed and operation['slotId']not in allowed:
        raise HTTPException(422,'Это время не разрешено исходным курсом')
    limit=record['course']['maxPerDay']
    if _course_counts(record,version,ledger)[slot['day']]>=limit:
        raise HTTPException(409,'В этот день уже достигнут исходный предел занятий этой нагрузки')
    token=_context.set({'operation':operation,'trustedRecord':deepcopy(record)})
    try:checked=preview_edit(experiment_id,_edit_body(body,operation),profile)
    finally:_context.reset(token)
    if not checked['validation']['valid']:raise HTTPException(409,'Допзанятие не проходит проверку ограничений')
    binding={'actor':actor,'experimentId':experiment_id,'request':SuggestInput.model_validate(body.model_dump(include=set(SuggestInput.model_fields))).model_dump(),
        'operation':operation,'editorPreviewHash':checked['previewHash'],'recordHash':digest(record)}
    return {'status':'candidate_ready','recordId':record['id'],'operation':operation,'previewHash':digest(binding),
        'editorPreviewHash':checked['previewHash'],'changes':checked['changes'],'validation':checked['validation'],
        'message':'Найдено место для одного допзанятия. Текущие занятия сохраняют свои места. Подтвердите запись в черновик.'}


def suggest(experiment_id,body,profile):
    body=SuggestInput.model_validate(body)
    _,version,_,record=_bound(experiment_id,body,profile)
    from .scheduling_solver import solve_plan,validate_solution
    plan=deepcopy(version['input'])
    demand=deepcopy(record['demand']);demand.update(id='makeup_'+digest(record['id'])[:32],weeklyCount=1,sourceLessonIds=[],syncKey='')
    demand.pop('teachingDays',None)  # One debt is one extra session, not a new weekly course.
    known_rooms={r['id']for r in plan['rooms']}
    if demand.get('roomIds'):demand['roomIds']=[r for r in demand['roomIds']if r in known_rooms]
    if record['demand'].get('roomIds')and not demand['roomIds']:raise HTTPException(409,'Нет подтверждённого кабинета исходного курса')
    # Preserve every existing event. The solver chooses a free legal position,
    # never removes a regular lesson in order to pay an older debt.
    plan['pins']=deepcopy(version['assignments']);plan['baseline']=deepcopy(version['assignments'])
    first=record['origin']['date']
    allowed={s['id']for s in plan['slots']if(date.fromisoformat(plan['periodStart'])+timedelta(days=s['day'])).isoformat()>first}
    counts=_course_counts(record,version,_ledger(db.read_state(),profile['schoolId']))
    allowed={identity for identity in allowed if counts[next(s['day']for s in plan['slots']if s['id']==identity)]<record['course']['maxPerDay']}
    original=set(demand.get('allowedSlotIds',[]))
    if original:allowed&=original
    if not allowed:return {'status':'infeasible','message':'В выбранной неделе нет допустимой даты после отмены. Выберите другую неделю.','recordId':record['id']}
    demand['allowedSlotIds']=sorted(allowed);plan['demands'].append(demand)
    result=solve_plan(plan,mode='repair',time_limit=5)
    if result['status']not in {'optimal','feasible'}:
        return {'status':result['status'],'recordId':record['id'],'message':'Допзанятие не размещено при сохранении текущих мест и обязательных ограничений. Выберите другую неделю или отдельно согласуйте перестановки.','diagnostics':result.get('diagnostics',[])}
    if not validate_solution(plan,result['assignments'])['valid']:raise HTTPException(409,'Независимая проверка расчёта не пройдена')
    assignment=next(a for a in result['assignments']if a['eventId']==demand['id']+'_0')
    op={'kind':'add','subject':record['subject'],'teacherIds':record['demand']['teacherIds'],
        'groupIds':record['demand']['groupIds'],'slotId':assignment['slotId'],'roomId':assignment['roomId'],
        'reason':'Допзанятие за отмену '+record['id']+' от '+record['origin']['date']}
    return _preview(experiment_id,body,op,profile)


def apply(experiment_id,body,profile):
    body=ApplyInput.model_validate(body)
    actor=_actor(profile);fingerprint=digest({'experimentId':experiment_id,'body':body.model_dump(),'actor':actor})
    ledger=_ledger(db.read_state(),actor['schoolId'])
    old=next((r for r in ledger['receipts']if r['requestId']==body.requestId and r['actor']==actor),None)
    if old:
        if old['fingerprint']!=fingerprint:raise HTTPException(409,'Квитанция компенсации уже использована иначе')
        return deepcopy(old['response'])
    checked=_preview(experiment_id,body,body.operation,profile)
    if checked['previewHash']!=body.previewHash:raise HTTPException(409,'Предпросмотр допзанятия изменился')
    from .experimental_edits import apply_edit
    _,_,_,record=_bound(experiment_id,body,profile)
    context={**body.model_dump(),'fingerprint':fingerprint,'trustedRecord':deepcopy(record)}
    token=_context.set(context)
    try:
        apply_edit(experiment_id,{**_edit_body(body,body.operation),'previewHash':checked['editorPreviewHash']},profile)
    finally:_context.reset(token)
    ledger=_ledger(db.read_state(),actor['schoolId'])
    saved=next((r for r in ledger['receipts']if r['requestId']==body.requestId and r['actor']==actor),None)
    if saved is None:raise HTTPException(409,'Квитанция допзанятия не подтверждена; обновите реестр')
    return deepcopy(saved['response'])


@router.get('/{experiment_id}/makeups')
def list_route(experiment_id:str=APIPath(pattern=r'^exp_[a-f0-9]{32}$'),profile=Depends(session_profile)):
    return collection(experiment_id,profile)


@router.post('/{experiment_id}/makeups/preview')
def suggest_route(body:SuggestInput,experiment_id:str=APIPath(pattern=r'^exp_[a-f0-9]{32}$'),profile=Depends(session_profile)):
    return suggest(experiment_id,body,profile)


@router.post('/{experiment_id}/makeups/apply')
def apply_route(body:ApplyInput,experiment_id:str=APIPath(pattern=r'^exp_[a-f0-9]{32}$'),profile=Depends(session_profile)):
    return apply(experiment_id,body,profile)
