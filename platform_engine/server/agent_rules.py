"""Explicitly reviewed weekly rules, separate from dated absences and the grid.

The ledger is domain JSON, not agent memory or model output. All calculation
effects are pure; only apply/archive write this namespace after current manager
authority, exact context and CAS checks. No publication or timetable writes.
"""
from contextlib import closing
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
import re
import sqlite3
from typing import Literal, Union

from fastapi import HTTPException
from pydantic import Field, ValidationError, model_validator

from . import db
from .scheduling_models import PlanningInput, StrictModel, Identifier, canonical, minutes
from .teacher_absences import _actor, _context, digest, literal_absence

KEY = '_agentStandingRules'
MAX_RULES = 200
MAX_COMMANDS = 256
MAX_BYTES = 2 * 1024 * 1024
SHA = r'^[a-f0-9]{64}$'
RULE_ID = r'^rule_[a-f0-9]{32}$'
REQUEST = r'^[A-Za-z0-9_-]{16,80}$'


def _iso(value):
    if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
        raise ValueError('Нужна точная дата YYYY-MM-DD')
    date.fromisoformat(value)
    return value


class Resource(StrictModel):
    id: Identifier
    name: str = Field(min_length=1, max_length=160)


class GroupRef(Resource):
    className: str = Field(min_length=1, max_length=80)
    kind: Literal['whole', 'subgroup']
    partition: str = Field(max_length=80)


class WeeklyPlacement(StrictModel):
    kind: Literal['weekly_placement']
    demandId: str = Field(min_length=1, max_length=77, pattern=r'^[A-Za-z0-9_-]+$')
    occurrence: int = Field(ge=0, le=39)
    demandFingerprint: str = Field(pattern=SHA)
    teacherRefs: list[Resource] = Field(min_length=1, max_length=8)
    groupRefs: list[GroupRef] = Field(min_length=1, max_length=200)
    day: int = Field(ge=0, le=5)
    slot: int = Field(ge=1, le=20)
    start: str = Field(pattern=r'^[0-9]{2}:[0-9]{2}$')
    end: str = Field(pattern=r'^[0-9]{2}:[0-9]{2}$')
    room: Resource

    @model_validator(mode='after')
    def intervals(self):
        if minutes(self.start) >= minutes(self.end):raise ValueError('Некорректный интервал занятия')
        for refs in (self.teacherRefs,self.groupRefs):
            if len({r.id for r in refs}) != len(refs):raise ValueError('Повтор ресурса правила')
        return self


class WeeklyUnavailable(StrictModel):
    kind: Literal['weekly_teacher_unavailable']
    teacher: Resource
    weekdays: list[int] = Field(min_length=1, max_length=6)
    startTime: str = Field(pattern=r'^[0-9]{2}:[0-9]{2}$')
    endTime: str = Field(pattern=r'^[0-9]{2}:[0-9]{2}$')

    @model_validator(mode='after')
    def intervals(self):
        if any(type(d)is not int or not 0<=d<=5 for d in self.weekdays) or len(set(self.weekdays))!=len(self.weekdays):
            raise ValueError('Нужны разные учебные дни0–5')
        end=1440 if self.endTime=='24:00' else minutes(self.endTime)
        if minutes(self.startTime)>=end:raise ValueError('Некорректное время недоступности')
        return self


Rule = Union[WeeklyPlacement, WeeklyUnavailable]


class Actor(StrictModel):
    schoolId: str = Field(min_length=1,max_length=160)
    userId: str = Field(min_length=1,max_length=160)
    membershipId: str = Field(min_length=1,max_length=160)
    membershipRevision: int = Field(ge=0)
    role: Literal['director','planner']


class Proposal(StrictModel):
    kind: Literal['standing_rule']
    summary: str = Field(min_length=1,max_length=4000)
    rules: list[Rule] = Field(min_length=1,max_length=100)
    effectiveFrom: str
    binding: dict
    hash: str = Field(pattern=SHA)

    @model_validator(mode='after')
    def checked(self):
        _iso(self.effectiveFrom)
        b=self.binding
        required={'schoolId','calendarRevision','calendarHash','draftRevision','draftHash','sourceBinding',
                  'inputHash','rulesRevision','rulesHash','actor'}
        target={'targetSourceBinding','targetExperimentId','expectedRevision','expectedVersionHash'}
        config={'configRevision'}
        if set(b) not in (required|target,required|config):raise ValueError('Неполная привязка правила')
        Actor.model_validate(b['actor'])
        if b['schoolId']!=b['actor']['schoolId']:raise ValueError('Школа привязки не совпадает')
        for key in ('calendarRevision','draftRevision','rulesRevision', 'expectedRevision' if 'targetExperimentId'in b else 'configRevision'):
            if type(b[key])is not int or not 0<=b[key]<=2**31-1:raise ValueError('Некорректная версия привязки')
        for key in ('calendarHash','draftHash','inputHash',*(['expectedVersionHash']if'targetExperimentId'in b else[])):
            if not isinstance(b[key],str)or not re.fullmatch(SHA,b[key]):raise ValueError('Некорректный хеш привязки')
        if b['rulesHash']is not None and(not isinstance(b['rulesHash'],str)or not re.fullmatch(SHA,b['rulesHash'])):
            raise ValueError('Некорректный хеш реестра')
        if b['sourceBinding']is not None and not isinstance(b['sourceBinding'],dict):raise ValueError('Нужна привязка источников')
        if 'targetExperimentId'in b and(not isinstance(b['targetExperimentId'],str)or not re.fullmatch(r'exp_[a-f0-9]{32}',b['targetExperimentId'])or not isinstance(b['targetSourceBinding'],dict)):
            raise ValueError('Некорректная привязка эксперимента')
        return self


