"""Locally grounded, explicitly approved dated teacher availability.

The existing text agent supplies an advisory intent. Names, dates, authority and
all writes are resolved here; no model, provider, publication or deletion calls.
"""
from copy import deepcopy
from contextlib import closing
from datetime import date, timedelta
import hashlib
import json
import re
import sqlite3

from fastapi import HTTPException

from . import db
from .calendar_models import ExceptionInput
from .scheduling_models import PlanningInput, canonical

MAX_RECEIPTS = 128


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def literal_absence(text, *, advisory=False):
    """Small positive grammar, never a claim of model understanding."""
    value=text.casefold().replace('ё','е').strip()
    if any(mark in value for mark in ('"','«','»','`','\n>')) or '?' in value:
        return False
    if re.search(r'\b(?:сказал|написал|сообщил|если|удали|удалите|отмени|отмените|опубликуй|опубликуйте)\b',value):
        return False
    if re.search(r'\bне\s+(?:(?:будет|может|должен)\s+)?(?:отсутств|недоступ)|\bне\s+учитыва',value):return False
    pattern=(r'\b(?:отсутств\w*|недоступ\w*|не\s+будет|не\s+смог\w*|командиров\w*|больнич\w*)\b' if advisory else r'\b(?:отсутствует|отсутствуют|недоступен|недоступна|недоступны|не\s+будет)\b')
    return bool(re.search(pattern,value))


def _dates(messages, metadata):
    for message in reversed(messages):
        text=message.casefold().replace('ё','е')
        values=re.findall(r'(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)|(?<!\d)\d{1,2}[./]\d{1,2}[./]\d{4}(?!\d)',text)
        if values:
            try:
                parsed=[date.fromisoformat(v) if '-' in v else date(int(v[-4:]),int(re.split(r'[./]',v)[1]),int(re.split(r'[./]',v)[0])) for v in values]
            except ValueError:return None,'Укажите существующие даты с годом.'
            unique=list(dict.fromkeys(parsed))
            if len(unique)>2:return None,'Укажите одну дату или один диапазон «с YYYY-MM-DD по YYYY-MM-DD».'
            if len(unique)==2 and not re.search(r'\bс\s*\d.*\bпо\s*\d',text):return None,'Для диапазона явно укажите «с даты по дату».'
            begin,end=unique[0],unique[-1]
            if len(unique)==1 and re.search(r'недел',text):
                if begin.weekday()!=0:return None,'Для всей недели укажите её понедельник.'
                try:end=begin+timedelta(days=6)
                except OverflowError:return None,'Неделя выходит за допустимый диапазон дат.'
            if not 0<=(end-begin).days<366:return None,'Диапазон должен идти по порядку и занимать не более366дней.'
            return (begin,end),None
        if ((re.search(r'(?:эт\w*|текущ\w*)\s+недел',text) and re.search(r'следующ|будущ',text))
                or re.search(r'(?:эт\w*|текущ\w*)\s+(?:и|,)\s*(?:следующ\w*|будущ\w*)\s+недел',text)):
            return None,'Укажите один точный диапазон отсутствия вместо нескольких относительных недель.'
        relative=[]
        if re.search(r'(?:эт\w*|текущ\w*)\s+недел',text):relative.append(0)
        if re.search(r'(?:следующ\w*|будущ\w*)\s+недел',text):relative.append(7)
        if relative:
            first=(metadata or [{}])[0]
            if len(set(relative))!=1 or not first.get('weekStart') or not first.get('timezone'):
                return None,'Укажите точные даты отсутствия с годом: дата исходного сообщения или часовой пояс не подтверждены.'
            try:begin=date.fromisoformat(first['weekStart'])+timedelta(days=relative[0])
            except (ValueError,OverflowError):return None,'Укажите точные даты отсутствия с годом.'
            try:return (begin,begin+timedelta(days=6)),None
            except OverflowError:return None,'Неделя выходит за допустимый диапазон дат.'
    return None,'На какие даты отсутствует педагог? Укажите дату с годом или диапазон «с YYYY-MM-DD по YYYY-MM-DD».'


