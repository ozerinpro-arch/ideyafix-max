"""Read-only director dashboard; explicit scope, no personal pupil projections."""
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
import json
import re
import sqlite3
from typing import Literal
import unicodedata

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import ValidationError

from . import db, launch_runtime, school_ops, school_requests, timetable
from .auth import session_profile
from .replacement_outcomes import conducted_summary
from .school_analytics import aggregate_schedule
from .school_publications import read_active_snapshot
from .workspace import empty_school_state, verify_school_state

MAX_SNAPSHOT_BYTES = db.MAX_SNAPSHOT_BYTES
REQUEST_STATES = ('needs_clarification','submitted','waiting_reply','proposed','applied','rejected')
TASK_STATES = ('queued','running','waiting_reply','blocked','completed','cancelled','expired','stale','failed')
MANUAL_STATES = {'tasks':('todo','progress','done'),'documents':('draft','review','approved'),
                 'assignments':('todo','submitted','reviewed'),'absences':('open','resolved')}


def _director(profile):
    school_ops._school_only()
    profile = launch_runtime.actor(profile,'schedule.read')
    school_requests.require_bound_school(profile['schoolId'])
    if profile.get('staffRole',profile.get('role')) != 'director':
        raise HTTPException(403,'Панель директора доступна только действующему директору')
    return profile


def _text_bytes(column, postgres):
    return f'octet_length({column})' if postgres else f'length(CAST({column} AS BLOB))'


def _payload(connection, table, postgres):
    if not db.table_exists(connection, table): return None
    row = connection.execute(f'SELECT {_text_bytes("payload", postgres)} FROM {table} WHERE id=1').fetchone()
    if row is None: return None
    if row[0] > MAX_SNAPSHOT_BYTES: raise ValueError('Snapshot limit')
    value=json.loads(connection.execute('SELECT payload FROM '+table+' WHERE id=1').fetchone()[0])
    if not isinstance(value,dict):raise ValueError('Invalid domain snapshot')
    return value


def _snapshot(school, storage=None):
    """Read domain state, draft and publication in one consistent snapshot."""
    postgres = storage is not None
    if postgres:
        connection = storage.read_connection()
    else:
        path = db.database_path()
        if not path.exists(): return empty_school_state(),timetable._empty(),None
        connection = sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=15)
    with closing(connection):
        connection.execute('BEGIN')
        state = _payload(connection, 'demo_state', postgres)
        if state is None:state=empty_school_state()
        verify_school_state(state)
        draft = _payload(connection, 'timetable_draft', postgres)
        if draft is None:draft=timetable._empty()
        if db.table_exists(connection, 'school_publications'):
            size = connection.execute(
                f'SELECT {_text_bytes("snapshot_json", postgres)}, {_text_bytes("evidence_json", postgres)} '
                'FROM school_publications WHERE school_id=? ORDER BY version DESC LIMIT 1', (school,)
            ).fetchone()
            if size and any(type(n)is not int or n>MAX_SNAPSHOT_BYTES for n in size):raise ValueError('Publication limit')
        published = read_active_snapshot(school,connection=connection)
        return state,draft,published


def _validate_draft(draft):
    if (not isinstance(draft,dict) or type(draft.get('revision')) is not int
            or not 0 <= draft['revision'] <= timetable.MAX_REVISION
            or not isinstance(draft.get('lessons'),list) or len(draft['lessons']) > timetable.MAX_LESSONS):
        raise ValueError('Invalid schedule')
    ids=set()
    for row in draft['lessons']:
        if not isinstance(row,dict) or not isinstance(row.get('id'),str) or not row['id'] or row['id'] in ids:
            raise ValueError('Invalid lesson identity')
        ids.add(row['id'])
        if type(row.get('day')) is not int or not 0<=row['day']<=5 or type(row.get('slot')) is not int or not 1<=row['slot']<=20:
            raise ValueError('Invalid position')
        for key,limit in (('className',80),('subject',160)):
            if not isinstance(row.get(key),str) or len(row[key])>limit: raise ValueError('Invalid label')
            if any(unicodedata.category(c) in {'Cc','Cs'} for c in row[key]): raise ValueError('Invalid label')
        if not row['className']: raise ValueError('Invalid class')
        for key,limit in (('teachers',120),('rooms',80)):
            if not isinstance(row.get(key),list) or len(row[key])>8: raise ValueError('Invalid resources')
            if any(not isinstance(v,str) or not v or len(v)>limit or any(unicodedata.category(c) in {'Cc','Cs'} for c in v) for v in row[key]):
                raise ValueError('Invalid resource')
        for key in ('start','end'):
            if not isinstance(row.get(key),str) or not re.fullmatch(r'[0-9]{2}:[0-9]{2}',row[key]): raise ValueError('Invalid interval')
            # The shared interval parser validates hours/minutes as well.
            timetable._minutes(row[key])
        if timetable._minutes(row['end'])<=timetable._minutes(row['start']):raise ValueError('Invalid interval')
    return draft