class Entry(StrictModel):
    id: str = Field(pattern=RULE_ID)
    summary: str = Field(min_length=1,max_length=4000)
    effectiveFrom: str
    rule: Rule
    archived: bool
    createdRevision: int = Field(ge=1,le=2**31-1)
    archivedRevision: int | None
    createdAt: str
    archivedAt: str | None
    actor: Actor

    @model_validator(mode='after')
    def checked(self):
        _iso(self.effectiveFrom);datetime.fromisoformat(self.createdAt)
        if self.archived != (self.archivedRevision is not None and self.archivedAt is not None):raise ValueError('Архив правила повреждён')
        if not self.archived and(self.archivedRevision is not None or self.archivedAt is not None):raise ValueError('Неархивированное правило содержит архивные данные')
        if self.archived:
            datetime.fromisoformat(self.archivedAt)
            if type(self.archivedRevision)is not int or self.archivedRevision<=self.createdRevision:raise ValueError('Порядок архива повреждён')
        return self


class Receipt(StrictModel):
    requestId: str = Field(pattern=REQUEST)
    kind: Literal['apply','archive']
    actor: Actor
    hash: str = Field(pattern=SHA)
    revision: int = Field(ge=1,le=2**31-1)
    ruleIds: list[str] = Field(min_length=1,max_length=100)
    summary: str = Field(min_length=1,max_length=4000)
    payload: dict


class History(StrictModel):
    revision: int = Field(ge=1,le=2**31-1)
    action: Literal['apply','archive']
    ruleIds: list[str] = Field(min_length=1,max_length=100)
    actor: Actor
    at: str
    requestId: str = Field(pattern=REQUEST)


class Ledger(StrictModel):
    version: Literal[1]
    schoolId: str = Field(min_length=1,max_length=160)
    revision: int = Field(ge=0,le=MAX_COMMANDS)
    rules: list[Entry] = Field(max_length=MAX_RULES)
    history: list[History] = Field(max_length=MAX_COMMANDS)
    receipts: list[Receipt] = Field(max_length=MAX_COMMANDS)


class ArchiveInput(StrictModel):
    expectedRevision: int = Field(ge=0,le=2**31-1)
    requestId: str = Field(pattern=REQUEST)


def _who(profile):
    return {k:profile[k]for k in Actor.model_fields}


def _proposal(value):
    try:
        parsed=Proposal.model_validate(value).model_dump()
        if len(json.dumps(parsed,ensure_ascii=False).encode())>MAX_BYTES:raise ValueError()
        unsigned=deepcopy(parsed);unsigned.pop('hash')
        if digest(unsigned)!=parsed['hash']:raise ValueError()
        return parsed
    except (ValidationError,ValueError,TypeError,KeyError):
        raise HTTPException(409,'Предложение постоянного правила повреждено или изменилось')from None


