"""Director-only, local tabular exchange. Preview never writes or calls a model.

Manual cards, observed lessons and a complete solver configuration are separate
datasets. Import never creates accounts, guesses identities, deletes omitted rows,
sends notifications or converts an informational absence into a scheduling rule.
"""
from __future__ import annotations

import base64
import binascii
from contextlib import ExitStack, closing, contextmanager
from copy import deepcopy
import csv
import hashlib
from io import BytesIO, StringIO
import json
import math
import re
import sqlite3
from typing import Literal
from uuid import uuid4, uuid5, NAMESPACE_URL
from xml.sax.saxutils import escape
import zipfile

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import Field, ValidationError

from . import db, school_lesson_progress as progress, school_ops as ops, timetable
from .auth import session_profile
from .request_planning import canonical_manager
from .school_requests import require_bound_school
from .workspace import empty_school_state, verify_school_state

PREFIX = '/api/school/data-exchange'
KEY = '_schoolDataExchange'
MAX_CONTENT = 3 * 1024 * 1024
MAX_BODY = 4 * 1024 * 1024
MAX_ROWS = 5000
MAX_CELL = 20000
MAX_RECEIPTS = 100
MAX_LEDGER_BYTES = 2 * 1024 * 1024
MISSING = object()
KINDS = (*ops.KINDS, 'lessons', 'planningConfiguration', progress.KIND)
LABELS = {'students':'Ученики', 'teacherProfiles':'Карточки педагогов', 'tasks':'Задачи',
    'documents':'Документы', 'assignments':'Задания ученикам', 'materials':'Общие материалы',
    'absences':'Сведения об отсутствии', 'notifications':'Записи уведомлений',
    'lessons':'Наблюдаемая сетка занятий', 'planningConfiguration':'Модель авторасписания',
    progress.KIND:'Явка и оценки по урокам'}
FIELD_LABELS = {'name':'Имя', 'className':'Класс', 'note':'Примечание', 'subject':'Предмет',
    'title':'Название', 'description':'Описание', 'status':'Статус', 'due':'Срок',
    'teacherName':'Учитель', 'day':'День', 'slot':'Урок', 'start':'Начало', 'end':'Окончание',
    'teachers':'Учителя', 'rooms':'Кабинеты', 'content':'Содержание', 'type':'Тип',
    'weeklyHours':'Недельная нагрузка', 'available':'Доступен', 'reason':'Причина',
    'read':'Прочитано', 'text':'Текст', 'studentId':'ID ученика', 'owner':'Ответственный',
    'attendancePercent':'Посещаемость, %', 'averageGrade':'Средняя оценка', 'grade':'Оценка',
    'answer':'Ответ', 'feedback':'Обратная связь', 'publicationVersion':'Версия публикации',
    'lessonId':'ID урока', 'lessonDate':'Дата урока', 'attendance':'Явка', 'sourceRef':'Код источника'}
NOTICE = 'Добавление без удаления и изменения существующих записей. Точные дубли пропускаются. Карточки не создают доступы и не задают ограничения авторасписания.'


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _authorized(profile: dict = Depends(session_profile)) -> dict:
    """Reusable by the headers-only advisory route; rechecks current authority."""
    actor = canonical_manager(profile)
    if actor.get('staffRole', actor.get('role')) != 'director' or actor.get('role') != 'director':
        raise HTTPException(403, 'Импорт и экспорт доступны директору школы')
    ops._school_only()
    require_bound_school(actor['schoolId'])
    return actor


class PreviewInput(ops.StrictInput):
    kind: str
    format: Literal['csv', 'xlsx', 'json']
    content: str = Field(min_length=1, max_length=MAX_CONTENT)
    mapping: dict[str, int] | None = Field(default=None, max_length=64)
    expectedRevision: int = Field(ge=0, le=ops.MAX_REVISION)


class ApplyInput(PreviewInput):
    previewHash: str = Field(pattern=r'^[a-f0-9]{64}$')
    requestId: str = Field(pattern=r'^[A-Za-z0-9_-]{8,128}$')


class ExportInput(ops.StrictInput):
    kind: str
    format: Literal['csv', 'xlsx', 'json']
    expectedRevision: int = Field(ge=0, le=ops.MAX_REVISION)
    includeArchived: bool = False


def _model(kind):
    if kind in ops.MODELS:
        return ops.MODELS[kind]
    if kind == 'lessons':
        return timetable.EditableLessonInput
    if kind == progress.KIND:
        return progress.LessonProgressInput
    raise HTTPException(422, 'Неизвестный вид данных')