def _published_draft(published):
    if published is None:return None
    if type(published.get('version')) is not int or published['version']<1: raise ValueError('Invalid publication')
    period = published.get('periodStart')
    if period is not None and (not isinstance(period,str) or date.fromisoformat(period).isoformat()!=period or date.fromisoformat(period).weekday()!=0):
        raise ValueError('Invalid publication period')
    return _validate_draft({'revision':published['draftRevision'],'sourceName':'','updatedAt':published.get('publishedAt'),
                            'lessons':published['lessons']})


def _query(source,day,class_name,teacher,subject):
    if not isinstance(source,str) or source not in {'draft','published'}:raise HTTPException(422,'Источник: draft или published')
    if day is not None and (type(day) is not int or not 0<=day<=5):raise HTTPException(422,'День должен быть от0 до5')
    for value,limit in ((class_name,80),(teacher,120),(subject,160)):
        if value is not None and (not isinstance(value,str) or not 1<=len(value)<=limit or any(unicodedata.category(c) in {'Cc','Cs'} for c in value)):
            raise HTTPException(422,'Некорректная точная метка фильтра')



def _interval_totals(intervals):
    """Reuse the existing room heatmap union for any one resource/time scope."""
    end=-1;occupied=0
    for start,stop in sorted(intervals):
        occupied+=max(0,stop-max(start,end));end=max(end,stop)
    return {'lessonCount':len(intervals),'scheduledMinutes':sum(b-a for a,b in intervals),
            'occupiedMinutes':occupied}


def _detailed_groups(lessons):
    """Selected-record participation; room occupancy is a day-wide interval union.

    Record counts retain shared-group rows, just like aggregate_schedule.
    Each teacher/room appears once per record; absent resources invent no group.
    Slot labels never partition the room/day union into double-counted pieces.
    """
    slots=defaultdict(lambda:{'lessonCount':0,'scheduledMinutes':0})
    named={key:defaultdict(lambda:{'lessonCount':0,'scheduledMinutes':0})
           for key in ('byClassDay','byTeacherDay','bySubjectDay')}
    room_days=defaultdict(list)
    for row in lessons:
        start,stop=timetable._minutes(row['start']),timetable._minutes(row['end'])
        duration=stop-start
        item=slots[row['slot']];item['lessonCount']+=1;item['scheduledMinutes']+=duration
        for key,names in (('byClassDay',(row['className'],)),
                          ('byTeacherDay',set(row['teachers'])),
                          ('bySubjectDay',(row['subject'],))):
            for name in names:
                item=named[key][(name,row['day'])]
                item['lessonCount']+=1;item['scheduledMinutes']+=duration
        for room in set(row['rooms']):room_days[(room,row['day'])].append((start,stop))
    return {'bySlot':[{'slot':slot,**values}for slot,values in sorted(slots.items())],
            **{key:[{'name':name,'day':day,**values}for (name,day),values in sorted(groups.items())]
               for key,groups in named.items()},
            'roomByDay':[{'room':room,'day':day,**_interval_totals(intervals)}
                         for (room,day),intervals in sorted(room_days.items())]}


def _heatmaps(lessons):
    cells=defaultdict(lambda:{'lessonCount':0,'scheduledMinutes':0})
    room_cells=defaultdict(list)
    for row in lessons:
        start,end=timetable._minutes(row['start']),timetable._minutes(row['end'])
        cell=cells[(row['day'],row['slot'])];cell['lessonCount']+=1;cell['scheduledMinutes']+=end-start
        for room in set(row['rooms']):room_cells[(room,row['day'],row['slot'])].append((start,end))
    rooms=[]
    for (room,day,slot),intervals in sorted(room_cells.items()):
        rooms.append({'room':room,'day':day,'slot':slot,**_interval_totals(intervals)})
    return ([{'day':day,'slot':slot,**values}for (day,slot),values in sorted(cells.items())],rooms)