def ledger(state,school_id):
    if KEY not in state:return {'version':1,'schoolId':school_id,'revision':0,'rules':[],'history':[],'receipts':[]}
    try:
        raw=state[KEY]
        if not isinstance(raw,dict)or type(raw.get('version'))is not int or raw['version']!=1:raise ValueError()
        if len(json.dumps(raw,ensure_ascii=False,allow_nan=False).encode())>MAX_BYTES:raise ValueError()
        data=Ledger.model_validate(raw).model_dump()
        if data['schoolId']!=school_id:raise ValueError()
        rules={r['id']:r for r in data['rules']};receipts={r['requestId']:r for r in data['receipts']}
        if len(rules)!=len(data['rules'])or len(receipts)!=len(data['receipts']):raise ValueError()
        if sorted(r['revision']for r in receipts.values())!=list(range(1,data['revision']+1)):raise ValueError()
        if sorted(h['revision']for h in data['history'])!=list(range(1,data['revision']+1)):raise ValueError()
        for record in receipts.values():
            if len(set(record['ruleIds']))!=len(record['ruleIds'])or any(i not in rules for i in record['ruleIds']):raise ValueError()
            if record['actor']['schoolId']!=school_id:raise ValueError()
            h=next(h for h in data['history']if h['revision']==record['revision'])
            if any(h[k]!=record[j]for k,j in [('requestId','requestId'),('actor','actor'),('ruleIds','ruleIds'),('action','kind')]):raise ValueError()
            datetime.fromisoformat(h['at'])
            if record['kind']=='apply':
                proposal=_proposal(record['payload'])
                if proposal['hash']!=record['hash']or proposal['binding']['actor']!=record['actor']or proposal['summary']!=record['summary']or len(proposal['rules'])!=len(record['ruleIds']):raise ValueError()
                for ident,rule in zip(record['ruleIds'],proposal['rules']):
                    entry=rules[ident]
                    if any(entry[k]!=v for k,v in [('rule',rule),('summary',proposal['summary']),('effectiveFrom',proposal['effectiveFrom']),('createdRevision',record['revision']),('createdAt',h['at']),('actor',record['actor'])]):raise ValueError()
                    if ident!='rule_'+digest([record['actor'],record['requestId'],record['hash'],record['ruleIds'].index(ident)])[:32]:raise ValueError()
            else:
                payload=record['payload'];ArchiveInput.model_validate({k:payload[k]for k in ('expectedRevision','requestId')})
                if set(payload)!={'ruleId','expectedRevision','requestId'}or digest(payload)!=record['hash']or payload['requestId']!=record['requestId']or record['ruleIds']!=[payload['ruleId']]:raise ValueError()
                entry=rules[payload['ruleId']]
                if not entry['archived']or entry['archivedRevision']!=record['revision']or entry['archivedAt']!=h['at']or record['summary']!=entry['summary']:raise ValueError()
        for entry in rules.values():
            if not any(r['kind']=='apply'and entry['id']in r['ruleIds']for r in receipts.values()):raise ValueError()
            if entry['archived']and not any(r['kind']=='archive'and entry['id']in r['ruleIds']for r in receipts.values()):raise ValueError()
        return data
    except (ValidationError,ValueError,TypeError,KeyError,StopIteration,HTTPException):
        raise HTTPException(409,'Реестр постоянных правил повреждён или принадлежит другой школе; изменения не выполнены')from None


def binding(state,school_id):
    if KEY not in state:return None
    data=ledger(state,school_id)
    return {'revision':data['revision'],'hash':digest(data)}


def active(state,school_id):
    return any(not r['archived']for r in ledger(state,school_id)['rules'])


def _refs(plan,demand):
    teachers={r['id']:r for r in plan['teachers']};groups={r['id']:r for r in plan['groups']}
    return ([{k:teachers[i][k]for k in ('id','name')}for i in sorted(demand['teacherIds'])],
            [{k:groups[i][k]for k in ('id','name','className','kind','partition')}for i in sorted(demand['groupIds'])])


def _fingerprint(plan,demand):
    ts,gs=_refs(plan,demand)
    value={k:deepcopy(demand[k])for k in ('id','subject','weeklyCount','sourceLessonIds')}
    value.update(teacherIds=sorted(demand['teacherIds']),groupIds=sorted(demand['groupIds']),teacherRefs=ts,groupRefs=gs)
    return digest(value)


def _issue(code,message,ident):return {'code':code,'message':message,'ids':[ident]}