def _kind(kind):
    if kind not in KINDS:
        raise HTTPException(422, 'Неизвестный вид данных')


def _ledger(state, school_id):
    value = state.get(KEY)
    if value is None:
        return {'version':1, 'schoolId':school_id, 'receipts':{}, 'history':[]}
    if (not isinstance(value, dict) or set(value) != {'version','schoolId','receipts','history'}
            or value['version'] != 1 or value['schoolId'] != school_id
            or not isinstance(value['receipts'], dict) or len(value['receipts']) > MAX_RECEIPTS
            or not isinstance(value['history'], list) or len(value['history']) > MAX_RECEIPTS):
        raise HTTPException(503, 'Журнал обмена данными требует проверки')
    for key, receipt in value['receipts'].items():
        if (not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', key)
                or not isinstance(receipt, dict) or set(receipt) != {'actorHash','payloadHash','result','at','hash'}
                or receipt.get('hash') != _digest({k:v for k,v in receipt.items() if k != 'hash'})
                or not isinstance(receipt['result'], dict) or receipt['result'].get('requestId') != key):
            raise HTTPException(503, 'Повреждена квитанция обмена данными')
    return deepcopy(value)


def _state(connection):
    row = connection.execute('SELECT payload FROM demo_state WHERE id=1').fetchone() if ops._table_exists(connection, 'demo_state') else None
    value = json.loads(row[0]) if row else empty_school_state()
    verify_school_state(value)
    return value


def _config(connection):
    if connection is None or not ops._table_exists(connection, 'planning_config'):
        return {'revision':0, 'baseDraftRevision':0, 'input':None}
    from .planning import PlanningService
    return PlanningService._config(connection)


@contextmanager
def _snapshot():
    path = db.database_path()
    if not path.exists():
        yield None, empty_school_state(), timetable._empty(), _config(None)
        return
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=15)) as connection:
        connection.execute('BEGIN')
        yield connection, _state(connection), timetable._load(connection) if ops._table_exists(connection, 'timetable_draft') else timetable._empty(), _config(connection)


def _revision(kind, state, draft, config, school_id):
    if kind == progress.KIND:
        return progress.ledger(state, school_id)['revision']
    return draft['revision'] if kind == 'lessons' else config['revision'] if kind == 'planningConfiguration' else ops._ops(state)['revision']


def _field_type(spec):
    if 'anyOf' in spec:
        spec = next((x for x in spec['anyOf'] if x.get('type') != 'null'), {})
    return spec.get('type', 'string')


def catalog(profile):
    actor = _authorized(profile)
    with _snapshot() as (_, state, draft, config):
        _ledger(state, actor['schoolId'])
        datasets = []
        for kind in KINDS:
            fields = []
            if kind != 'planningConfiguration':
                schema = _model(kind).model_json_schema()
                fields = [{'key':key, 'label':FIELD_LABELS.get(key, key), 'type':_field_type(spec),
                    'required':key in schema.get('required', [])} for key, spec in schema['properties'].items()]
            notice = NOTICE
            if kind == 'absences':
                notice = 'Информационные записи. Они не создают датированное отсутствие и не меняют расчёт.'
            if kind == 'notifications':
                notice = 'Записи реестра. Импорт не отправляет сообщения и не подтверждает доставку.'
            if kind == 'lessons':
                notice = 'Добавление наблюдаемых занятий в локальный черновик. Это не подтверждение полной нормативной модели и не публикация.'
            if kind == 'planningConfiguration':
                notice = 'Полная замена модели авторасписания после предпросмотра. Сохраняются явные ID ресурсов, группы, нагрузка и правила; расчёт и публикация не запускаются.'
            if kind == progress.KIND:
                notice = progress.NOTICE
            datasets.append({'kind':kind, 'label':LABELS[kind], 'fields':fields,
                'revision':_revision(kind,state,draft,config,actor['schoolId']),
                'limit':progress.LIMIT if kind == progress.KIND else ops.RECORD_LIMITS.get(kind,timetable.MAX_LESSONS),
                'formats':['json'] if kind == 'planningConfiguration' else ['csv','xlsx','json'], 'notice':notice})
        _authorized(profile)
        return {'revision':ops._ops(state)['revision'], 'datasets':datasets, 'formats':['csv','xlsx','json']}


def _json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Повторяющиеся ключи JSON')
            result[key] = value
        return result
    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, RecursionError):
        raise HTTPException(422, 'Некорректный JSON или повторяющиеся ключи') from None


