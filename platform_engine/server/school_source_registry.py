"""Source evidence only: never an active PlanningInput or timetable mutation.

The server builds every fact from its bundled package and a bounded read-only
grid snapshot. Unknown constraints remain null; source observations and the
explicitly authorised count hypotheses do not confirm a scheduling model.
"""
from copy import deepcopy
import json

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from . import db
from .school_ops import _snapshot
from .school_plan_imports import _actor
from .school_plan_sources import load_source_package, source_conditions
from .scheduling_models import canonical

NAMESPACE = '_schoolSourceRegistry'
MAX_REVISION = 2**31 - 1
MAX_RECEIPTS = 128
MAX_SNAPSHOTS = 16
MAX_BYTES = 4 * 1024 * 1024


class RegistrySaveInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, str_strip_whitespace=True)
    expectedRegistryRevision: int = Field(ge=0, le=MAX_REVISION)
    packageHash: str = Field(pattern=r'^[a-f0-9]{64}$')
    draftRevision: int = Field(ge=0, le=MAX_REVISION)
    draftHash: str = Field(pattern=r'^[a-f0-9]{64}$')
    requestId: str = Field(min_length=16, max_length=80, pattern=r'^[A-Za-z0-9_-]+$')


def _hash(value):
    from .planning import _hash as digest
    return digest(value)


def _stored(state, school_id):
    value = state.get(NAMESPACE)
    if value is None:
        return {'schoolId':school_id, 'version':1, 'revision':0, 'mode':'source_only', 'snapshots':[], 'receipts':[]}
    if (not isinstance(value,dict) or value.get('schoolId') != school_id or value.get('version') != 1
            or value.get('mode') != 'source_only' or type(value.get('revision')) is not int
            or not 0 <= value['revision'] <= MAX_REVISION
            or not isinstance(value.get('snapshots'),list) or len(value['snapshots']) > MAX_SNAPSHOTS
            or not isinstance(value.get('receipts'),list) or len(value['receipts']) > MAX_RECEIPTS):
        raise HTTPException(503,'Реестр исходных фактов требует проверки; сохранение остановлено.')
    revisions = set()
    for snapshot in value['snapshots']:
        if (not isinstance(snapshot,dict) or not isinstance(snapshot.get('registry'),dict)
                or snapshot.get('hash') != _hash(snapshot['registry'])
                or type(snapshot['registry'].get('revision')) is not int
                or snapshot['registry']['revision'] in revisions
                or snapshot['registry'].get('schoolId') != school_id
                or snapshot['registry'].get('mode') != 'source_only'):
            raise HTTPException(503,'Сохранённые исходные факты требуют проверки.')
        revisions.add(snapshot['registry']['revision'])
    if (value['revision'] and value['revision'] not in revisions) or (not value['revision'] and revisions):
        raise HTTPException(503,'Версия реестра исходных фактов требует проверки.')
    keys = set()
    for receipt in value['receipts']:
        if (not isinstance(receipt,dict) or not isinstance(receipt.get('key'),str)
                or receipt['key'] in keys or receipt.get('revision') not in revisions
                or not isinstance(receipt.get('fingerprint'),str)):
            raise HTTPException(503,'Квитанции исходных фактов требуют проверки.')
        keys.add(receipt['key'])
    return deepcopy(value)


def _bounded_draft(draft):
    # No raw source text, notes, identities/accounts or document contents.
    if (not isinstance(draft,dict) or type(draft.get('revision')) is not int
            or not isinstance(draft.get('lessons'),list) or len(draft['lessons']) > 10000):
        raise HTTPException(503,'Черновик требует проверки перед сохранением источников.')
    keys = ('id','className','day','slot','start','end','subject','teachers','rooms')
    try:
        return {'revision':draft['revision'],'lessons':[{key:deepcopy(row[key]) for key in keys} for row in draft['lessons']]}
    except (TypeError,KeyError):
        raise HTTPException(503,'Черновик требует проверки перед сохранением источников.') from None