def effect(state,plan,school_id):
    data=ledger(state,school_id);result=deepcopy(plan);blockers=[];diagnostics=[]
    if not any(not r['archived']for r in data['rules']):return {'effectiveInput':result,'blockers':[],'diagnostics':[]}
    model=PlanningInput.model_validate(plan).model_dump();slots=model['slots'];teachers={t['id']:t for t in model['teachers']}
    demands={d['id']:d for d in model['demands']};rooms={r['id']:r for r in model['rooms']}
    first=date.fromisoformat(model['periodStart']);pins={p['eventId']:deepcopy(p)for p in model['pins']}
    for entry in data['rules']:
        if entry['archived']or entry['effectiveFrom']>(first+timedelta(days=5)).isoformat():continue
        rule=entry['rule'];ident=entry['id']
        if rule['kind']=='weekly_teacher_unavailable':
            ref=rule['teacher'];teacher=teachers.get(ref['id'])
            if teacher is None or teacher['name']!=ref['name']:
                blockers.append(_issue('standing_teacher_identity','Педагог постоянного правила отсутствует или изменился; нужно явное сопоставление.',ident));continue
            lo=minutes(rule['startTime']);hi=1440 if rule['endTime']=='24:00'else minutes(rule['endTime'])
            days={d for d in rule['weekdays']if(first+timedelta(days=d)).isoformat()>=entry['effectiveFrom']}
            if any(e['day']in days and minutes(e['start'])<hi and lo<minutes(e['end'])for e in teacher['presenceEvents']):
                blockers.append(_issue('standing_presence_conflict','Недоступность пересекает явную очную обязанность; обязанность не удалена.',ident))
            teacher['unavailableSlotIds']=sorted(set(teacher['unavailableSlotIds'])|{s['id']for s in slots if s['day']in days and minutes(s['start'])<hi and lo<minutes(s['end'])})
        else:
            if(first+timedelta(days=rule['day'])).isoformat()<entry['effectiveFrom']:continue
            demand=demands.get(rule['demandId'])
            if demand is None or rule['occurrence']>=demand['weeklyCount']or _fingerprint(model,demand)!=rule['demandFingerprint']or _refs(model,demand)!=(rule['teacherRefs'],rule['groupRefs']):
                blockers.append(_issue('standing_demand_identity','Нагрузка, состав или номер занятия постоянного правила изменились; правило не сопоставлялось по похожему имени.',ident));continue
            room=rooms.get(rule['room']['id'])
            matches=[s for s in slots if all(s[k]==rule[k]for k in ('day','slot','start','end'))]
            if room is None or room['name']!=rule['room']['name']or len(matches)!=1:
                blockers.append(_issue('standing_placement_identity','Кабинет или точный недельный слот отсутствует/изменился; нужно пересмотреть правило.',ident));continue
            event=f"{demand['id']}_{rule['occurrence']}";row={'eventId':event,'slotId':matches[0]['id'],'roomId':room['id']}
            if event in pins and pins[event]!=row:
                blockers.append(_issue('standing_pin_conflict','Постоянная позиция противоречит другому явному закреплению; старое закрепление сохранено.',ident));continue
            if room['id']not in demand['roomIds']or demand['allowedSlotIds']and row['slotId']not in demand['allowedSlotIds']:
                blockers.append(_issue('standing_placement_domain','Постоянная позиция не входит в разрешённые слоты/кабинеты нагрузки.',ident));continue
            pins[event]=row
        diagnostics.append(_issue('standing_rule_applied','Применено явно подтверждённое постоянное недельное правило.',ident))
    event_demands={f"{d['id']}_{n}":d for d in demands.values()for n in range(d['weeklyCount'])}
    for event,pin in pins.items():
        demand=event_demands.get(event)
        if demand and any(pin['slotId']in teachers[t]['unavailableSlotIds']for t in demand['teacherIds']):
            blockers.append(_issue('standing_availability_pin_conflict','Недоступность и явная позиция занятия конфликтуют; занятия и ограничения сохранены.',event))
    model['pins']=list(pins.values())
    return {'effectiveInput':model,'blockers':blockers,'diagnostics':diagnostics}


def _base(state,model_mode):
    return {'revision':state.get('expectedRevision',0),'issues':[],'questions':[],
            'modelMode':model_mode,'proposal':None,'proposal_hash':''}


def _clarify(base,message,path='standing_rule'):
    return {**base,'status':'needs_clarification','message':message,'questions':[{'path':path,'message':message}]}


def _snapshot(state,profile):
    if not db.database_path().is_file():raise HTTPException(409,'Сначала откройте школьный кабинет и выберите исходный план.')
    with closing(sqlite3.connect(db.database_path().as_uri()+'?mode=ro',uri=True,timeout=15))as connection:
        connection.execute('BEGIN');domain,_,bound,plan,version=_context(connection,profile,state.get('targetExperimentId'))
    if version and(bound['expectedRevision']!=state.get('expectedRevision')or bound['expectedVersionHash']!=state.get('targetVersionHash')):
        raise HTTPException(409,'Выбранная версия изменилась; подготовьте постоянное правило заново.')
    current=binding(domain,profile['schoolId'])
    bound.update(rulesRevision=current['revision']if current else 0,rulesHash=current['hash']if current else None,actor=_who(profile))
    return domain,bound,plan,version


def _prepared(base,bound,plan,rules,summary):
    proposal={'kind':'standing_rule','summary':summary,'rules':rules,'effectiveFrom':plan['periodStart'],'binding':bound}
    proposal['hash']=digest(proposal);proposal=_proposal(proposal)
    return {**base,'status':'awaiting_approval','proposal':proposal,'proposal_hash':proposal['hash'],
            'message':summary+' Подтвердите сохранение постоянного правила и отдельный пересчёт. Сетка не применяется и не публикуется.'}