def _actor(profile):
    from .request_planning import canonical_manager
    actor=canonical_manager(profile)
    if actor.get('role') not in {'director','planner'}:raise HTTPException(403,'Ограничение отсутствия подтверждает директор или составитель')
    from .school_requests import require_bound_school
    require_bound_school(actor['schoolId'])
    return actor


def _context(connection,profile,target=None):
    """One domain snapshot; every hash is re-read in the final write transaction."""
    from .planning import PlanningService
    from .school_plan_imports import source_binding
    from .school_calendar import _calendar,_read_state
    state=_read_state(connection)
    calendar=_calendar(state,profile['schoolId'])
    draft=PlanningService._draft_snapshot(connection)
    binding={'schoolId':profile['schoolId'],'calendarRevision':calendar['revision'],
             'calendarHash':digest(calendar),'draftRevision':draft['revision'],
             'draftHash':digest(draft),'sourceBinding':source_binding(connection)}
    if target:
        from .experimental_edits import read_version
        version=read_version(connection,target,profile)
        from . import experimental_school as experiments
        raw=connection.execute('SELECT payload FROM timetable_draft WHERE id=1').fetchone()
        actual=experiments._current(json.loads(raw[0]) if raw else {'revision':0,'lessons':[]},experiments.load_source_package())
        if version['sourceBinding']!=actual:raise HTTPException(409,'Школьная сетка или документы изменились; выберите актуальный эксперимент.')
        plan=version['input']
        binding['targetSourceBinding']=deepcopy(version['sourceBinding'])
        binding.update(targetExperimentId=target,expectedRevision=version['revision'],expectedVersionHash=version['versionHash'])
    else:
        configured=PlanningService._config(connection)
        if configured['input'] is None:raise HTTPException(409,'Сначала сохраните нормативный план или выберите экспериментальный черновик.')
        plan=configured['input'];version=None
        binding.update(configRevision=configured['revision'])
    plan=PlanningInput.model_validate(plan).model_dump()
    binding['inputHash']=digest(plan)
    return state,calendar,binding,plan,version