def _rows_by_status(counts,states):return [{'status':status,'count':counts[status]}for status in states]


def _rounded(value):return float(value.quantize(Decimal('0.01'),rounding=ROUND_HALF_UP))


def _metric(rows,key,total):
    values=[Decimal(str(row[key])) for row in rows if row.get(key) is not None]
    return {'value':_rounded(sum(values)/len(values)) if values else None,'known':len(values),'unknown':total-len(values),
            'coveragePercent':_rounded(Decimal(len(values))*100/total) if total else None,
            'status':'available' if values else 'not_available'}


def _operations(state,school):
    ops=school_ops._ops(state); records=ops['records'];valid={};counts={};invalid_total=0
    for kind in school_ops.KINDS:
        # Legacy rows have no schoolId and belong to this host-bound domain.
        # Explicitly foreign rows are health diagnostics only: they must not
        # alter own-school active/archive counts, coverage, status or limits.
        items=[row for row in records[kind] if not isinstance(row,dict) or row.get('schoolId',school)==school]
        invalid_total+=len(records[kind])-len(items)
        if len(items)>school_ops.RECORD_LIMITS[kind]:raise ValueError('Manual registry limit')
        active=[];archived=invalid=0;validated=[];seen=set()
        for record in items:
            if not isinstance(record,dict) or type(record.get('archived')) is not bool:
                invalid+=1;continue
            if record['archived']:archived+=1;continue
            active.append(record)
            identifier=record.get('id')
            if not isinstance(identifier,str) or not 1<=len(identifier)<=80 or identifier in seen:
                invalid+=1;continue
            seen.add(identifier)
            if kind in MANUAL_STATES and record.get('status') not in MANUAL_STATES[kind]:
                invalid+=1;continue
            try:
                fields={k:record[k]for k in school_ops.MODELS[kind].model_fields if k in record}
                checked=school_ops.MODELS[kind].model_validate(fields).model_dump()
            except (ValidationError,ValueError,TypeError):invalid+=1;continue
            validated.append(checked)
        valid[kind]=validated;invalid_total+=invalid
        counts[kind]={'active':len(active),'archived':archived,'invalid':invalid}
        if kind in MANUAL_STATES:
            counts[kind]['byStatus']=_rows_by_status(Counter(r['status']for r in validated),MANUAL_STATES[kind])
    students=counts['students']['active']
    return {'scope':'school_all_manual_records','revision':ops['revision'],
            'records':{kind:counts[kind]for kind in MANUAL_STATES},
            'students':{'active':students,'averageGrade':_metric(valid['students'],'averageGrade',students),
                        'attendancePercent':_metric(valid['students'],'attendancePercent',students)},
            'teacherProfilesCount':len(valid['teacherProfiles']),'materialsCount':len(valid['materials']),
            'invalidRecordCount':invalid_total}


def _queues(state,school):
    items=school_requests._read_ns(state,school)['requests']
    if not isinstance(items,dict) or len(items)>school_requests.LIMIT:raise ValueError('Invalid request queue')
    counts=Counter();questions=invalid=0
    for item in items.values():
        if not isinstance(item,dict) or item.get('schoolId')!=school or item.get('status') not in REQUEST_STATES:
            invalid+=1;continue
        counts[item['status']]+=1
        question_rows=item.get('questions',[])
        if not isinstance(question_rows,list) or len(question_rows)>2000:raise ValueError('Invalid questions')
        questions+=sum(isinstance(q,dict) and q.get('state')=='open' for q in question_rows)
    tasks=state.get('_launchRuntime',{}).get('tasks',{})
    if not isinstance(tasks,dict) or len(tasks)>launch_runtime.MAX_RECORDS:raise ValueError('Invalid task queue')
    task_counts=Counter();invalid_tasks=0
    for task in tasks.values():
        if not isinstance(task,dict):invalid_tasks+=1;continue
        if task.get('schoolId')!=school:continue
        if task.get('state') not in TASK_STATES:invalid_tasks+=1;continue
        task_counts[task['state']]+=1
    return ({'scope':'school_all_requests','total':sum(counts.values()),'byStatus':_rows_by_status(counts,REQUEST_STATES),
             'awaitingDecision':counts['proposed'],'openQuestions':questions,'invalidRecordCount':invalid},
            {'scope':'school_launch_tasks','total':sum(task_counts.values()),'byStatus':_rows_by_status(task_counts,TASK_STATES),'invalidRecordCount':invalid_tasks})


