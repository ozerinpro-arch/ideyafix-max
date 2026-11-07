"""Read-only weekday projection of an exact saved experiment revision.

A date selection is not a calendar change, solver run, approval or publication.
"""
from contextlib import closing
from copy import deepcopy
from datetime import date,timedelta
import json,sqlite3
from fastapi import APIRouter,Depends,HTTPException,Path as APIPath
from pydantic import Field,field_validator
from . import db,experimental_edits as edits,experimental_school as experiments
from .auth import session_profile
from .scheduling_models import StrictModel,canonical
from .school_calendar import effective_calendar_snapshot

router=APIRouter(tags=['weekday preview'],route_class=experiments.SafeExperimentalRoute)

class WeekPreviewInput(StrictModel):
    expectedRevision:int=Field(ge=1,le=2**31-1)
    expectedVersionHash:str=Field(pattern=r'^[a-f0-9]{64}$')
    startDate:str=Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    schoolDays:list[int]=Field(min_length=1,max_length=6)

    @field_validator('startDate')
    @classmethod
    def exact_date(cls,value):
        target=date.fromisoformat(value)
        monday=target-timedelta(days=target.weekday())
        try:monday+timedelta(days=6)
        except OverflowError:raise ValueError('Неделя выходит за допустимый диапазон дат.')from None
        return value

    @field_validator('schoolDays')
    @classmethod
    def weekdays(cls,value):
        if any(type(day)is not int or not 0<=day<=5 for day in value)or len(set(value))!=len(value):
            raise ValueError('Выберите неповторяющиеся дни с понедельника по субботу.')
        return sorted(value)