def prepare_experimental(state,version,preview,operations):
    profile=_actor(state['owner']);base=_base(state,state.get('model_mode','structured_model'))
    if state.get('ruleScope','once')!='persistent':return _clarify(base,'Для постоянного правила явно выберите постоянную область действия.')
    ops=[op.model_dump(exclude_none=True)if hasattr(op,'model_dump')else deepcopy(op)for op in operations]
    if not ops or any(op.get('kind')not in {'move','swap','room','pin'}or op.get('kind')=='pin'and op.get('pinned')is not True for op in ops):
        return _clarify(base,'Постоянно можно сохранить только полную недельную позицию существующего занятия: перенос, обмен, кабинет или закрепление. Изменение нагрузки/педагога, удаление и снятие закрепления требуют отдельного решения.')
    from .experimental_edits import Move, Swap, Room, Pin
    try:
        types={'move':Move,'swap':Swap,'room':Room,'pin':Pin}
        if len(ops)>100:raise ValueError()
        ops=[types[op['kind']].model_validate(op).model_dump(exclude_none=True)for op in ops]
    except (ValidationError,ValueError,KeyError,TypeError):return _clarify(base,'Нужны точные допустимые операции выбранного занятия без дополнительных полей.')
    if not preview.get('validation',{}).get('valid'):
        return {**base,'status':'blocked','message':'Правки не прошли независимую проверку; постоянное правило не подготовлено.','issues':preview.get('issues',[])}
    try:_,bound,_,current=_snapshot(state,profile)
    except HTTPException as e:
        if e.status_code in {401,403}:raise
        return {**base,'status':'stale','message':str(e.detail)}
    if current is None or current['versionHash']!=version['versionHash']or current['revision']!=version['revision']:
        return {**base,'status':'stale','message':'Выбранная версия изменилась; подготовьте правило заново.'}
    plan=PlanningInput.model_validate(current['originalInput']).model_dump()
    positions={a['eventId']:deepcopy(a)for a in current['physicalAssignments']};touched=set()
    identities={f"{d['id']}_{n}":(d,n)for d in plan['demands']for n in range(d['weeklyCount'])}
    details={r['eventId']:r for r in current['eventDetails']}
    try:
        for op in ops:
            event=op['eventId'];row=positions[event];touched.add(event)
            if event not in identities:raise ValueError('Добавленное вручную занятие не имеет устойчивой исходной нагрузки.')
            if op['kind']=='move':row['slotId']=op['slotId'];row['roomId']=op.get('roomId',row['roomId'])
            elif op['kind']=='room':row['roomId']=op['roomId']
            elif op['kind']=='swap':
                other=op['otherEventId'];second=positions[other]
                if other not in identities or other==event:raise ValueError('Для недельного обмена нужны два разных исходных занятия.')
                touched.add(other);row['slotId'],second['slotId']=second['slotId'],row['slotId']
        from .scheduling_solver import validate_solution
        checked=validate_solution(plan,list(positions.values()))
        if not checked['valid']:
            return {**base,'status':'blocked','message':'Полные недельные позиции нарушают исходные ограничения; правило не сохранено.','issues':checked['issues']}
        rules=[];summaries=[];slots={s['id']:s for s in plan['slots']};rooms={r['id']:r for r in plan['rooms']}
        for event in sorted(touched):
            demand,n=identities[event];detail=details[event]
            if any(detail[k]!=demand[k]for k in ('subject','teacherIds','groupIds')):raise ValueError('Сначала явно согласуйте изменения предмета или состава: недельное правило их не переносит.')
            row=positions[event];slot=slots[row['slotId']];room=rooms[row['roomId']];ts,gs=_refs(plan,demand)
            rules.append({'kind':'weekly_placement','demandId':demand['id'],'occurrence':n,'demandFingerprint':_fingerprint(plan,demand),
                          'teacherRefs':ts,'groupRefs':gs,**{k:slot[k]for k in ('day','slot','start','end')},'room':{'id':room['id'],'name':room['name']}})
            label=', '.join(sorted({g['className']for g in gs}))+' · '+demand['subject']+' · '+', '.join(t['name']for t in ts)
            summaries.append(f"{label} ({event}): каждую неделю, {('Пн','Вт','Ср','Чт','Пт','Сб')[slot['day']]}, {slot['slot']} урок ({slot['start']}–{slot['end']}), кабинет {room['name']}")
        summary=f"С {plan['periodStart']}: "+'; '.join(summaries)+'. Полная позиция: день, урок и кабинет. Привязка к точной нагрузке и ресурсам выбранной модели; при изменении состава требуется пересмотр.'
        if len(summary)>4000:return _clarify(base,'Подготовьте меньше постоянных позиций одним запросом, чтобы полностью показать каждую из них.')
        return _prepared(base,bound,plan,rules,summary)
    except (KeyError,ValueError,TypeError)as error:return _clarify(base,str(error)or'Не удалось однозначно определить недельные позиции.')


_DAYS={0:r'понедельник(?:ам|и)?',1:r'вторник(?:ам|и)?',2:r'сред(?:ам|у|ы|а)',3:r'четверг(?:ам|и)?',4:r'пятниц(?:ам|у|ы|а)',5:r'суббот(?:ам|у|ы|а)'}