def director_dashboard(profile,source='draft',day=None,class_name=None,teacher=None,subject=None,*,storage=None):
    profile=_director(profile);_query(source,day,class_name,teacher,subject)
    try:
        school = profile['schoolId']
        state,draft,published = _snapshot(school) if storage is None else _snapshot(school, storage)
        _validate_draft(draft)
        published_draft=_published_draft(published)
        selected=draft if source=='draft' else published_draft
        lessons=selected['lessons'] if selected else []
        options={'days':sorted({r['day']for r in lessons}),'classes':sorted({r['className']for r in lessons}),
                 'teachers':sorted({v for r in lessons for v in r['teachers']}),'subjects':sorted({r['subject']for r in lessons})}
        for value,key in ((class_name,'classes'),(teacher,'teachers'),(subject,'subjects')):
            if value is not None and value not in options[key]:raise HTTPException(422,'Точная метка не найдена в выбранной версии')
        filtered=[r for r in lessons if (day is None or r['day']==day) and (class_name is None or r['className']==class_name)
                  and (teacher is None or teacher in r['teachers']) and (subject is None or r['subject']==subject)]
        schedule=None
        if selected is not None:
            schedule=aggregate_schedule({**selected,'lessons':filtered})
            # Source file paths, parser/raw/source cells, pupil fields and issue
            # lesson IDs never leave this dashboard.
            schedule.pop('source',None)
            schedule['heatmap'],schedule['roomHeatmap']=_heatmaps(filtered)
            schedule.update(_detailed_groups(filtered))
        draft_full=aggregate_schedule(draft)['totals']
        published_full=aggregate_schedule(published_draft)['totals']if published_draft else None
        comparison={'scope':'school_full_versions',
                    'draft':{'revision':draft['revision'],'lessonCount':draft_full['lessonCount'],'scheduledMinutes':draft_full['scheduledMinutes']},
                    'published':{'version':published['version'],'draftRevision':published['draftRevision'],'periodStart':published.get('periodStart'),
                                 'lessonCount':published_full['lessonCount'],'scheduledMinutes':published_full['scheduledMinutes']} if published else None,
                    'lessonCountDelta':draft_full['lessonCount']-published_full['lessonCount'] if published_full else None}
        requests,workflow=_queues(state,profile['schoolId'])
        result={'schemaVersion':1,'source':{'kind':source,'available':selected is not None,
                'revision':selected['revision']if selected else None,'publicationVersion':published['version']if source=='published'and published else None,
                'periodStart':published.get('periodStart')if source=='published'and published else None},
                'filters':{'applied':{'day':day,'className':class_name,'teacher':teacher,'subject':subject},'options':options},
                'schedule':schedule,'comparison':comparison,'operations':_operations(state,profile['schoolId']),'requests':requests,'workflow':workflow,
                'replacementDelivery':conducted_summary(state,profile['schoolId']),
                'notes':['Минуты — длительность записей; у педагогов и кабинетов — участие. Это не нормативная нагрузка и не процент использования мощности.',
                         'Фильтры относятся только к выбранному расписанию. Сравнение версий, реестры и очереди — целиком своей школы.',
                         'Оценка и посещаемость — среднее введённых значений известных записей; неизвестное не считается нулём. Исторических трендов и прогнозов нет.']}
        _director(profile)  # current authority must still hold at final projection
        return result
    except HTTPException:raise
    except (sqlite3.Error,ValueError,KeyError,TypeError,AttributeError,OverflowError):
        raise HTTPException(503,'Не удалось прочитать текущую школьную сводку. Данные не изменялись; повторите загрузку.')from None


def router_for(storage=None):
    routes = APIRouter(prefix='/api/school/analytics', tags=['director analytics'])

    @routes.get('/director')
    def director_analytics(source: Literal['draft', 'published'] = 'draft',
                           day: int | None = Query(default=None, ge=0, le=5),
                           className: str | None = Query(default=None, min_length=1, max_length=80),
                           teacher: str | None = Query(default=None, min_length=1, max_length=120),
                           subject: str | None = Query(default=None, min_length=1, max_length=160),
                           profile: dict = Depends(session_profile)):
        return director_dashboard(profile, source=source, day=day, class_name=className,
                                  teacher=teacher, subject=subject, storage=storage)

    return routes


router = router_for()