def _xlsx_table(content):
    try:
        payload = base64.b64decode(content, validate=True)
    except (ValueError, binascii.Error):
        raise HTTPException(422, 'XLSX должен быть передан как base64') from None
    roots = timetable._archive(payload)  # ZIP/XML/formulas/macros/external links are checked for the entire book.
    workbook = roots.get('xl/workbook.xml'); relations = roots.get('xl/_rels/workbook.xml.rels')
    if workbook is None or relations is None or '[Content_Types].xml' not in roots:
        raise HTTPException(422, 'Отсутствует структура книги XLSX')
    sheets = workbook.findall('m:sheets/m:sheet', timetable.NS)
    if len(sheets) != 1:
        raise HTTPException(422, 'Импортируйте один явно выбранный лист')
    links = {x.get('Id'):x for x in relations}
    link = links.get(sheets[0].get('{'+timetable.DOC_REL+'}id'))
    if link is None or link.get('Type') != timetable.DOC_REL+'/worksheet':
        raise HTTPException(422, 'Неверная связь листа XLSX')
    root = roots.get(timetable._relationship_target('xl/workbook.xml',link.get('Target','')))
    if root is None or root.tag != '{'+timetable.MAIN+'}worksheet':
        raise HTTPException(422, 'Лист отсутствует в книге')
    shared = []
    shared_links = [x for x in relations if x.get('Type') == timetable.DOC_REL+'/sharedStrings']
    if len(shared_links) > 1:
        raise HTTPException(422, 'Неоднозначный словарь строк')
    if shared_links:
        shared_root = roots.get(timetable._relationship_target('xl/workbook.xml',shared_links[0].get('Target','')))
        if shared_root is None or shared_root.tag != '{'+timetable.MAIN+'}sst':
            raise HTTPException(422, 'Повреждён словарь строк')
        for item in shared_root:
            if item.tag != '{'+timetable.MAIN+'}si' or len(shared) >= timetable.MAX_SHARED_STRINGS:
                raise HTTPException(422, 'Слишком большой словарь строк')
            shared.append(''.join(x.text or '' for x in item.findall('.//m:t',timetable.NS)))
    sheet_data = root.findall('m:sheetData',timetable.NS)
    if len(sheet_data) != 1:
        raise HTTPException(422, 'Неоднозначные строки листа')
    rows, coordinates, numbers = [], set(), set()
    for row in sheet_data[0]:
        index = row.get('r','')
        if row.tag != '{'+timetable.MAIN+'}row' or not re.fullmatch(r'[1-9][0-9]{0,4}',index) or int(index)>MAX_ROWS+1 or index in numbers:
            raise HTTPException(422, 'Повтор строки или превышен лимит строк')
        numbers.add(index); cells = {}
        for cell in row:
            coord = cell.get('r',''); match = timetable.COORD_PATTERN.fullmatch(coord)
            if cell.tag != '{'+timetable.MAIN+'}c' or not match or int(match[2]) != int(index) or coord in coordinates:
                raise HTTPException(422, 'Повторяющаяся или неверная ячейка')
            coordinates.add(coord); col = 0
            for char in match[1]: col = col*26+ord(char)-64
            if col>64: raise HTTPException(422, 'Слишком много столбцов')
            # Reuse the established cell representation without its timetable-only
            # 2000-character cap: document/material fields explicitly allow 20000.
            kind=cell.get('t','n'); values=cell.findall('m:v',timetable.NS)
            if kind=='inlineStr': text=''.join(x.text or '' for x in cell.findall('m:is//m:t',timetable.NS))
            elif len(values)>1: raise HTTPException(422,'Неоднозначная ячейка')
            else:
                text=(values[0].text or '') if values else ''
                if kind=='s':
                    if not re.fullmatch(r'[0-9]{1,8}',text) or int(text)>=len(shared): raise HTTPException(422,'Повреждена строка XLSX')
                    text=shared[int(text)]
                elif kind not in {'n','str','b','d'}: raise HTTPException(422,'Неподдерживаемая ячейка XLSX')
            if len(text)>MAX_CELL: raise HTTPException(422,'Слишком длинная ячейка')
            cells[col]=text
        if any(x.strip() for x in cells.values()): rows.append((int(index),[cells.get(i,'') for i in range(1,max(cells,default=0)+1)]))
    rows.sort()
    return [r for _,r in rows]