def prepare_availability(state,*,model_mode='deterministic'):
    profile=_actor(state['owner']);base={**_base(state,model_mode),'absence_pending':True,'standing_rule_pending':True}
    if state.get('ruleScope','once')!='persistent':return _clarify(base,'Разовый запрос не становится постоянным правилом без явного выбора области действия.')
    messages=state.get('user_messages')or[state.get('user_message','')]
    if re.search(r'\b(?:отмени\w*|отмена|не\s+учитыва\w*|не\s+(?:будет\s+)?отсутств\w*|не\s+недоступ\w*)\b',messages[-1].casefold()):
        return {**base,'status':'rejected','decision':'reject','message':'Постоянное правило не сохранено; расписание не изменено.'}
    recurrence=r'\b(?:по\s+(?:'+('|'.join(_DAYS.values()))+r')|кажд\w*\s+(?:'+('|'.join(_DAYS.values()))+r'))\b'
    complete=[n for n,m in enumerate(messages)if literal_absence(m,advisory=False)and re.search(recurrence,m.casefold().replace('ё','е'))]
    messages=messages[complete[-1]:]if complete else messages
    text=' '.join(messages).casefold().replace('ё','е')
    temporal=re.sub(r'\bс\s*[0-9]{2}:[0-9]{2}\s*(?:до|по|[-–—])\s*[0-9]{2}:[0-9]{2}','',text)
    if (not any(literal_absence(m,advisory=False)for m in messages)
            or re.search(r'\b(?:эт\w*|следующ\w*|будущ\w*|текущ\w*)\s+недел|\d{4}-\d{2}-\d{2}|\d{1,2}[./]\d{1,2}|\b(?:завтра|сегодня|вчера)\b|(?<![0-9])\d{1,2}\s*(?:январ|феврал|март|апрел|мая|июн|июл|август|сентябр|октябр|ноябр|декабр)',text)
            or re.search(r'\b(?:до|после|начиная|четверт\w*|семестр\w*|каникул\w*|в\s+течение|на\s+(?:[0-9]+|недел\w*|месяц\w*|год)|(?:нов\w*|эт\w*|текущ\w*)\s+год|утром|вечером|ночью|днем|половин\w*|между)\b|(?<!\w)(?:до|с|к)\s*(?=[0-9])|[0-9]{1,2}\s*[-–—]\s*[0-9]{1,2}|[0-9]+\s*(?:урок|заняти)',temporal)
            or any(re.search(r'[«»"`?]|\b(?:сказал\w*|написал\w*|сообщил\w*|говорит|говорят|пишет|возможно|наверное|если|удали|отмени|не\s+учитыва|не\s+(?:будет\s+)?отсутств|не\s+недоступ)',m.casefold())for m in messages)):
        return _clarify(base,'Для постоянного правила сообщите прямо «педагог недоступен по понедельникам». Отсутствие на эту/следующую неделю и диапазон дат остаются разовыми, не бессрочными.','standing_rule.recurrence')
    days=[]
    for day,word in _DAYS.items():
        if re.search(r'\b(?:по\s+'+word+r'|кажд\w*\s+'+word+r')\b',text):days.append(day)
    if not days:return _clarify(base,'Назовите явное еженедельное повторение: например, «по понедельникам»; день или срок не будут угаданы.','standing_rule.recurrence')
    # Every other mentioned weekday must be explicitly recurring too.
    if any(re.search(r'\b'+word+r'\b',text)and day not in days for day,word in _DAYS.items()):
        return _clarify(base,'Уточните еженедельное повторение отдельно для каждого названного дня.','standing_rule.recurrence')
    times=re.findall(r'(?<!\d)[0-9]{2}:[0-9]{2}(?!\d)',text)
    lo,hi='00:00','24:00'
    if times:
        if len(times)!=2 or not re.search(r'\bс\s*'+re.escape(times[0])+r'\s*(?:до|по|[-–—])\s*'+re.escape(times[1]),text):
            return _clarify(base,'Укажите один явный интервал «с HH:MM до HH:MM» или отсутствие весь день.','standing_rule.time')
        lo,hi=times
        try:WeeklyUnavailable(kind='weekly_teacher_unavailable',teacher={'id':'check','name':'check'},weekdays=days,startTime=lo,endTime=hi)
        except ValidationError:return _clarify(base,'Время должно быть существующим интервалом внутри суток.','standing_rule.time')
    try:_,bound,plan,version=_snapshot(state,profile)
    except HTTPException as e:
        if e.status_code in {401,403}:raise
        return {**base,'status':'blocked','message':str(e.detail)}
    matches=[t for t in plan['teachers']if re.search(r'(?<!\w)'+re.escape(canonical(t['id']))+r'(?!\w)',canonical(text))or re.search(r'(?<!\w)'+re.escape(canonical(t['name']))+r'(?!\w)',canonical(text))]
    if len(matches)!=1:
        return _clarify(base,'Укажите одного точного педагога текущего плана; похожие и одинаковые имена не сопоставляются автоматически.','standing_rule.teacher')
    teacher=matches[0]
    # A closed literal grammar is intentional. Unknown qualifiers (including
    # worded dates/durations) must never be discarded to make a rule indefinite.
    remainder=canonical(text)
    for value in sorted({canonical(teacher['name']),canonical(teacher['id'])},key=len,reverse=True):
        remainder=re.sub(r'(?<!\w)'+re.escape(value)+r'(?!\w)',' ',remainder)
    remainder=re.sub(recurrence,' ',remainder)
    remainder=re.sub(r'\bс\s*[0-9]{2}:[0-9]{2}\s*(?:до|по|[-–—])\s*[0-9]{2}:[0-9]{2}',' ',remainder)
    for pattern in (r'\b(?:отсутствует|отсутствуют|недоступен|недоступна|недоступны|не\s+будет)\b',
                    r'\b(?:каждую\s+неделю|каждой\s+недели|еженедельно|постоянно|всегда|постоянное\s+правило)\b',
                    r'\b(?:весь\s+день|целый\s+день|в\s+течение\s+всего\s+дня)\b',
                    r'\b(?:педагог|преподаватель|учитель|пожалуйста|учти|учтите|сохрани|сохраните|и|также)\b'):
        remainder=re.sub(pattern,' ',remainder)
    if re.sub(r'[\s.,;:!()\-–—]+','',remainder):
        return _clarify(base,'В сообщении остались неразобранные условия или срок. Напишите полную фразу «точный педагог недоступен по понедельникам», при необходимости добавьте один интервал «с HH:MM до HH:MM». Конечный срок не станет бессрочным.','standing_rule.recurrence')
    rule={'kind':'weekly_teacher_unavailable','teacher':{k:teacher[k]for k in ('id','name')},'weekdays':days,'startTime':lo,'endTime':hi}
    summary=f"С {plan['periodStart']}: {teacher['name']} недоступен каждую неделю, "+', '.join(('Пн','Вт','Ср','Чт','Пт','Сб')[d]for d in days)+f', {lo}–{hi}. Действует на будущие расчёты до явного архивирования. Привязка к точному педагогу текущей модели; другой состав требует явного сопоставления.'
    return _prepared(base,bound,plan,[rule],summary)


