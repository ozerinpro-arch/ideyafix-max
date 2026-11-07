"""Read-only schedule aggregates for current managers; no pupil registry access."""
from collections import Counter,defaultdict
import json
import sqlite3
from fastapi import APIRouter,Depends,HTTPException
from .auth import session_profile
from . import launch_runtime,school_ops,school_requests,timetable

router=APIRouter(prefix='/api/school/analytics',tags=['schedule analytics'])


def _manager(profile):
    school_ops._school_only()
    profile=launch_runtime.actor(profile,'schedule.read')
    school_requests.require_bound_school(profile['schoolId'])
    if profile.get('staffRole',profile.get('role'))not in {'director','planner'}:
        raise HTTPException(403,'Аналитика расписания доступна директору и завучу')
    return profile


def _checked_rows(lessons):
    """Reuse the internal batch provenance validator before trusting exceptions.

    Draft lesson IDs remain opaque here; temporary IDs satisfy the write-only
    batch envelope without imposing its ID syntax on existing read records.
    The authoritative event/group shape and shared-event consistency rules are
    reused unchanged. Parser warnings stay on the original read-only copies.
    """
    rows=[];provenance=[]
    for index,lesson in enumerate(lessons):
        row=dict(lesson)
        internal=any(key in row for key in ('_planningEventId','_planningGroups'))
        public=any(key in row for key in ('planningEventId','planningGroups'))
        if internal or public:
            event=row.get('_planningEventId')if internal else row.get('planningEventId')
            groups=row.get('_planningGroups')if internal else row.get('planningGroups')
            if event is None or groups is None:
                raise ValueError('Incomplete planning provenance')
            if public and internal and (row.get('planningEventId')!=event or row.get('planningGroups')!=groups):
                raise ValueError('Contradictory planning provenance')
            row.update(_planningEventId=event,_planningGroups=groups)
            provenance.append({'id':f'analytics_{index}',
                **{key:row[key]for key in timetable.EditableLessonInput.model_fields},
                '_planningEventId':event,'_planningGroups':groups})
        rows.append(row)
    if provenance:
        try:
            timetable._batch_payload(provenance,0,'Analytics provenance validation')
        except HTTPException as error:
            raise ValueError('Invalid planning provenance')from error
    return rows


def aggregate_schedule(draft):
    """Aggregate the same draft snapshot/directory/checks as the schedule editor.

    Minutes are duration of records (participation for teachers/rooms), never
    normative hours, utilization percentage, attendance or approved publication.
    """
    check_rows=_checked_rows(draft['lessons'])
    directory=school_ops._directory(draft)
    total_minutes=0;by_day=defaultdict(lambda:{'lessonCount':0,'scheduledMinutes':0})
    subject_counts=Counter();loads={key:Counter()for key in ('classes','teachers','rooms','subjects')}
    for lesson in draft['lessons']:
        duration=timetable._minutes(lesson['end'])-timetable._minutes(lesson['start'])
        if duration<=0:raise ValueError('Invalid interval')
        total_minutes+=duration
        by_day[lesson['day']]['lessonCount']+=1;by_day[lesson['day']]['scheduledMinutes']+=duration
        loads['classes'][lesson['className']]+=duration
        for key,field in (('teachers','teachers'),('rooms','rooms')):
            for name in set(lesson[field]):loads[key][name]+=duration
        subject_counts[lesson['subject']]+=1;loads['subjects'][lesson['subject']]+=duration
    def rows(kind):
        if kind=='subjects':items=[{'name':name,'lessonCount':count}for name,count in sorted(subject_counts.items())]
        else:items=directory[kind]
        return [{'name':item['name'],'lessonCount':item['lessonCount'],'scheduledMinutes':loads[kind][item['name']]}for item in items]
    # Revalidate actual rows, never trust a stale persisted `issues` cache.
    issues=timetable.compute_issues(check_rows);kinds=Counter(i['kind']for i in issues)
    lessons=draft['lessons']
    return {'source':{'kind':'draft','isPublished':False,'revision':draft['revision'],
            'sourceName':draft['sourceName'],'updatedAt':draft['updatedAt']},
        'totals':{'lessonCount':len(lessons),'classCount':len(directory['classes']),
            'teacherCount':len(directory['teachers']),'roomCount':len(directory['rooms']),
            'subjectCount':len(subject_counts),'scheduledMinutes':total_minutes},
        'completeness':{'missingTeacherLessons':sum(not x['teachers']for x in lessons),
            'missingRoomLessons':sum(not x['rooms']for x in lessons),
            'multiTeacherLessons':sum(len(set(x['teachers']))>1 for x in lessons),
            'multiRoomLessons':sum(len(set(x['rooms']))>1 for x in lessons)},
        'checks':{'issueCount':len(issues),'errorCount':sum(i['severity']=='error'for i in issues),
            'warningCount':sum(i['severity']=='warning'for i in issues),
            'overlapIssueCount':sum(kinds[k]for k in ('class','teacher','room')),
            'affectedLessonCount':len({ident for issue in issues for ident in issue['lessonIds']}),
            'byKind':[{'kind':kind,'count':count}for kind,count in sorted(kinds.items())]},
        'byDay':[{'day':day,**values}for day,values in sorted(by_day.items())],
        'byClass':rows('classes'),'byTeacher':rows('teachers'),'byRoom':rows('rooms'),'bySubject':rows('subjects')}


@router.get('/schedule')
def schedule_analytics(profile=Depends(session_profile)):
    _manager(profile)
    try:
        _,draft=school_ops._snapshot(raw_draft=True)
        return aggregate_schedule(draft)
    except (sqlite3.Error,json.JSONDecodeError,KeyError,TypeError,ValueError,AttributeError,OverflowError):
        raise HTTPException(503,'Не удалось прочитать черновик расписания. Повторите загрузку; если ошибка сохраняется, обратитесь к администратору.')from None