def _table(body):
    if len(body.content.encode('utf-8'))>MAX_CONTENT:
        raise HTTPException(413, 'Файл превышает допустимый размер')
    if body.format=='json':
        envelope=_json(body.content)
        if (not isinstance(envelope,dict) or set(envelope)!={'version','kind','records'}
                or type(envelope['version']) is not int or envelope['version']!=1 or envelope['kind']!=body.kind
                or not isinstance(envelope['records'],list) or len(envelope['records'])>MAX_ROWS
                or any(not isinstance(r,dict) for r in envelope['records'])):
            raise HTTPException(422,'Требуется JSON {version:1, kind, records:[...]} без служебных полей')
        headers=list(dict.fromkeys(k for row in envelope['records'] for k in row))
        return headers, [[row.get(k,MISSING) for k in headers] for row in envelope['records']], True
    if body.format=='xlsx': rows=_xlsx_table(body.content)
    else:
        try:
            # CSV has one documented delimiter; ambiguity is never guessed.
            rows=list(csv.reader(StringIO(body.content.lstrip('\ufeff'),newline=''),delimiter=';',strict=True))
        except (csv.Error,UnicodeError): raise HTTPException(422,'Некорректный CSV UTF-8 с разделителем ;') from None
    if not rows or len(rows)>MAX_ROWS+1 or any(len(r)>64 or any(len(c)>MAX_CELL for c in r) for r in rows):
        raise HTTPException(422,'Пустая таблица или превышен лимит строк/столбцов/ячеек')
    headers=[str(h).strip() for h in rows[0]]
    if not headers or any(not h or len(h)>160 for h in headers) or len(set(headers))!=len(headers):
        raise HTTPException(422,'Заголовки должны быть непустыми и уникальными')
    if any(len(r)>len(headers) for r in rows[1:]): raise HTTPException(422,'Строка шире заголовков')
    return headers, [r+['']*(len(headers)-len(r)) for r in rows[1:] if any(c.strip() for c in r)], False


def _mapping(headers,model,provided):
    known=model.model_fields
    if provided is None:
        aliases={timetable.canonical(k):k for k in known}
        aliases.update({timetable.canonical(FIELD_LABELS.get(k,k)):k for k in known})
        mapping={}
        for index,header in enumerate(headers):
            key=aliases.get(timetable.canonical(header))
            if key:
                if key in mapping: raise HTTPException(422,'Два столбца сопоставлены одному полю')
                mapping[key]=index
        unknown=[h for i,h in enumerate(headers) if i not in mapping.values()]
        return mapping,unknown
    if (any(k not in known or type(v) is not int or not 0<=v<len(headers) for k,v in provided.items())
            or len(set(provided.values()))!=len(provided)):
        raise HTTPException(422,'Некорректное сопоставление столбцов')
    return dict(provided),[]


def _convert(value,spec):
    kind=_field_type(spec)
    if value=='' or value is None:
        if 'anyOf' in spec and any(x.get('type')=='null' for x in spec['anyOf']): return None
        if kind=='array': return []
        if kind=='string': return ''
        raise ValueError('Нужно число или логическое значение')
    if kind=='string': return value
    if kind=='boolean':
        values={'true':True,'false':False,'1':True,'0':False,'да':True,'нет':False}
        if str(value).casefold() not in values: raise ValueError('Нужно true/false или да/нет')
        return values[str(value).casefold()]
    if kind=='integer':
        if not re.fullmatch(r'-?[0-9]{1,10}',value): raise ValueError('Нужно целое число')
        return int(value)
    if kind=='number':
        if not re.fullmatch(r'-?(?:[0-9]+(?:[.,][0-9]+)?|[.,][0-9]+)',value): raise ValueError('Нужно число')
        number=float(value.replace(',','.'))
        if not math.isfinite(number): raise ValueError('Нужно конечное число')
        return number
    if kind=='array': return _json(value)
    return value