def _public(data):return {'revision':data['revision'],'rules':deepcopy(data['rules']),'history':deepcopy(data['history'])}


def collection(profile):
    profile=_actor(profile)
    from .school_calendar import _read_state
    if not db.database_path().is_file():
        result=_public(ledger(_read_state(),profile['schoolId']));_actor(profile);return result
    with closing(sqlite3.connect(db.database_path().as_uri()+'?mode=ro',uri=True))as connection:
        connection.execute('BEGIN');result=_public(ledger(_read_state(connection),profile['schoolId']))
    _actor(profile);return result


def _receipt(record):return {'ruleIds':deepcopy(record['ruleIds']),'rulesRevision':record['revision'],'summary':record['summary']}


def _capacity(data,count=0):
    if data['revision']>=MAX_COMMANDS or len(data['rules'])+count>MAX_RULES:
        raise HTTPException(429,'Достигнут предел постоянных правил/квитанций; существующие записи сохранены.')


def _store(connection,state,data,profile):
    state[KEY]=data;ledger(state,profile['schoolId']);_actor(profile)
    connection.execute('UPDATE demo_state SET payload=? WHERE id=1',(json.dumps(state,ensure_ascii=False,allow_nan=False),))
    if connection.execute('SELECT changes()').fetchone()[0]!=1:raise HTTPException(409,'Школьный контур не инициализирован.')
    connection.commit()


def apply(proposal,profile,task_id):
    profile=_actor(profile);proposal=_proposal(proposal);actor=_who(profile)
    if not isinstance(task_id,str)or not re.fullmatch(REQUEST,task_id):raise HTTPException(422,'Некорректный номер подтверждения.')
    if proposal['binding']['actor']!=actor:raise HTTPException(403,'Постоянное правило подготовлено другим составителем.')
    from .school_calendar import _read_state
    with closing(sqlite3.connect(db.database_path().as_uri()+'?mode=rw',uri=True,timeout=15))as connection:
        connection.execute('BEGIN IMMEDIATE');state=_read_state(connection);data=ledger(state,profile['schoolId'])
        prior=next((r for r in data['receipts']if r['requestId']==task_id),None)
        if prior:
            if prior['actor']!=actor or prior['hash']!=proposal['hash']or prior['kind']!='apply':raise HTTPException(409,'Номер подтверждения уже связан с другим правилом.')
            _actor(profile);return _receipt(prior)
        _capacity(data,len(proposal['rules']))
        _,_,current,_,_=_context(connection,profile,proposal['binding'].get('targetExperimentId'))
        rb=binding(state,profile['schoolId']);current.update(rulesRevision=rb['revision']if rb else 0,rulesHash=rb['hash']if rb else None,actor=actor)
        if current!=proposal['binding']:raise HTTPException(409,'План, календарь, источники или постоянные правила изменились; подготовьте предложение заново.')
        revision=data['revision']+1;stamp=datetime.now(timezone.utc).isoformat();ids=[]
        for index,rule in enumerate(proposal['rules']):
            ident='rule_'+digest([actor,task_id,proposal['hash'],index])[:32];ids.append(ident)
            data['rules'].append({'id':ident,'summary':proposal['summary'],'effectiveFrom':proposal['effectiveFrom'],'rule':deepcopy(rule),'archived':False,
                'createdRevision':revision,'archivedRevision':None,'createdAt':stamp,'archivedAt':None,'actor':actor})
        record={'requestId':task_id,'kind':'apply','actor':actor,'hash':proposal['hash'],'revision':revision,'ruleIds':ids,'summary':proposal['summary'],'payload':proposal}
        data['revision']=revision;data['receipts'].append(record)
        data['history'].append({'revision':revision,'action':'apply','ruleIds':ids,'actor':actor,'at':stamp,'requestId':task_id})
        _store(connection,state,data,profile);return _receipt(record)