def preview(experiment_id,body,profile):
    edits._actor(profile)
    body=body if isinstance(body,WeekPreviewInput)else WeekPreviewInput.model_validate(body)
    path=db.database_path()
    if not path.is_file():raise HTTPException(404,'Выбранная версия не найдена.')
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=15))as connection:
        connection.execute('BEGIN')
        version=edits.read_version(connection,experiment_id,profile)
        if version['revision']!=body.expectedRevision or version['versionHash']!=body.expectedVersionHash:
            raise HTTPException(409,'Выбранная версия изменилась. Обновите её перед просмотром новых дат.')
        if not version.get('sourcesCurrent',False)or not version.get('calendarCurrent',False)or not version['validation']['valid']:
            raise HTTPException(409,'Исходные данные или календарь выбранной версии изменились либо содержат конфликты. Нужен проверенный шаблон.')
        state=json.loads(connection.execute('SELECT payload FROM demo_state WHERE id=1').fetchone()[0])
        plan=version['input'];slots={s['id']:s for s in plan['slots']}
        start=date.fromisoformat(body.startDate);monday=start-timedelta(days=start.weekday())
        teaching=[(monday+timedelta(days=day)).isoformat()for day in body.schoolDays if day>=start.weekday()]
        allowed=set(teaching)
        assignments=[a for a in version['assignments']if(monday+timedelta(days=slots[a['slotId']]['day'])).isoformat()in allowed]
        projection=experiments._projection(plan,assignments,version['provenance'],monday.isoformat())
        reverse={internal:physical for physical,internal in version['eventIdMap'].items()}
        for row in projection['lessons']:
            row['eventId']=reverse[row['eventId']]
            row['id']='exp_l_'+experiments._hash([row['eventId'],row['className']])[:32]
        projection['weekDays']=[day for day in body.schoolDays if day>=start.weekday()]
        names={canonical(t['name']):t['id']for t in plan['teachers']}
        bindings=state.get('_schoolCalendar',{}).get('teacherBindings',{})
        aliases={key:names[canonical(name)]for key,name in bindings.items()if canonical(name)in names}
        target=effective_calendar_snapshot(monday.isoformat(),school_id=profile['schoolId'],state=state,
            teacher_ids={t['id']for t in plan['teachers']},room_ids={r['id']for r in plan['rooms']},teacher_aliases=aliases)
        warnings=[]
        def warn(code,message,event_ids=()):
            warnings.append({'code':code,'message':message,'eventIds':sorted(set(event_ids))})
        for issue in target['readiness']['blockers']:
            warn('target_'+issue['code'],issue['message'])
        closed=[d['date']for d in target['days']if d['status']=='closed'and d['date']in allowed]
        if closed:warn('target_closed_dates','В сохранённом календаре закрыты выбранные даты: '+', '.join(closed)+'. Выбор для просмотра календарь не изменяет.')
        events={f"{d['id']}_{n}":d for d in plan['demands']for n in range(d['weeklyCount'])}
        outside,phase,unavailable,bells_differ=[],[],[],[]
        bells={(b['day'],b['slot']):(b['start'],b['end'])for b in target['bells']}
        for assignment in assignments:
            slot=slots[assignment['slotId']];stamp=(monday+timedelta(days=slot['day'])).isoformat();demand=events[assignment['eventId']];physical=reverse[assignment['eventId']]
            if(demand['activeFrom']and stamp<demand['activeFrom'])or(demand['activeUntil']and stamp>demand['activeUntil']):outside.append(physical)
            if demand['weekTypes']and canonical(target['weekType'])not in{canonical(t)for t in demand['weekTypes']}:phase.append(physical)
            if(slot['day'],slot['slot'])in bells and bells[(slot['day'],slot['slot'])]!=(slot['start'],slot['end']):bells_differ.append(physical)
            for event in target['unavailable']:
                resource=aliases.get(event['targetId'],event['targetId'])if event['kind']=='teacher_unavailable'else event['targetId']
                involved=resource in demand['teacherIds']if event['kind']=='teacher_unavailable'else resource==assignment['roomId']
                if involved and event['date']==stamp and slot['start']<(event['endTime']or'24:00')and(event['startTime']or'00:00')<slot['end']:unavailable.append(physical)
        if outside:warn('target_load_dates','Часть выбранных занятий вне дат действия нагрузки шаблона. Актуальная фаза требует уточнения.',outside)
        if phase:warn('target_load_phase','Тип новой недели не подтверждает фазу части выбранной нагрузки.',phase)
        if unavailable:warn('target_resource_unavailable','В новом календаре есть недоступность педагога или кабинета на время выбранных занятий.',unavailable)
        if bells_differ:warn('target_bells_differ','Звонки нового календаря отличаются от времени шаблона. В предпросмотре сохранено исходное время.',bells_differ)
        edits._actor(profile)
        return {'kind':'weekday_template_preview','persisted':False,'startDate':body.startDate,'weekStart':monday.isoformat(),'schoolDays':body.schoolDays,
            'teachingDates':teaching,'source':{'experimentId':experiment_id,'revision':version['revision'],'versionHash':version['versionHash'],
                'periodStart':version['periodStart'],'mode':'working_assumptions','physicalEventCount':len(version['physicalAssignments']),
                'classRowCount':len(version['projection']['lessons'])},
            'counts':{'teachingDates':len(teaching),'physicalEvents':len(assignments),'classRows':len(projection['lessons'])},'projection':projection,
            'targetCalendar':{'revision':target['calendarRevision'],'ready':target['readiness']['ready'],'days':deepcopy(target['days'])},
            'targetConditionsVerified':False,'warnings':warnings,
            'notice':'Сохранены дни недели, время, педагоги и кабинеты выбранной версии. Это только просмотр: новая сетка, отмены и допзанятия не сохранялись; согласование и публикация не выполнялись.',
            'limitations':['Дни выбраны для предпросмотра и не меняют школьный календарь.','Выбранный шаблон содержит рабочие предположения; он не становится фактическим расписанием новой четверти.',
                'Актуальность нагрузки и фазы преподавателей, звонков, доступности и школьных условий на новые даты требует отдельной проверки. Недельная нагрузка не сжималась в оставшиеся дни.']}


@router.post('/{experiment_id}/week-preview')
def preview_route(body:WeekPreviewInput,experiment_id:str=APIPath(pattern=r'^exp_[a-f0-9]{32}$'),profile=Depends(session_profile)):
    return preview(experiment_id,body,profile)