def _preview(body,actor,connection,state,draft,config,resources):
    _kind(body.kind); current=_revision(body.kind,state,draft,config,actor['schoolId'])
    if current!=body.expectedRevision: raise HTTPException(409,'Данные изменились: повторите предпросмотр')
    source_hash=_digest({'kind':body.kind,'format':body.format,'content':body.content,'mapping':body.mapping})
    if body.kind=='planningConfiguration':
        from .planning import ConfigInput
        if body.format!='json' or body.mapping is not None: raise HTTPException(422,'Полная модель поддерживает только JSON без сопоставления столбцов')
        envelope=_json(body.content)
        if not isinstance(envelope,dict) or set(envelope)!={'version','kind','input'} or type(envelope['version']) is not int or envelope['version']!=1 or envelope['kind']!=body.kind:
            raise HTTPException(422,'Требуется JSON {version:1,kind:planningConfiguration,input:PlanningInput}')
        try:
            plan=ConfigInput.model_validate({'expectedRevision':current,'baseDraftRevision':draft['revision'],'input':envelope['input']}).input.model_dump()
        except ValidationError as error:
            raise HTTPException(422,[{'field':'.'.join(map(str,e['loc'])),'message':e['msg']} for e in error.errors(include_input=False,include_url=False)]) from None
        counts={k:len(plan[k]) for k in ('teachers','groups','rooms','slots','demands')}
        result={'kind':body.kind,'revision':current,'sourceHash':source_hash,'headers':[],'mapping':{},
            'rows':[{'row':1,'status':'create','record':plan,'errors':[]}], 'counts':{'create':1,'skip':0,'error':0},
            'ready':True,'notice':'Полная замена модели авторасписания. Не запускает расчёт, не публикует сетку и не создаёт доступы.',
            'binding':{'schoolId':actor['schoolId'],'baseDraftRevision':draft['revision']},'modelCounts':counts}
    else:
        model=_model(body.kind); headers,table,typed=_table(body)
        if len(headers)>64: raise HTTPException(422,'Слишком много столбцов')
        if set(headers)&ops.SERVER_FIELDS: raise HTTPException(422,'Служебные ID, архив и даты нельзя импортировать')
        mapping,unknown=_mapping(headers,model,body.mapping)
        schema=model.model_json_schema()['properties']
        journal = body.kind == progress.KIND
        if journal:
            existing = progress.ledger(state, actor['schoolId'])['records']
            by_occurrence = progress.index(existing)
            published = progress.PublishedLessons(connection, state, actor['schoolId'], resources)
        else:
            existing=draft['lessons'] if body.kind=='lessons' else ops._ops(state)['records'][body.kind]
        seen={_digest({k:r.get(k) for k in model.model_fields}) for r in existing}
        rows=[]; counts={'create':0,'skip':0,'error':0}
        for index,raw in enumerate(table,2):
            values={}; errors=[]
            for field,col in mapping.items():
                try:
                    value=raw[col]
                    # Missing JSON properties retain schema defaults, unlike explicit null.
                    if value is MISSING: continue
                    if body.format=='csv' and _field_type(schema[field])=='string' and isinstance(value,str):
                        if value.startswith("''") or (value.startswith("'") and value[1:].lstrip(' \t\r\n\ufeff').startswith(('=','+','-','@'))):
                            value=value[1:]
                    values[field]=value if typed else _convert(value,schema[field])
                except (ValueError,HTTPException) as error:
                    errors.append({'field':field,'message':str(error.detail if isinstance(error,HTTPException) else error)})
            if unknown: errors.append({'field':'headers','message':'Сопоставьте или явно исключите неизвестные столбцы: '+', '.join(unknown)})
            try:
                value=model.model_validate(values).model_dump()
            except ValidationError as error:
                errors.extend({'field':'.'.join(map(str,e['loc'])),'message':e['msg']} for e in error.errors(include_input=False,include_url=False)); value=None
            if value is not None and not errors:
                try:
                    if body.kind=='assignments': ops._references(ops._ops(state),body.kind,value)
                    if body.kind=='absences' and not any(value['teacherName'] in l['teachers'] for l in draft['lessons']): raise ValueError('Учитель не найден в наблюдаемом расписании')
                    if journal:
                        value = published.bind(value)
                        key = progress.occurrence(value)
                        previous = by_occurrence.get(key)
                        if previous is not None and any(previous.get(field) != item for field, item in value.items()):
                            raise ValueError('Для ученика и этого урока уже есть другая запись. Проверьте источник.')
                except (HTTPException,ValueError) as error:
                    if isinstance(error,HTTPException) and error.status_code>=500: raise
                    errors.append({'field':'reference','message':str(error.detail if isinstance(error,HTTPException) else error)})
            if errors:
                status = 'error'
            elif journal:
                status = 'skip' if key in by_occurrence else 'create'
                by_occurrence[key] = value
            else:
                status = 'skip' if _digest(value) in seen else 'create'
                seen.add(_digest(value))
            counts[status]+=1; rows.append({'row':index,'status':status,'record':value,'errors':errors})
        limit=progress.LIMIT if journal else ops.RECORD_LIMITS.get(body.kind,timetable.MAX_LESSONS)
        if len(existing)+counts['create']>limit: raise HTTPException(422,'Превышен лимит записей раздела')
        result={'kind':body.kind,'revision':current,'sourceHash':source_hash,'headers':headers,'mapping':mapping,
            'rows':rows,'counts':counts,'ready':bool(rows) and counts['error']==0,
            'notice':NOTICE,'binding':{'schoolId':actor['schoolId'], 'draftRevision':draft['revision'] if body.kind=='absences' else None}}
        if body.kind=='lessons':
            result['notice']='Добавление в наблюдаемый черновик, не в выбранный эксперимент; нормативная модель и публикация не изменяются.'
            combined=[*draft['lessons'], *[{'id':'import_'+str(r['row']),**r['record']} for r in rows if r['status']=='create']]
            issues=timetable.compute_issues(combined); result['issues']=issues
            if any(i['severity']=='error' for i in issues): result['ready']=False
            if config['input'] and config['input'].get('confirmed') is True:
                result['ready']=False; result['blockers']=['Подтверждена нормативная модель: изменяйте проверенный кандидат расчёта. Импорт свободной сетки не доказывает соблюдение ограничений.']
        elif body.kind in {'absences','notifications'}:
            result['notice']='Только информационные записи; не меняют ограничения расчёта и не отправляют сообщения.'
        elif journal:
            result['notice']=progress.NOTICE
    result['previewHash']=_digest({'preview':result,'actor':_actor_hash(actor)})
    return result