def prepare(state, *, model_mode='deterministic'):
    profile=_actor(state['owner'])
    base={'revision':state.get('expectedRevision',0),'issues':[],'questions':[],
          'modelMode':model_mode,'absence_pending':True,'proposal':None,'proposal_hash':''}
    messages=state.get('user_messages',[state['user_message']])
    if re.search(r'\b(?:отмена|не\s+учитыва\w*|не\s+(?:будет\s+)?отсутств\w*)\b',messages[-1].casefold()):
        return {**base,'status':'rejected','message':'Сообщение об отсутствии отменено. Датированные ограничения и расписание не изменены.','decision':'reject'}
    if not any(literal_absence(message,advisory=True) for message in messages):
        message='Сообщите прямо, какой педагог и в какие даты отсутствует. Цитата или вопрос не создаёт ограничение.'
        return {**base,'status':'needs_clarification','message':message,'questions':[{'path':'absence.intent','message':message}]}
    try:
        with closing(sqlite3.connect(db.database_path().as_uri()+'?mode=ro',uri=True,timeout=15)) as connection:
            connection.execute('BEGIN')
            _,calendar,binding,plan,version=_context(connection,profile,state.get('targetExperimentId'))
    except HTTPException as error:
        if error.status_code in {401,403}:raise
        message=error.detail if isinstance(error.detail,str)else'Проверьте исходный план.'
        return {**base,'status':'blocked','message':message,'questions':[{'path':'absence.context','message':message}]}
    base['revision']=binding['expectedRevision'] if version else binding['draftRevision']
    if version and (binding['expectedRevision']!=state['expectedRevision'] or binding['expectedVersionHash']!=state['targetVersionHash']):
        return {**base,'status':'stale','message':'Выбранный эксперимент изменился; начните новый запрос.'}
    matches=[]
    for message in reversed(messages):
        text=canonical(message)
        matches=[teacher for teacher in plan['teachers'] if re.search(r'(?<!\w)'+re.escape(canonical(teacher['name']))+r'(?!\w)',text)
                 or re.search(r'(?<!\w)'+re.escape(canonical(teacher['id']))+r'(?!\w)',text)]
        if matches:break
    if len(matches)!=1:
        candidates=matches or plan['teachers']
        question={'path':'absence.teacher','message':'Укажите одного точного педагога из текущего плана. Одинаковые имена различаются идентификатором.',
                  'candidateIds':[t['id']for t in candidates[:20]],
                  'candidates':[{'id':t['id'],'label':t['name']+' · '+t['id']}for t in candidates[:20]]}
        return {**base,'status':'needs_clarification','message':question['message'],'questions':[question]}
    selected=matches[0]
    resolved,question=_dates(messages,state.get('date_metadata'))
    if question:return {**base,'status':'needs_clarification','message':question,'questions':[{'path':'absence.dates','message':question}]}
    begin,end=resolved
    period=begin-timedelta(days=begin.weekday())
    if version and (period.isoformat()!=plan['periodStart'] or end>period+timedelta(days=6)):
        message='Даты должны относиться к выбранной экспериментальной неделе. Для другой недели выберите другой вариант.'
        return {**base,'status':'needs_clarification','message':message,'questions':[{'path':'absence.dates','message':message}]}
    # A planning task is one week; longer absences are saved as dated ranges but
    # this calculation covers only their first selected week.
    plan['periodStart']=period.isoformat()
    school_days=sorted({s['day']for s in plan['slots'] if begin<=period+timedelta(days=s['day'])<=end})
    if calendar['config'] is not None:
        from .school_calendar import effective_calendar_snapshot
        snapshot=effective_calendar_snapshot(period.isoformat(),school_id=profile['schoolId'],state={ '_schoolCalendar':calendar })
        school_days=[d['weekday']for d in snapshot['days']if d['weekday']<6 and d['status']=='school' and begin.isoformat()<=d['date']<=end.isoformat()]
    assignments=version['assignments'] if version else plan['baseline']
    slots={s['id']:s for s in plan['slots']}
    by_event={f"{d['id']}_{n}":d for d in plan['demands']for n in range(d['weeklyCount'])}
    affected=sum(selected['id']in by_event[a['eventId']]['teacherIds'] and begin<=period+timedelta(days=slots[a['slotId']]['day'])<=end for a in assignments)
    count_scope='known_assignments'
    if not assignments:
        if begin<=period and end>=period+timedelta(days=5):
            affected=sum(d['weeklyCount']for d in plan['demands']if selected['id']in d['teacherIds']);count_scope='weekly_load'
        else:count_scope='unknown'
    summary={'teacherId':selected['id'],'teacherName':selected['name'],'startDate':begin.isoformat(),'endDate':end.isoformat(),
             'periodStart':period.isoformat(),'schoolDays':school_days,'affectedLessonCount':affected,
             'calendarRevision':calendar['revision'],'affectedLessonCountScope':count_scope}
    for key in ('configRevision','targetExperimentId','expectedRevision'):
        if key in binding:summary[key]=binding[key]
    proposal={'kind':'teacher_absence','absence':summary,'binding':binding}
    proposal['hash']=digest(proposal);summary['hash']=proposal['hash']
    # The public summary hash is excluded from its own canonical digest.
    return {**base,'status':'awaiting_approval','absence':summary,'proposal':proposal,'proposal_hash':proposal['hash'],
            'message':f"Подготовлено отсутствие: {selected['name']}, {begin.isoformat()} — {end.isoformat()}. Подтвердите сохранение датированного ограничения и отдельный пересчёт. Занятия не удаляются; сетка не применяется и не публикуется."}


def proposal_hash(proposal):
    value=deepcopy(proposal);value.pop('hash',None);value.get('absence',{}).pop('hash',None)
    return digest(value)


