"""A dated review draft, isolated from the active timetable and publication.

This route deliberately does not turn historical class rows into a normative
solver model. Missing rooms, group membership and teacher phases remain open.
"""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
from datetime import date, timedelta
import hashlib
import json
import re
import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from . import db
from .auth import require_roles
from .school_ops import _snapshot
from .school_plan_imports import _actor
from .workspace import verify_school_state
from .school_plan_sources import load_source_package

NAMESPACE = '_datedTimetableDrafts'
WEEK = '2026-10-26'
CALENDAR = {'periodStart': WEEK,
    'teachingDates': ['2026-10-26', '2026-10-27', '2026-10-28'],
    'closedDates': ['2026-10-29', '2026-10-30', '2026-10-31'],
    'holidayEnd': None, 'source': 'Вымышленный учебный сценарий Идеяфикс: три учебных дня, затем три закрытых дня.'}


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _matches(pattern, value):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


class CreateDatedDraft(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    weekStart: Literal['2026-10-26']
    expectedSourceRevision: int = Field(ge=0, le=2**31-1)
    requestId: str = Field(min_length=16, max_length=80, pattern=r'^[A-Za-z0-9_-]+$')


def _stored(state, school_id):
    result = state.get(NAMESPACE)
    if result is None:
        return {'version': 1, 'schoolId': school_id, 'drafts': []}
    if (not isinstance(result, dict) or type(result.get('version')) is not int or result.get('version') != 1
            or result.get('schoolId') != school_id or not isinstance(result.get('drafts'), list)
            or len(result['drafts']) > 8):
        raise HTTPException(503, 'Реестр датированных черновиков требует проверки.')
    for item in result['drafts']:
        if not isinstance(item, dict) or not isinstance(item.get('draft'), dict):
            raise HTTPException(503, 'Датированный черновик требует проверки.')
        draft = item['draft']
        if (draft.get('scope') != 'historical_carryover' or draft.get('publicationBlocked') is not True
                or not isinstance(draft.get('lessons'), list) or draft.get('weekStart') != WEEK
                or draft.get('closedDates') != CALENDAR['closedDates']
                or draft.get('teachingDates') != CALENDAR['teachingDates']
                or draft.get('holidayEnd') is not None
                or not _matches(r'[a-f0-9]{64}', item.get('commandHash'))
                or not _matches(r'[a-f0-9]{64}', item.get('sourceHash'))
                or not _matches(r'dated_[a-f0-9]{24}', draft.get('id'))
                or not _matches(r'[A-Za-z0-9_-]{16,80}', item.get('requestId'))
                or not isinstance(item.get('actorId'), str)
                or type(item.get('sourceRevision')) is not int):
            raise HTTPException(503, 'Датированный черновик требует проверки.')
    if len({item['draft']['weekStart'] for item in result['drafts']}) != len(result['drafts']):
        raise HTTPException(503, 'Повторные версии датированной недели требуют проверки.')
    return deepcopy(result)


def _fictional_plan(connection):
    from .scheduling_models import PlanningInput
    package=load_source_package()
    if package.get('packageId')!='ideyafix-fictional-inputs-v1':
        raise ValueError('Expected the independent fictional source package')
    row=connection.execute('SELECT input_json FROM planning_config WHERE id=1').fetchone()
    if row is None:
        raise ValueError('The fictional normative plan is missing')
    plan=PlanningInput.model_validate(json.loads(row[0])).model_dump()
    if plan['confirmed'] is not True:
        raise ValueError('The fictional normative facts must be explicitly confirmed')
    return package,plan


def _current_rule_binding(connection=None):
    try:
        if connection is None:
            with closing(sqlite3.connect(db.database_path(),timeout=15)) as active:
                package,plan=_fictional_plan(active)
        else:
            package,plan=_fictional_plan(connection)
        return _hash({'package':package,'confirmedPlan':plan})
    except (ValueError,sqlite3.Error):
        return None


def _fictional_basis(source,connection):
    """Bind the review to explicit own facts; never borrow another source's basis."""
    from .short_week_draft import source_rows_fingerprint
    package,plan=_fictional_plan(connection)
    groups={row['id']:row for row in plan['groups']}
    teachers={row['id']:row for row in plan['teachers']}
    rooms={row['id']:row for row in plan['rooms']}
    if not isinstance(source.get('lessons'),list) or not 1<=len(source['lessons'])<=2000:
        raise ValueError('Expected a bounded explicit fictional source')
    for lesson in source['lessons']:
        matches=[d for d in plan['demands'] if d['subject']==lesson['subject']
            and lesson['className'] in {groups[g]['className'] for g in d['groupIds']}
            and set(lesson['teachers'])=={teachers[t]['name'] for t in d['teacherIds']}
            and lesson['rooms'] and set(lesson['rooms'])<={rooms[r]['name'] for r in (d['roomIds'] or rooms)}]
        if len(matches)!=1:
            raise ValueError('A source row lacks an exact confirmed fictional demand')
    return {'periodStart':WEEK,'sourcePeriodStart':plan['periodStart'],
        'sourceRowsFingerprint':source_rows_fingerprint(source['lessons']),
        'originalExpectedRows':len(source['lessons']),'rules':[],'repairs':[],
        'sourceFile':{'kind':'explicit_fictional_model','packageId':package['packageId'],
                      'confirmedPlanHash':_hash(plan)},
        'unknowns':[{'code':'fictional_review_only','label':'Вымышленное упражнение не подтверждает новое расписание.'}]}


def _response(stored, source):
    item = next((i for i in stored['drafts'] if i['draft']['weekStart'] == WEEK), None)
    draft = deepcopy(item['draft']) if item else None
    if draft:
        rules_binding = _current_rule_binding()
        draft['sourceChanged'] = (item['sourceHash'] != _hash(source)
                                  or rules_binding is None
                                  or item.get('rulesPackageHash') != rules_binding)
    return {'draft': draft, 'canCreate': item is None and bool(source['lessons']),
            'sourceRevision': source['revision']}


def read_dated_draft(profile):
    _actor(profile)
    state, source = _snapshot(raw_draft=True)
    return _response(_stored(state, profile['schoolId']), source)


def guard_saved_calendar(state, school_id, period_start, lessons=None):
    """The saved fictional closure also gates full-week calculation paths."""
    if NAMESPACE not in state:
        return
    stored = _stored(state, school_id)
    item = next((i for i in stored['drafts'] if i['draft']['weekStart'] == period_start), None)
    if item is None:
        return
    closed = set(item['draft']['closedDates'])
    if lessons is not None:
        first = date.fromisoformat(period_start)
        if not any((first + timedelta(days=row['day'])).isoformat() in closed for row in lessons):
            return
    raise HTTPException(409, 'В вымышленном сценарии 29–31 октября закрыты. Полный недельный расчёт нельзя согласовать или опубликовать; доступен отдельный учебный черновик на 26–28 октября.')


def create_dated_draft(body: CreateDatedDraft, profile):
    _actor(profile)
    from .short_week_draft import build_short_week_draft
    actor_id = str(profile['userId'])
    command_hash = _hash(body.model_dump())
    # One transaction protects source CAS, receipt and the separate namespace.
    with closing(sqlite3.connect(db.database_path(), timeout=15)) as connection:
        connection.execute('BEGIN IMMEDIATE')
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='demo_state'").fetchone():
            raise HTTPException(409, 'Сначала откройте рабочий кабинет школы и загрузите исходную сетку.')
        row = connection.execute('SELECT payload FROM demo_state WHERE id=1').fetchone()
        if row is None:
            raise HTTPException(409, 'Сначала загрузите исходную школьную сетку.')
        state = json.loads(row[0]); verify_school_state(state)
        stored = _stored(state, profile['schoolId'])
        row = connection.execute('SELECT payload FROM timetable_draft WHERE id=1').fetchone()
        source = json.loads(row[0]) if row else {'revision': 0, 'lessons': []}
        receipt = next((i for i in stored['drafts'] if i['requestId'] == body.requestId), None)
        if receipt:
            if receipt['commandHash'] != command_hash or receipt['actorId'] != actor_id:
                raise HTTPException(409, 'Этот номер запроса уже использован для другого действия.')
            return {'draft': deepcopy(receipt['draft']), 'canCreate': False,
                    'sourceRevision': receipt['sourceRevision']}
        if source['revision'] != body.expectedSourceRevision:
            raise HTTPException(409, 'Исходная сетка изменилась. Обновите её перед расчётом.')
        if any(i['draft']['weekStart'] == WEEK for i in stored['drafts']):
            raise HTTPException(409, 'Черновик этой недели уже сохранён. Обновите страницу.')
        if not source['lessons']:
            raise HTTPException(409, 'Не найдены исходные школьные уроки.')
        try:
            basis = _fictional_basis(source,connection)
            result = build_short_week_draft(source, deepcopy(CALENDAR), source_basis=basis,
                rules=basis.get('rules'), repairs=basis.get('repairs'))
        except (ValueError,KeyError,sqlite3.Error):
            raise HTTPException(409, 'Нужны собственный вымышленный пакет и точное соответствие подтверждённой модели исходным занятиям.') from None
        stamp = datetime.now(timezone.utc).isoformat()
        rules_binding = _current_rule_binding(connection)
        draft = {**result, 'id': 'dated_' + _hash([profile['schoolId'], WEEK])[:24],
            'revision': 1, 'weekStart': WEEK, 'title': '26–28 октября · вымышленная трёхдневная неделя',
            'teachingDates': CALENDAR['teachingDates'], 'closedDates': CALENDAR['closedDates'],
            'holidayEnd': None, 'sourceName': source.get('sourceName', ''),
            'sourcePeriod': basis['sourcePeriodStart']+' · вымышленная подтверждённая модель', 'scope': 'historical_carryover',
            'publicationBlocked': True, 'sourceChanged': rules_binding is None, 'createdAt': stamp,
            'notes': [
                'Это явно вымышленное упражнение: 26–28 октября учебные, 29–31 октября закрыты. Оно не задаёт настоящий календарь организации.',
                'Переносятся наблюдаемые дни собственной подтверждённой модели. Уроки закрытых дней не уплотняются и новая недельная нагрузка не выдумывается.',
                'Учителя, классы и кабинеты сверены с собственными подтверждёнными фактами. Чужие исторические назначения и исправления не используются.',
                'Черновик не согласован и не опубликован. Личные расписания учителей не изменены.']}
        if rules_binding is None:
            draft['notes'].append('Основание собственной вымышленной модели изменилось; нужна отдельная повторная проверка.')
        # The public repair list is explanatory; original detail remains in
        # repairsApplied/provenance for review, never in active lesson records.
        draft['repairs'] = result.get('repairsApplied', [])
        stored['drafts'].append({'draft': draft, 'sourceHash': _hash(source),
            'rulesPackageHash': rules_binding,
            'sourceRevision': source['revision'], 'requestId': body.requestId,
            'commandHash': command_hash, 'actorId': actor_id})
        payload = json.dumps(stored, ensure_ascii=False, allow_nan=False)
        if len(payload.encode()) > 3 * 1024 * 1024:
            raise HTTPException(429, 'Достигнут предел хранения датированного черновика.')
        state[NAMESPACE] = stored
        _actor(profile)
        connection.execute('UPDATE demo_state SET payload=? WHERE id=1', (json.dumps(state, ensure_ascii=False),))
        connection.commit()
        return _response(stored, source)


router = APIRouter(prefix='/api/timetable/dated-draft', tags=['dated draft'])


@router.get('')
def http_read(weekStart: Literal['2026-10-26'] = WEEK,
              profile=Depends(require_roles('admin', 'director'))):
    return read_dated_draft(profile)


@router.post('')
def http_create(body: CreateDatedDraft, profile=Depends(require_roles('admin', 'director'))):
    return create_dated_draft(body, profile)