def preview(body,profile):
    body=PreviewInput.model_validate(body); actor=_authorized(profile)
    with _snapshot() as (connection,state,draft,config), ExitStack() as resources:
        _ledger(state,actor['schoolId']); result=_preview(body,actor,connection,state,draft,config,resources)
        _authorized(profile)
        return result


def _actor_hash(actor):
    return _digest({k:actor.get(k) for k in ('schoolId','userId','membershipId','membershipRevision','role')})


def _replay(state,actor,body):
    data=_ledger(state,actor['schoolId']); receipt=data['receipts'].get(body.requestId)
    if receipt:
        if receipt['actorHash']!=_actor_hash(actor) or receipt['payloadHash']!=_digest(body.model_dump()):
            raise HTTPException(409,'Идентификатор запроса уже использован для других данных или пользователя')
        return deepcopy(receipt['result'])


def _receipt(state,actor,body,result):
    data=_ledger(state,actor['schoolId']); receipt={'actorHash':_actor_hash(actor),'payloadHash':_digest(body.model_dump()),'result':deepcopy(result),'at':ops._stamp()}
    receipt['hash']=_digest(receipt); data['receipts'][body.requestId]=receipt
    data['history'].append({'requestId':body.requestId,'kind':body.kind,'revision':result['revision'],'at':receipt['at'],'created':result['created']})
    while len(data['receipts'])>MAX_RECEIPTS or len(json.dumps(data,ensure_ascii=False).encode())>MAX_LEDGER_BYTES:
        oldest=next((x['requestId'] for x in data['history'] if x['requestId'] in data['receipts']),body.requestId)
        if oldest==body.requestId: raise HTTPException(409,'Квитанция превышает лимит журнала')
        del data['receipts'][oldest]
    data['history']=data['history'][-MAX_RECEIPTS:]; state[KEY]=data