def archive(rule_id,body,profile):
    profile=_actor(profile);body=ArchiveInput.model_validate(body);actor=_who(profile)
    if not isinstance(rule_id,str)or not re.fullmatch(RULE_ID,rule_id):raise HTTPException(422,'Некорректный идентификатор правила.')
    payload={'ruleId':rule_id,**body.model_dump()};fingerprint=digest(payload)
    from .school_calendar import _read_state
    with closing(sqlite3.connect(db.database_path().as_uri()+'?mode=rw',uri=True,timeout=15))as connection:
        connection.execute('BEGIN IMMEDIATE');state=_read_state(connection);data=ledger(state,profile['schoolId'])
        prior=next((r for r in data['receipts']if r['requestId']==body.requestId),None)
        if prior:
            if prior['actor']!=actor or prior['hash']!=fingerprint or prior['kind']!='archive':raise HTTPException(409,'Номер запроса уже связан с другим действием.')
            _actor(profile);return _public(data)
        if data['revision']!=body.expectedRevision:raise HTTPException(409,'Постоянные правила изменились; обновите список.')
        entry=next((r for r in data['rules']if r['id']==rule_id),None)
        if entry is None:raise HTTPException(404,'Постоянное правило не найдено.')
        if entry['archived']:raise HTTPException(409,'Постоянное правило уже архивировано.')
        _capacity(data);stamp=datetime.now(timezone.utc).isoformat();revision=data['revision']+1
        entry.update(archived=True,archivedRevision=revision,archivedAt=stamp);data['revision']=revision
        data['receipts'].append({'requestId':body.requestId,'kind':'archive','actor':actor,'hash':fingerprint,'revision':revision,'ruleIds':[rule_id],'summary':entry['summary'],'payload':payload})
        data['history'].append({'revision':revision,'action':'archive','ruleIds':[rule_id],'actor':actor,'at':stamp,'requestId':body.requestId})
        _store(connection,state,data,profile);return _public(data)


def assert_applied(state,proposal,profile,task_id):
    profile=_actor(profile);proposal=_proposal(proposal);data=ledger(state,profile['schoolId'])
    receipt=next((r for r in data['receipts']if r['requestId']==task_id),None)
    if receipt is None or receipt['kind']!='apply'or receipt['actor']!=_who(profile)or receipt['hash']!=proposal['hash']:
        raise HTTPException(409,'Подтверждение постоянного правила не найдено для этого составителя.')
    if receipt['revision']!=proposal['binding']['rulesRevision']+1 or data['revision']!=receipt['revision']or any(r['archived']for r in data['rules']if r['id']in receipt['ruleIds']):
        raise HTTPException(409,'Подтверждённые постоянные правила изменились или архивированы; нужен новый пересчёт.')
    return _receipt(receipt)


def assert_dispatch_context(state,proposal,profile,task_id):
    """Final write-transaction guard; a preflight read is not a CAS check."""
    profile=_actor(profile);proposal=_proposal(proposal)
    receipt=assert_applied(state,proposal,profile,task_id)
    from .school_calendar import _calendar
    calendar=_calendar(state,profile['schoolId'])
    if (calendar['revision']!=proposal['binding']['calendarRevision']
            or digest(calendar)!=proposal['binding']['calendarHash']):
        raise HTTPException(409,'Календарь изменился после подтверждения правила; правило сохранено, но нужен новый пересчёт.')
    return receipt


def dispatch(proposal,profile,task_id):
    domain_profile=deepcopy(profile);profile=_actor(profile);proposal=_proposal(proposal)
    with closing(sqlite3.connect(db.database_path().as_uri()+'?mode=ro',uri=True,timeout=15))as connection:
        connection.execute('BEGIN');state,_,current,plan,_=_context(connection,profile,proposal['binding'].get('targetExperimentId'))
        assert_dispatch_context(state,proposal,profile,task_id)
        expected={k:v for k,v in proposal['binding'].items()if k not in {'rulesRevision','rulesHash','actor'}}
        if current!=expected:raise HTTPException(409,'Исходный план, календарь или выбранная версия изменились после подтверждения правила.')
    if proposal['binding'].get('targetExperimentId'):
        from .experimental_school import recalculate_rules
        result=recalculate_rules(proposal,domain_profile,task_id)
        return {'experimentalDraftId':result['id'],'status':'calculating','actions':[{'id':'open_experiment','label':'Открыть пересчёт с постоянным правилом'}]}
    from .planning import get_service
    run=get_service().create_run({'expectedConfigRevision':proposal['binding']['configRevision'],'expectedDraftRevision':proposal['binding']['draftRevision'],
        'mode':'repair','timeLimitSeconds':10,'periodStart':plan['periodStart'],'requestId':'standing_'+digest(task_id)[:48]},domain_profile,
        standing_rule_guard={'proposal':proposal,'taskId':task_id})
    return {'runId':run['id'],'status':'calculating','actions':[{'section':'runs','runId':run['id'],'label':'Открыть пересчёт с постоянным правилом'}]}
