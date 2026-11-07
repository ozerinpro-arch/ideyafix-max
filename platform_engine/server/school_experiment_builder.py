"""Independent source-bound experiment compiled by the native generic compiler.

Only the platform's explicitly fictional source package is selected. Existing
confirmed constraints, pins, room domains and chains are retained; historical
people, cells and assumptions from another organisation are never imported.
"""
from collections import defaultdict
from contextlib import closing
from copy import deepcopy
import json
import sqlite3
from hashlib import sha256

from . import db
from .school_plan_sources import SourcePackage, SourceResolutions, compile_source_plan, _digest
from .scheduling_models import PlanningInput, canonical


def _hash(value):
    return sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def _id(prefix,value):
    return prefix+_hash(value)[:18]


def project_arduino_source_window(package,plan,provenance):
    """No historical course window is authority for this independent package."""
    return {'effectiveInput':deepcopy(plan),'blockers':[],'diagnostics':[],'arduinoWindow':None}


def build_experimental_plan(package,draft,period_start):
    source=SourcePackage.model_validate(package)
    if source.packageId!='ideyafix-fictional-inputs-v1':
        raise ValueError('Для этого пакета требуется явный независимый адаптер источников')
    with closing(sqlite3.connect(db.database_path().as_uri()+'?mode=ro',uri=True)) as connection:
        row=connection.execute('SELECT input_json FROM planning_config WHERE id=1').fetchone()
    if row is None:raise ValueError('Нужна явно заполненная нормативная модель')
    plan=PlanningInput.model_validate(json.loads(row[0])).model_dump(mode='json')
    if not plan['confirmed']:raise ValueError('Нужны явно подтверждённые ограничения модели')
    plan['periodStart']=period_start
    teachers={canonical(t['name']):t for t in plan['teachers']}
    groups={g['id']:g for g in plan['groups']}
    resolutions={'classMap':{},'groupMap':{},'teacherMap':{},'roomIds':{},'existingProvenance':[]}
    remap={};used=set();explicit={}
    for cell in source.cells:
        if len(cell.normalizedSubjects)!=1:
            raise ValueError('Составной предмет требует явного разнесения исходных назначений')
        part=cell.normalizedSubjects[0]
        teacher=teachers.get(canonical(cell.teacher))
        if teacher is None:raise ValueError('Педагог источника не сопоставлен подтверждённой модели')
        candidates=[d for d in plan['demands'] if canonical(d['subject'])==canonical(part.subject)
            and d['weeklyCount']==part.weeklyCount and d['teacherIds']==[teacher['id']]
            and all(canonical(groups[g]['className'])==canonical(cell.classLabel) for g in d['groupIds'])]
        if len(candidates)!=1 or candidates[0]['id'] in used:
            raise ValueError('Назначение источника требует однозначной явной связи с нормативной нагрузкой')
        demand=candidates[0];used.add(demand['id'])
        identifier='src_'+_digest([source.packageId,[cell.sourceCell],part.subject])[:28]
        remap[demand['id']]=identifier;explicit[identifier]=deepcopy(demand)
        resolutions['classMap'][cell.classLabel]=groups[demand['groupIds'][0]]['className']
        resolutions['groupMap'][cell.sourceCell]=demand['groupIds']
        resolutions['teacherMap'][cell.teacher]=teacher['id']
        resolutions['roomIds'][cell.sourceCell]=demand['roomIds']
        resolutions['existingProvenance'].append({'demandId':identifier,'sourceCells':[cell.sourceCell],
            'subject':part.subject,'weeklyCount':part.weeklyCount,'teacherIds':demand['teacherIds']})
    events={f"{old}_{n}":f"{new}_{n}" for old,new in remap.items()
            for n in range(next(d['weeklyCount'] for d in plan['demands'] if d['id']==old))}
    for demand in plan['demands']:demand['id']=remap.get(demand['id'],demand['id'])
    for field in ('baseline','pins'):
        for assignment in plan[field]:assignment['eventId']=events.get(assignment['eventId'],assignment['eventId'])
    for chain in plan['eventChains']:chain['eventIds']=[events.get(event,event) for event in chain['eventIds']]
    compiled=compile_source_plan(package,SourceResolutions.model_validate(resolutions).model_dump(mode='json'),plan,period_start)
    if compiled['unresolved'] or compiled['excluded']:
        raise ValueError('Исходные данные или ограничения требуют явного уточнения')
    result=compiled['input'];result['confirmed']=True
    result=PlanningInput.model_validate(result).model_dump(mode='json')
    by_demand={item['id']:item for item in result['demands']}
    teacher_by_id={item['id']:item['name'] for item in result['teachers']}
    group_by_id={item['id']:item for item in result['groups']}
    provenance=[]
    compiled_by_id={item['demandId']:item for item in compiled['provenance']}
    for identifier,demand in by_demand.items():
        original=compiled_by_id.get(identifier,{})
        provenance.append({**original,'demandId':identifier,'sourceCells':original.get('sourceCells',[]),
            'subject':demand['subject'],'weeklyCount':demand['weeklyCount'],
            'teacherNames':[teacher_by_id[t] for t in demand['teacherIds']],
            'classNames':sorted({group_by_id[g]['className'] for g in demand['groupIds']}),
            'groupIds':demand['groupIds'],'sourceRows':[],'sourceConditions':[],
            'basis':'explicit_confirmed_native_model','assumptionIds':[]})
    classes=sorted({group['className'] for group in result['groups']})
    assumption={'id':_id('hyp_',period_start),'category':'computation','label':'Независимый эксперимент Идеяфикс',
        'value':{'confirmedConstraintsReused':True,'sourceCompiler':'compile_source_plan'},
        'basis':'Полностью вымышленные источники и явно подтверждённые собственные ограничения. Эксперимент не активирует нормативы.',
        'sourceCells':sorted(cell.sourceCell for cell in source.cells),'status':'working_assumption'}
    count=sum(d['weeklyCount'] for d in result['demands'])
    return {'input':result,'provenance':provenance,'assumptions':[assumption],'warnings':[],'deferredLoads':[],
        'metadata':{'mode':'working_assumptions','normativeActivation':False,'publicationAllowed':False,
            'userAuthorizedComputation':True,'packageHash':_hash(package),'draftHash':_hash(draft['lessons']),
            'independentSourceAdapter':'native_compile_source_plan'},
        'coverage':{'sourceCells':len(source.cells),'coveredCells':len(source.cells),'excludedCells':[],
            'activeSourceCellCount':len(source.cells),'deferredSourceCellCount':0,
            'activeSourceCells':sorted(cell.sourceCell for cell in source.cells),'deferredSourceCells':[],
            'currentEventCount':count,'eventCount':count,'classes':len(classes),'classNames':classes,
            'teachers':len(result['teachers']),'rooms':len(result['rooms']),'confirmedSharedLinks':0,
            'extraRows':[],'extraRowHours':0,'extraRowDemandIds':[],'baselineEvents':len(result['baseline']),
            'legacyMatchingEvents':0,'assumptionCount':1}}