def apply(body,profile):
    body=ApplyInput.model_validate(body); actor=_authorized(profile)
    # Exact committed retries recover before a later data revision, with current
    # authority and bound tenant still mandatory. No request can mutate on replay.
    with _snapshot() as (connection,state,draft,config), ExitStack() as resources:
        result=_replay(state,actor,body)
        if result is not None:
            _authorized(profile)
            return result
        checked=_preview(body,actor,connection,state,draft,config,resources)
        if checked['previewHash']!=body.previewHash or not checked['ready']:
            raise HTTPException(409,'Предпросмотр изменился или содержит ошибки; повторите проверку')
    if body.kind=='planningConfiguration':
        from .planning import get_service
        service=get_service(); factory=service._connect
    elif body.kind=='lessons': factory=timetable._connect
    else: factory=db.connect
    with closing(factory()) as connection, ExitStack() as resources:
        try:
            # Subgroup writes read the domain before updating the personal store.
            # Lock the journal's domain transaction first, before reading that store.
            connection.execute('BEGIN EXCLUSIVE' if body.kind == progress.KIND else 'BEGIN IMMEDIATE')
            actor=_authorized(profile); state=_state(connection)
            replay=_replay(state,actor,body)
            if replay is not None:
                _authorized(profile)
                connection.rollback()
                return replay
            connection.execute('CREATE TABLE IF NOT EXISTS demo_state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')
            connection.execute('INSERT OR IGNORE INTO demo_state VALUES(1,?)',(json.dumps(state,ensure_ascii=False),))
            draft=timetable._load(connection) if ops._table_exists(connection,'timetable_draft') else timetable._empty()
            config=_config(connection); checked=_preview(body,actor,connection,state,draft,config,resources)
            if checked['previewHash']!=body.previewHash or not checked['ready']:
                raise HTTPException(409,'Предпросмотр изменился или содержит ошибки; повторите проверку')
            rows=[r for r in checked['rows'] if r['status']=='create']; stamp=ops._stamp(); ids=[]
            if body.kind=='planningConfiguration':
                if config['revision']>=ops.MAX_REVISION: raise HTTPException(409,'Достигнут лимит ревизий')
                connection.execute('INSERT INTO planning_config VALUES(1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,base_draft_revision=excluded.base_draft_revision,input_json=excluded.input_json,updated_at=excluded.updated_at',
                    (config['revision']+1,draft['revision'],json.dumps(rows[0]['record'],ensure_ascii=False,allow_nan=False),stamp))
                connection.execute("UPDATE planning_runs SET status='stale',message=?,updated_at=? WHERE status IN ('optimal','feasible')",('Модель изменена после расчёта; требуется новый расчёт.',stamp))
                revision=config['revision']+1
            elif body.kind=='lessons':
                timetable._cas(draft,body.expectedRevision)
                lessons=deepcopy(draft['lessons'])
                for row in rows:
                    ident='tt_'+uuid5(NAMESPACE_URL,actor['schoolId']+'/'+body.requestId+'/'+str(row['row'])).hex; ids.append(ident)
                    lessons.append({**row['record'],'id':ident,'raw':'','sourceCell':'Импорт!'+str(row['row']),'edited':True})
                if rows:
                    changed={**draft,'revision':draft['revision']+1,'sourceName':'Импорт таблицы занятий','lessons':lessons,'issues':timetable.compute_issues(lessons),'updatedAt':stamp}
                    timetable._save(connection,draft,changed,timetable._actor(actor),'import_flat'); revision=changed['revision']
                else: revision=draft['revision']
            elif body.kind == progress.KIND:
                journal = progress.ledger(state, actor['schoolId'])
                ops._cas(journal, body.expectedRevision)
                revision = journal['revision']
                if rows:
                    records = []
                    for row in rows:
                        ident = str(uuid4())
                        ids.append(ident)
                        records.append({**row['record'], 'id': ident, 'createdAt': stamp})
                    revision += 1
                    state[progress.KEY] = {**journal, 'revision': revision, 'records': [*journal['records'], *records]}
            else:
                data=deepcopy(ops._ops(state)); ops._cas(data,body.expectedRevision)
                for row in rows:
                    ident=str(uuid4()); ids.append(ident)
                    record={**row['record'],'id':ident,'createdAt':stamp,'updatedAt':stamp,'archived':False}
                    if body.kind=='absences': record['timetableRevision']=draft['revision']
                    data['records'][body.kind].append(record)
                if rows: ops._commit(state,data,actor,'import',body.kind,{'title':'Импорт таблицы','id':''},stamp)
                revision=data['revision']
            result={'revision':revision,'created':len(rows),'skipped':checked['counts']['skip'],'recordIds':ids,'requestId':body.requestId}
            _authorized(profile); _receipt(state,actor,body,result)
            connection.execute('UPDATE demo_state SET payload=? WHERE id=1',(json.dumps(state,ensure_ascii=False,allow_nan=False),))
            connection.commit(); return result
        except BaseException:
            connection.rollback(); raise


def _cell(value, *, escape_formula=True):
    text='' if value is None else json.dumps(value,ensure_ascii=False,separators=(',',':')) if not isinstance(value,str) else value
    if not escape_formula: return text
    return "'"+text if text.startswith("'") else timetable._spreadsheet_text(text)