def apply(proposal,profile,task_id):
    """Commit only the absence, with an exact actor-bound durable receipt."""
    profile=_actor(profile)
    if proposal.get('kind')!='teacher_absence' or proposal_hash(proposal)!=proposal.get('hash'):
        raise HTTPException(409,'Предложение отсутствия изменилось')
    actor={k:profile.get(k)for k in ('schoolId','userId','membershipId','membershipRevision','role')}
    with closing(sqlite3.connect(db.database_path().as_uri()+'?mode=rw',uri=True,timeout=15)) as connection:
        connection.execute('BEGIN IMMEDIATE')
        try:
            from .school_calendar import _calendar,_read_state,_cas,_commit
            state=_read_state(connection);calendar=_calendar(state,profile['schoolId'])
            receipts=calendar.get('absenceReceipts',[])
            previous=next((r for r in receipts if r['taskId']==task_id),None)
            if previous:
                if previous['actor']!=actor or previous['hash']!=proposal['hash']:raise HTTPException(409,'Подтверждение отсутствия уже связано с другим запросом')
                connection.rollback();return deepcopy(previous['response'])
            if len(receipts)>=MAX_RECEIPTS:raise HTTPException(429,'Достигнут предел квитанций отсутствия; существующие записи сохранены')
            _,_,binding,_,_=_context(connection,profile,proposal['absence'].get('targetExperimentId'))
            if binding!=proposal['binding']:raise HTTPException(409,'План, календарь, источники или версия изменились; подготовьте отсутствие заново')
            absence=proposal['absence'];_cas(calendar,absence['calendarRevision'])
            record=ExceptionInput(startDate=absence['startDate'],endDate=absence['endDate'],kind='teacher_unavailable',
                targetId=absence['teacherId'],source='Подтверждённое сообщение составителя '+task_id,confirmed=True).model_dump()
            identity='absence_'+digest([actor,task_id,proposal['hash']])[:32]
            calendar['records']['exceptions'].append({**record,'id':identity,'archived':False})
            calendar.setdefault('teacherBindings',{})[absence['teacherId']]=absence['teacherName']
            response={'recordId':identity,'calendarRevision':calendar['revision']+1,'absence':deepcopy(absence)}
            calendar.setdefault('absenceReceipts',[]).append({'taskId':task_id,'actor':actor,'hash':proposal['hash'],'response':response,'proposal':deepcopy(proposal)})
            _commit(state,calendar,profile,'teacher_absence',identity)
            connection.execute('UPDATE demo_state SET payload=? WHERE id=1',(json.dumps(state,ensure_ascii=False),));connection.commit()
            return response
        except BaseException:connection.rollback();raise


def dispatch(proposal,profile,task_id):
    """Idempotent calculation after the separately durable approved fact."""
    verified=_actor(profile)
    absence=proposal['absence']
    with closing(sqlite3.connect(db.database_path().as_uri()+'?mode=ro',uri=True,timeout=15)) as connection:
        connection.execute('BEGIN')
        _,calendar,current,_,_=_context(connection,verified,absence.get('targetExperimentId'))
        original=proposal['binding']
        if any(value!=original.get(key) for key,value in current.items() if key not in {'calendarRevision','calendarHash'}):
            raise HTTPException(409,'Исходный план или выбранная версия изменились после подтверждения отсутствия.')
        actor={k:verified.get(k)for k in ('schoolId','userId','membershipId','membershipRevision','role')}
        record_id='absence_'+digest([actor,task_id,proposal['hash']])[:32]
        record=next((r for r in calendar['records']['exceptions'] if r['id']==record_id),None)
        if record is None or record['archived'] or record['targetId']!=absence['teacherId'] or record['startDate']!=absence['startDate'] or record['endDate']!=absence['endDate']:
            raise HTTPException(409,'Подтверждённое отсутствие изменилось; нужен новый запрос.')
    if absence.get('targetExperimentId'):
        from .experimental_school import recalculate_absence
        result=recalculate_absence(proposal,profile,task_id)
        return {'experimentalDraftId':result['id'],'status':'calculating',
                'actions':[{'id':'open_experiment','label':'Открыть пересчёт с отсутствием'}]}
    from .planning import get_service
    run=get_service().create_run({'expectedConfigRevision':absence['configRevision'],
        'expectedDraftRevision':proposal['binding']['draftRevision'],'mode':'repair','timeLimitSeconds':10,
        'periodStart':absence['periodStart'],'requestId':'absence_'+digest(task_id)[:48]},profile)
    return {'runId':run['id'],'status':'calculating','actions':[{'section':'runs','runId':run['id'],'label':'Открыть пересчёт с отсутствием'}]}