def build_registry(package, draft, school_id, revision=0):
    """Pure evidence builder; no compiler, solver, inferred capacity or access."""
    bounded = _bounded_draft(draft)
    binding = {'packageHash':_hash(package),'draftRevision':bounded['revision'],'draftHash':_hash(bounded)}
    cells, rows = package['cells'], package['sourceRows']
    names = sorted({cell['teacher'] for cell in cells} | {row['teacher'] for row in rows})
    teacher_ids = {name:'source_teacher_'+_hash(name)[:16] for name in names}
    teachers = [{'id':teacher_ids[name], 'name':name,
        'sourceCells':sorted(cell['sourceCell'] for cell in cells if cell['teacher'] == name),
        'sourceRows':sorted(row['row'] for row in rows if row['teacher'] == name),
        'subjectsObserved':sorted({cell['rawSubject'] for cell in cells if cell['teacher'] == name}
            | {row['rawSubject'] for row in rows if row['teacher'] == name}),
        'maxDaily':None,'maxWeekly':None,'maxConsecutive':None,'qualifications':None} for name in names]
    labels = sorted({cell['classLabel'] for cell in cells})
    classes = [{'id':'source_class_'+_hash(label)[:16],'label':label,'size':None} for label in labels]
    observed_classes = sorted({row['className'] for row in bounded['lessons']})
    aliases = []
    for label in labels:
        folded = canonical(label)
        if not folded.endswith('б') or not folded[:-1].isdigit(): continue
        matches = [name for name in observed_classes if canonical(name) == folded[:-1]+'в']
        if len(matches) == 1:
            aliases.append({'sourceClass':label,'targetClass':matches[0], 'status':'tentative',
                'requiresConfirmation':True,'basis':'legacy_label_observation'})
    room_names = sorted({name for row in bounded['lessons'] for name in row['rooms'] if name})
    rooms = [{'id':'observed_room_'+_hash(name)[:16],'name':name,'capacity':None,'status':'observed_only'} for name in room_names]
    source_files = {item['id']:item for item in package['sources']}
    shared = []
    for observation in package['groupObservations']:
        evidence = observation.get('evidence',{})
        origin = source_files.get(evidence.get('sourceId'),{})
        if (observation.get('status') == 'visually_confirmed_structural_link'
                and observation.get('relation') == 'horizontal_shared_annotation'
                and observation.get('verified_visually') is True and origin.get('sha256')
                and evidence.get('sourceSha256') == origin['sha256']):
            shared.append({**deepcopy(observation),'size':None,'memberIds':None})
    hypotheses = []
    for cell in cells:
        if cell['sourceCell'] in package.get('confirmedCounts', {}):
            continue
        proposed = (4 if cell['sourceCell'] == 'S44' and cell['rawCount'] == '4/2'
            and cell['rawSubject'] == 'Информатика' else
            2 if cell['sourceCell'] in {'D103','E103','F103','G103','H103','I103'}
            and cell['rawCount'] == '2/1' and cell['rawSubject'] == 'Труд' else None)
        if proposed is not None:
            hypotheses.append({**{key:cell[key] for key in ('sourceCell','classLabel','rawCount','rawSubject')},
                'weeklyCount':proposed,'status':'hypothesis','requiresConfirmation':True,
                'reason':'Разрешённая пользователем рабочая гипотеза; требуется проверка до активации нормативов.'})
    result = {'schoolId':school_id,'version':1,'revision':revision,'mode':'source_only',
        'packageId':package['packageId'],'asOf':package['asOf'],'binding':binding,
        'teachers':teachers,'classes':classes,'classAliases':aliases,'rooms':rooms,
        'assignments':[{**deepcopy(cell),'teacherId':teacher_ids[cell['teacher']]} for cell in cells],
        'sharedGroups':shared,'countHypotheses':hypotheses,
        'conditions':source_conditions(package),
        **{key:deepcopy(package.get(key,[] if key != 'confirmedCounts' else {})) for key in
            ('sources','sourceRows','groupObservations','confirmedCorrections','confirmedCounts','countPatterns','ruleUpdates','curriculumObservations','unresolvedQuestions')},
        'unknowns':[{'code':code,'label':label} for code,label in (
            ('calendar','Учебный календарь и исключения'),('bells','Действующие звонки'),
            ('group_membership','Состав, размеры и пересечения групп'),('room_capacity','Вместимость и разрешённые кабинеты'),
            ('teacher_limits','Квалификации и пределы нагрузки педагогов'),
            ('class_aliases','Соответствие обозначений классов'),
            ('fraction_counts','Проверка гипотез дробных часов') if hypotheses else ('module_cycles','Фазы чередующихся курсов'))]}
    result['counts'] = {key:len(result[key]) for key in ('teachers','classes','classAliases','rooms','assignments','sharedGroups','countHypotheses','conditions','confirmedCounts')}
    return result