def _download(kind,format,records, *, input_value=None):
    if format=='json':
        value={'version':1,'kind':kind,'input':input_value} if kind=='planningConfiguration' else {'version':1,'kind':kind,'records':records}
        return json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False).encode('utf-8'),'application/json; charset=utf-8'
    if kind=='planningConfiguration': raise HTTPException(422,'Модель авторасписания экспортируется только в JSON')
    headers=list(_model(kind).model_fields); rows=[headers,*[[_cell(r.get(k),escape_formula=format=='csv') for k in headers] for r in records]]
    if format=='csv':
        stream=StringIO(newline=''); csv.writer(stream,delimiter=';',lineterminator='\r\n').writerows(rows)
        return stream.getvalue().encode('utf-8-sig'),'text/csv; charset=utf-8'
    def column(i):
        value=''
        while i: i,rem=divmod(i-1,26); value=chr(65+rem)+value
        return value
    xml_rows=[]
    for i,row in enumerate(rows,1):
        cells=''.join(f'<c r="{column(j)}{i}" t="inlineStr"><is><t xml:space="preserve">{escape(text).replace(chr(13),"&#13;")}</t></is></c>' for j,text in enumerate(row,1))
        xml_rows.append(f'<row r="{i}">{cells}</row>')
    parts={
        '[Content_Types].xml':'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
        '_rels/.rels':f'<Relationships xmlns="{timetable.REL}"><Relationship Id="r1" Type="{timetable.DOC_REL}/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        'xl/workbook.xml':f'<workbook xmlns="{timetable.MAIN}" xmlns:r="{timetable.DOC_REL}"><sheets><sheet name="Данные" sheetId="1" r:id="r1"/></sheets></workbook>',
        'xl/_rels/workbook.xml.rels':f'<Relationships xmlns="{timetable.REL}"><Relationship Id="r1" Type="{timetable.DOC_REL}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        'xl/worksheets/sheet1.xml':f'<worksheet xmlns="{timetable.MAIN}"><sheetData>{"".join(xml_rows)}</sheetData></worksheet>'}
    stream=BytesIO()
    with zipfile.ZipFile(stream,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        for path,text in parts.items(): archive.writestr(path,'<?xml version="1.0" encoding="UTF-8"?>'+text)
    return stream.getvalue(),'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


def export(body,profile):
    body=ExportInput.model_validate(body); actor=_authorized(profile); _kind(body.kind)
    with _snapshot() as (_,state,draft,config):
        _ledger(state,actor['schoolId']); revision=_revision(body.kind,state,draft,config,actor['schoolId'])
        if revision!=body.expectedRevision: raise HTTPException(409,'Версия изменилась: обновите данные перед экспортом')
        if body.kind=='planningConfiguration':
            if config['input'] is None: raise HTTPException(422,'Нет сохранённой модели авторасписания')
            content,media=_download(body.kind,body.format,[],input_value=config['input'])
        else:
            model=_model(body.kind)
            if body.kind == progress.KIND:
                values = progress.ledger(state, actor['schoolId'])['records']
            else:
                values=draft['lessons'] if body.kind=='lessons' else ops._ops(state)['records'][body.kind]
            records=[{k:deepcopy(r.get(k)) for k in model.model_fields} for r in values if body.includeArchived or not r.get('archived',False)]
            content,media=_download(body.kind,body.format,records)
        _authorized(profile)
        return content,media,revision


router=APIRouter(prefix=PREFIX,tags=['director data exchange'])

@router.get('/catalog')
def get_catalog(profile:dict=Depends(_authorized)):
    return catalog(profile)

@router.get('/lesson-progress-references')
def lesson_progress_references(publicationVersion:int|None=Query(default=None,ge=1,le=ops.MAX_REVISION),
                               profile:dict=Depends(_authorized)):
    actor = _authorized(profile)
    with _snapshot() as (connection,state,_,_):
        data = progress.PublishedLessons(connection,state,actor['schoolId']).references(publicationVersion)
        _authorized(profile)
        content = json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False)
        return Response(content,media_type='application/json',headers={
            'Content-Disposition':f'attachment; filename="school-lesson-progress-references-v{data["publicationVersion"]}.json"',
            'Cache-Control':'no-store'})

@router.post('/preview')
def preview_route(body:PreviewInput,profile:dict=Depends(_authorized)):
    return preview(body,profile)

@router.post('/apply')
def apply_route(body:ApplyInput,profile:dict=Depends(_authorized)):
    return apply(body,profile)

@router.post('/export')
def export_route(body:ExportInput,profile:dict=Depends(_authorized)):
    content,media,revision=export(body,profile)
    return Response(content,media_type=media,headers={'Content-Disposition':f'attachment; filename="school-{body.kind}-r{revision}.{body.format}"','X-Data-Revision':str(revision)})

@router.get('/template/{kind}')
def template_route(kind:str,format:Literal['csv','xlsx','json']=Query(default='csv'),profile:dict=Depends(_authorized)):
    _authorized(profile); _kind(kind)
    if kind=='planningConfiguration':
        from .planning import template_input
        content,media=_download(kind,format,[],input_value=template_input()['input'])
    else: content,media=_download(kind,format,[])
    return Response(content,media_type=media,headers={'Content-Disposition':f'attachment; filename="school-{kind}-template.{format}"'})