def _result(registry):
    return {'saved':True,'registry':deepcopy(registry),'revision':registry['revision'],
        'binding':deepcopy(registry['binding']),'stale':False}


def read_registry(profile):
    _actor(profile)
    state, draft = _snapshot(raw_draft=True)
    stored = _stored(state,profile['schoolId'])
    preview = build_registry(load_source_package(),draft,profile['schoolId'],stored['revision'])
    registry = next((item['registry'] for item in stored['snapshots'] if item['registry']['revision'] == stored['revision']),None)
    return {'saved':registry is not None,'registry':deepcopy(registry),'preview':preview,
        'binding':deepcopy(preview['binding']),'revision':stored['revision'],
        'stale':registry is not None and registry['binding'] != preview['binding']}


def save_registry(body, profile):
    _actor(profile)
    if not isinstance(body,RegistrySaveInput): body = RegistrySaveInput.model_validate(body)
    from .planning import _actor as manager
    actor = manager(profile)
    key = _hash([*actor,body.requestId])
    fingerprint = _hash(body.model_dump())
    with db.edit_state() as state:
        _actor(profile)
        stored = _stored(state,profile['schoolId'])
        receipt = next((item for item in stored['receipts'] if item['key'] == key),None)
        if receipt:
            if receipt['fingerprint'] != fingerprint:
                raise HTTPException(409,'Идентификатор сохранения уже использован с другими данными.')
            registry = next(item['registry'] for item in stored['snapshots'] if item['registry']['revision'] == receipt['revision'])
            return _result(registry)
        if stored['revision'] != body.expectedRegistryRevision:
            raise HTTPException(409,'Реестр исходных фактов изменился. Обновите версию перед сохранением.')
        if len(stored['receipts']) >= MAX_RECEIPTS:
            raise HTTPException(429,'Достигнут предел квитанций сохранения исходных фактов.')
        _,draft = _snapshot(raw_draft=True)
        candidate = build_registry(load_source_package(),draft,profile['schoolId'],stored['revision'])
        expected = {'packageHash':body.packageHash,'draftRevision':body.draftRevision,'draftHash':body.draftHash}
        if candidate['binding'] != expected:
            raise HTTPException(409,'Документы или черновик изменились. Обновите исходные факты и проверьте их снова.')
        latest = next((item['registry'] for item in stored['snapshots'] if item['registry']['revision'] == stored['revision']),None)
        if latest != candidate:
            if stored['revision'] >= MAX_REVISION or len(stored['snapshots']) >= MAX_SNAPSHOTS:
                raise HTTPException(429,'Достигнут предел версий реестра исходных фактов.')
            stored['revision'] += 1
            candidate['revision'] = stored['revision']
            stored['snapshots'].append({'registry':candidate,'hash':_hash(candidate)})
        stored['receipts'].append({'key':key,'fingerprint':fingerprint,'revision':stored['revision']})
        if len(json.dumps(stored,ensure_ascii=False).encode()) > MAX_BYTES:
            raise HTTPException(429,'Достигнут предел размера реестра исходных фактов.')
        state[NAMESPACE] = stored
        return _result(candidate)
