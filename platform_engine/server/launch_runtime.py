"""Durable offline launch workflow. No model calls, network sends or domain approval.

The host supplies current membership verification. Missing verifier fails closed.
State lives only in _launchRuntime; existing timetable/auth/schema are untouched.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import secrets
import time
import unicodedata
from typing import Annotated, Callable, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .auth import session_profile
from .db import edit_state, read_state

class SafeLaunchRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def handle(request):
            try:
                return await original(request)
            except RequestValidationError:
                raise HTTPException(422, 'Некорректные поля локальной задачи') from None
        return handle


router = APIRouter(prefix='/api/launch', tags=['offline-launch'], route_class=SafeLaunchRoute)
_verifier: Callable | None = None
_resolver: Callable | None = None
_respondents: Callable | None = None
_membership_status: Callable | None = None
TERMINAL = {'completed', 'cancelled', 'expired', 'stale', 'failed'}
MANAGERS = {'director', 'planner'}
MAX_RECORDS = 2000


def install_membership_verifier(verifier: Callable | None, resolver: Callable | None = None, respondents: Callable | None = None,
                                membership_status: Callable | None = None):
    """Host wiring only. Never set from an HTTP body or MAX identity claim."""
    global _verifier, _resolver, _respondents, _membership_status
    _verifier, _resolver, _respondents, _membership_status = verifier, resolver, respondents, membership_status


def now_ms():
    return int(time.time() * 1000)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def membership_state(profile):
    """Host-only authority: unavailable lookup is never evidence of revocation.

    Legacy boolean verifiers remain supported. New hosts should supply the
    explicit status callback when False can mean an unavailable authority.
    """
    if (not isinstance(profile, dict) or profile.get('membershipVerified') is not True
            or not all(isinstance(profile.get(k), str) and profile[k] for k in ('schoolId', 'userId', 'membershipId'))
            or type(profile.get('membershipRevision')) is not int or profile['membershipRevision'] < 0):
        return 'invalid'
    try:
        if _membership_status is not None:
            result = _membership_status(deepcopy(profile))
            return result if result in {'current', 'invalid', 'unverifiable'} else 'unverifiable'
        if _verifier is None:
            return 'unverifiable'
        return 'current' if _verifier(deepcopy(profile)) is True else 'invalid'
    except Exception:
        return 'unverifiable'


def current(profile):
    return membership_state(profile) == 'current'


def actor(profile, permission='launch.read'):
    if not current(profile) or permission not in profile.get('permissions', []):
        raise HTTPException(403, 'Нет действующего подтверждённого школьного доступа')
    role = profile.get('staffRole', profile.get('role'))
    if role not in {*MANAGERS, 'teacher', 'access_admin'}:
        raise HTTPException(403, 'Роль не поддерживает подготовку запуска')
    if permission == 'launch.manage' and role not in MANAGERS:
        raise HTTPException(403, 'Нужны полномочия директора или завуча')
    return deepcopy(profile)


def resolve_member(profile, membership_id):
    if membership_id == profile['membershipId']:
        result = profile
    else:
        try:
            result = _resolver(profile['schoolId'], membership_id) if _resolver else None
        except Exception:
            result = None
    if not current(result) or result['schoolId'] != profile['schoolId']:
        raise HTTPException(404, 'Действующее членство не найдено')
    return deepcopy(result)


def namespace(state):
    return state.setdefault('_launchRuntime', {'formatVersion': 1, 'revision': 0,
        'tasks': {}, 'questions': {}, 'inbox': {}, 'outbox': {}, 'bindings': {}, 'requests': {}})


def capacity(items):
    if len(items) >= MAX_RECORDS:
        raise HTTPException(429, 'Локальная очередь заполнена; требуется разбор оператором')


def bump(data):
    data['revision'] += 1


def public_task(task):
    result = {k: deepcopy(v) for k, v in task.items() if k not in {'actor', 'leaseHash', 'requestHash'}}
    result['deadlineExceeded'] = task['state'] not in TERMINAL and task['deadlineAt'] <= now_ms()
    return result


def visible_task(task, profile):
    result = public_task(task)
    if profile.get('staffRole', profile.get('role')) == 'teacher' and task['ownerId'] != profile['userId']:
        return {k: result[k] for k in ('id', 'title', 'state', 'revision', 'createdAt', 'deadlineAt', 'deadlineExceeded', 'nextAction')}
    return result


def task_for(data, task_id, profile):
    task = data['tasks'].get(task_id)
    if not task or task['schoolId'] != profile['schoolId']:
        raise HTTPException(404, 'Задача не найдена')
    if profile.get('staffRole', profile.get('role')) not in MANAGERS:
        if task['ownerId'] != profile['userId'] and not any(q['taskId'] == task_id and q['respondentId'] == profile['userId'] for q in data['questions'].values()):
            raise HTTPException(404, 'Задача не найдена')
    return task


def revision(record, expected):
    if record['revision'] != expected:
        raise HTTPException(409, 'Версия изменилась. Обновите карточку перед повтором')


def active_task(task):
    if task['state'] in TERMINAL:
        raise HTTPException(409, 'Задача завершена или отозвана')
    if task['deadlineAt'] <= now_ms():
        raise HTTPException(409, 'Срок задачи истёк')
    if not current(task['actor']):
        raise HTTPException(403, 'Доступ автора изменился; задача требует повторной проверки')


def receipt_key(profile, request_id):
    return digest([profile['schoolId'], profile['userId'], request_id])


def receipt(data, profile, request_id, payload):
    key, fingerprint = receipt_key(profile, request_id), digest(payload)
    old = data['requests'].get(key)
    if old and old['hash'] != fingerprint:
        raise HTTPException(409, 'Повторный идентификатор относится к другому запросу')
    return key, fingerprint, deepcopy(old['result']) if old else None


def save_receipt(data, key, fingerprint, result):
    capacity(data['requests'])
    data['requests'][key] = {'hash': fingerprint, 'result': deepcopy(result)}


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    @field_validator('*')
    @classmethod
    def safe_strings(cls, value):
        if isinstance(value, str) and (not value.strip() or any(unicodedata.category(c) == 'Cs' or
                (unicodedata.category(c) == 'Cc' and c not in '\n\r\t') for c in value)):
            raise ValueError('Нужен непустой текст без недопустимых символов')
        return value


RequestId = Annotated[str, Field(min_length=16, max_length=80, pattern=r'^[A-Za-z0-9_-]+$')]


class TaskCreate(Strict):
    title: Annotated[str, Field(min_length=1, max_length=180)]
    message: Annotated[str, Field(min_length=1, max_length=2000)]
    requestId: RequestId
    deadlineAt: Annotated[int, Field(gt=0)]


class TaskTransition(Strict):
    action: Literal['cancel', 'retry', 'invalidate', 'prepare']
    expectedRevision: Annotated[int, Field(ge=0)]
    requestId: RequestId


class QuestionCreate(Strict):
    text: Annotated[str, Field(min_length=1, max_length=1000)]
    respondentMembershipId: Annotated[str, Field(min_length=1, max_length=100)]
    expectedRevision: Annotated[int, Field(ge=0)]
    requestId: RequestId


class QuestionAnswer(Strict):
    text: Annotated[str, Field(min_length=1, max_length=2000)]
    expectedRevision: Annotated[int, Field(ge=0)]
    requestId: RequestId


@router.get('/status')
def status(profile=Depends(session_profile)):
    profile = actor(profile, 'launch.status')
    result = {'mode': 'offline_preparation', 'membershipCurrent': True, 'currentUserId': profile['userId'],
        'canReadTasks': 'launch.read' in profile.get('permissions', []),
        'canManage': profile.get('staffRole', profile.get('role')) in MANAGERS and 'launch.manage' in profile.get('permissions', []),
        'ai': {'enabled': False, 'readiness': 'off', 'keyConfigured': None,
               'keyNotice': 'Ключи не читаются этим модулем; живая модель не проверена', 'context': 'user_message_only'},
        'max': {'enabled': False, 'mode': 'dry_run_only', 'termsStatus': 'unknown', 'credentialsStatus': 'not_inspected',
                'webhookPublic': False, 'networkSenderImplemented': False, 'bindingActivation': 'blocked'},
        'local': {'persistentTasks': True, 'leases': True, 'inboxDedupe': True, 'outboxDryRun': True},
        'blockers': ['Внешние модельные вызовы отключены; требуется отдельное разрешение',
                     'Условия MAX, бот и допустимость сценария не подтверждены',
                     'MAX-привязки не активируются; live webhook и отправитель не опубликованы']}
    from .school_integrations import current_resources
    resources = current_resources()
    if resources is not None:
        if resources.settings.school_id != profile['schoolId']:
            raise HTTPException(403, 'Подключения относятся к другой школе')
        live = resources.status()
        ai, messenger = live['ai'], live['max']
        result['liveConnections'] = live
        result['ai'].update(enabled=ai['enabled'] or ai['managerEnabled'],
            readiness='enabled' if ai['enabled'] or ai['managerEnabled'] else 'off',
            keyConfigured=ai['keyConfigured'],
            keyNotice='Настройки ИИ проверены; доступность и качество живой модели требуют отдельной проверки')
        result['max'].update(enabled=messenger['enabled'],
            mode='personal_live' if messenger['sendEnabled'] else 'receive_only' if messenger['enabled'] else 'configured_off',
            networkSenderImplemented=True,
            bindingActivation='available' if messenger['enabled'] else 'blocked',
            credentialsStatus='configured' if messenger['tokenConfigured'] else 'missing')
        result['blockers'] = list(dict.fromkeys(ai['blockers'] + messenger['blockers']))
    return result


@router.get('/respondents')
def list_respondents(profile=Depends(session_profile)):
    profile = actor(profile, 'launch.manage')
    if _respondents is None:
        return {'respondents': [], 'configured': False, 'blockers': ['respondent_provider_not_configured']}
    try:
        candidates = _respondents(profile['schoolId'])
        if not isinstance(candidates, list) or len(candidates) > MAX_RECORDS:
            raise ValueError('Invalid respondent provider result')
    except Exception:
        return {'respondents': [], 'configured': False, 'blockers': ['respondent_provider_unavailable']}
    rows, seen = [], set()
    for candidate in candidates:
        if not isinstance(candidate, dict) or not isinstance(candidate.get('membershipId'), str):
            continue
        try:
            resolved = resolve_member(profile, candidate['membershipId'])
        except HTTPException:
            continue
        role = resolved.get('staffRole', resolved.get('role'))
        if (resolved['membershipId'] in seen or role not in {*MANAGERS, 'teacher'}
                or not {'launch.manage', 'launch.answer_own'}.intersection(resolved.get('permissions', []))):
            continue
        seen.add(resolved['membershipId'])
        name = resolved.get('name')
        rows.append({'membershipId': resolved['membershipId'], 'userId': resolved['userId'],
            'name': name.strip()[:120] if isinstance(name, str) and name.strip() else 'Сотрудник', 'staffRole': role})
    return {'respondents': sorted(rows, key=lambda row: (row['name'].casefold(), row['membershipId'])),
            'configured': True, 'blockers': [] if rows else ['no_active_respondents']}


@router.get('/tasks')
def list_tasks(profile=Depends(session_profile)):
    profile = actor(profile)
    data = namespace(read_state())
    rows = []
    for task_id in data['tasks']:
        try:
            rows.append(visible_task(task_for(data, task_id, profile), profile))
        except HTTPException:
            pass
    return {'tasks': sorted(rows, key=lambda x: x['createdAt'], reverse=True), 'mode': 'offline_preparation'}


@router.post('/tasks')
def create_task(body: TaskCreate, profile=Depends(session_profile)):
    profile = actor(profile, 'launch.manage')
    with edit_state() as state:
        data = namespace(state)
        key, fp, old = receipt(data, profile, body.requestId, {'operation': 'create_task', **body.model_dump()})
        if old is not None:
            return old
        if not now_ms() < body.deadlineAt <= now_ms() + 31 * 86400000:
            raise HTTPException(422, 'Срок должен быть в будущем, не далее 31 дня')
        capacity(data['tasks'])
        task_id = 'ln_' + secrets.token_urlsafe(18)
        task = {'id': task_id, 'schoolId': profile['schoolId'], 'ownerId': profile['userId'], 'actor': profile,
            'title': body.title.strip(), 'message': body.message, 'state': 'queued', 'revision': 0,
            'createdAt': now_ms(), 'updatedAt': now_ms(), 'deadlineAt': body.deadlineAt,
            'nextAction': 'Выполнить локальную проверку подготовки', 'attempt': 0, 'leaseUntil': None,
            'inputHash': digest(body.model_dump(exclude={'requestId'})), 'result': None}
        data['tasks'][task_id] = task
        bump(data)
        result = public_task(task)
        save_receipt(data, key, fp, result)
        return result


@router.get('/tasks/{task_id}')
def get_task(task_id: str, profile=Depends(session_profile)):
    profile = actor(profile)
    data = namespace(read_state())
    task = task_for(data, task_id, profile)
    questions = [{k: deepcopy(v) for k, v in q.items() if k != 'respondentActor'} for q in data['questions'].values() if q['taskId'] == task_id and
                 (profile.get('staffRole', profile.get('role')) in MANAGERS or q['respondentId'] == profile['userId'])]
    result = visible_task(task, profile)
    return {'task': result, 'questions': questions}


@router.post('/tasks/{task_id}/transition')
def transition(task_id: str, body: TaskTransition, profile=Depends(session_profile)):
    profile = actor(profile, 'launch.manage')
    with edit_state() as state:
        data = namespace(state)
        key, fp, old = receipt(data, profile, body.requestId, {'taskId': task_id, **body.model_dump()})
        if old is not None:
            return old
        task = task_for(data, task_id, profile)
        revision(task, body.expectedRevision)
        if body.action == 'cancel':
            if task['state'] in TERMINAL:
                raise HTTPException(409, 'Задача уже завершена')
            task.update(state='cancelled', nextAction='Отменена сотрудником', leaseUntil=None)
        elif body.action == 'invalidate':
            active_task(task)
            task.update(state='stale', nextAction='Создать новую задачу с актуальными входами', leaseUntil=None)
        elif body.action == 'retry':
            if task['deadlineAt'] <= now_ms() or not current(task['actor']):
                raise HTTPException(409, 'Срок или доступ изменились')
            if task['state'] not in {'blocked', 'failed'}:
                raise HTTPException(409, 'Повтор сейчас недоступен')
            task.update(state='queued', nextAction='Повторить локальную проверку', leaseUntil=None)
        else:
            active_task(task)
            if task['state'] != 'queued':
                raise HTTPException(409, 'Локальная проверка доступна для очереди')
            task.update(state='blocked', attempt=task['attempt'] + 1,
                        nextAction='Нужны отдельное разрешение и принятый внешний контур',
                        result={'kind': 'offline_readiness_check', 'externalCalls': 0, 'schoolMutations': 0,
                                'message': 'Локальная карточка проверена и сохранена. ИИ и MAX не запускались.'})
        task.pop('leaseHash', None)
        task['revision'] += 1
        task['updatedAt'] = now_ms()
        for item in data['outbox'].values():
            if item['taskId'] == task_id and item['state'] == 'pending' and task['state'] in TERMINAL:
                item['state'] = 'cancelled'
        bump(data)
        result = public_task(task)
        save_receipt(data, key, fp, result)
        return result


def claim_task(task_id, profile, expected_revision, lease_seconds=60):
    """Local worker callable; returns a fencing token once, never in GET APIs."""
    profile = actor(profile, 'launch.manage')
    if type(lease_seconds) is not int or not 1 <= lease_seconds <= 300:
        raise HTTPException(422, 'Недопустимая длительность lease')
    with edit_state() as state:
        data = namespace(state)
        task = task_for(data, task_id, profile)
        revision(task, expected_revision)
        active_task(task)
        if task['state'] != 'queued':
            raise HTTPException(409, 'Задача уже занята')
        token = secrets.token_urlsafe(32)
        task.update(state='running', leaseHash=digest(token), leaseUntil=min(task['deadlineAt'], now_ms() + lease_seconds * 1000),
                    nextAction='Ожидать локальный worker', attempt=task['attempt'] + 1, revision=task['revision'] + 1)
        bump(data)
        return {'task': public_task(task), 'leaseToken': token}


def finish_local_task(task_id, profile, lease_token, expected_revision, input_hash):
    profile = actor(profile, 'launch.manage')
    with edit_state() as state:
        data = namespace(state)
        task = task_for(data, task_id, profile)
        revision(task, expected_revision)
        active_task(task)
        if (task['state'] != 'running' or task['leaseUntil'] <= now_ms() or not isinstance(lease_token, str)
                or not secrets.compare_digest(task.get('leaseHash', ''), digest(lease_token)) or task['inputHash'] != input_hash):
            raise HTTPException(409, 'Устаревший результат или lease')
        task.update(state='completed', nextAction='Локальная подготовка завершена; внешние действия не выполнялись',
                    leaseUntil=None, revision=task['revision'] + 1,
                    result={'kind': 'offline_contract', 'externalCalls': 0, 'schoolMutations': 0})
        task.pop('leaseHash', None)
        bump(data)
        return public_task(task)


def recover_tasks(profile):
    """Explicit bounded sweep. Leases recover only local work, never unknown sends."""
    profile = actor(profile, 'launch.manage')
    with edit_state() as state:
        data = namespace(state)
        changes = []
        deferred = []
        for task in data['tasks'].values():
            if task['schoolId'] != profile['schoolId'] or task['state'] in TERMINAL:
                continue
            authority = membership_state(task['actor'])
            if authority == 'unverifiable':
                deferred.append(task['id'])
                continue
            target = 'stale' if authority == 'invalid' else 'expired' if task['deadlineAt'] <= now_ms() else 'queued' if task['state'] == 'running' and task['leaseUntil'] <= now_ms() else None
            if target:
                task.update(state=target, revision=task['revision'] + 1, leaseUntil=None,
                            nextAction='Проверить актуальность входов и доступа' if target != 'queued' else 'Повторить прерванную локальную подготовку')
                task.pop('leaseHash', None)
                changes.append(task['id'])
        for item in data['outbox'].values():
            task = data['tasks'].get(item.get('taskId'))
            if item['schoolId'] == profile['schoolId'] and item['state'] == 'pending' and task and task['state'] in TERMINAL - {'completed'}:
                item['state'] = 'cancelled'
        if changes:
            bump(data)
        return {'changedTaskIds': changes, 'deferredTaskIds': deferred}


@router.post('/recover')
def recover_queue(profile=Depends(session_profile)):
    return recover_tasks(profile)


@router.post('/tasks/{task_id}/questions')
def ask_question(task_id: str, body: QuestionCreate, profile=Depends(session_profile)):
    profile = actor(profile, 'launch.manage')
    respondent = resolve_member(profile, body.respondentMembershipId)
    with edit_state() as state:
        data = namespace(state)
        key, fp, old = receipt(data, profile, body.requestId, {'taskId': task_id, **body.model_dump()})
        if old is not None:
            return old
        task = task_for(data, task_id, profile)
        revision(task, body.expectedRevision)
        active_task(task)
        if task['state'] not in {'queued', 'blocked'}:
            raise HTTPException(409, 'Вопрос сейчас недоступен')
        capacity(data['questions'])
        qid = 'lq_' + secrets.token_urlsafe(18)
        question = {'id': qid, 'taskId': task_id, 'schoolId': profile['schoolId'], 'text': body.text,
                    'respondentId': respondent['userId'], 'respondentActor': respondent,
                    'state': 'open', 'revision': 0, 'deadlineAt': task['deadlineAt'], 'answer': None}
        data['questions'][qid] = question
        task.update(state='waiting_reply', nextAction='Ожидать ответ на конкретный вопрос', revision=task['revision'] + 1)
        bump(data)
        result = {k: deepcopy(v) for k, v in question.items() if k != 'respondentActor'}
        save_receipt(data, key, fp, result)
        return result


@router.post('/questions/{question_id}/answer')
def answer_question(question_id: str, body: QuestionAnswer, profile=Depends(session_profile)):
    profile = actor(profile, 'launch.answer_own') if profile.get('staffRole', profile.get('role')) == 'teacher' else actor(profile, 'launch.manage')
    with edit_state() as state:
        return answer_in_namespace(namespace(state), question_id, body, profile)


def answer_in_namespace(data, question_id, body, profile):
    """Shared atomic question contract for portal and authenticated offline MAX event."""
    profile = actor(profile, 'launch.answer_own') if profile.get('staffRole', profile.get('role')) == 'teacher' else actor(profile, 'launch.manage')
    q = data['questions'].get(question_id)
    if not q or q['schoolId'] != profile['schoolId'] or q['respondentId'] != profile['userId']:
        raise HTTPException(404, 'Вопрос не найден')
    if not current(q['respondentActor']) or q['respondentActor']['membershipRevision'] != profile['membershipRevision']:
        raise HTTPException(403, 'Полномочия адресата изменились')
    task = task_for(data, q['taskId'], profile)
    active_task(task)
    key, fp, old = receipt(data, profile, body.requestId, {'questionId': question_id, **body.model_dump()})
    if old is not None:
        return old
    revision(q, body.expectedRevision)
    if q['state'] != 'open' or task['state'] != 'waiting_reply' or q['deadlineAt'] <= now_ms():
        raise HTTPException(409, 'Вопрос закрыт или устарел')
    q.update(state='answered', answer=body.text, revision=q['revision'] + 1)
    task.update(state='queued', revision=task['revision'] + 1, nextAction='Проверить полученный ответ; школьные данные не изменены')
    bump(data)
    result = {k: deepcopy(v) for k, v in q.items() if k != 'respondentActor'}
    save_receipt(data, key, fp, result)
    return result


@router.get('/outbox')
def list_outbox(profile=Depends(session_profile)):
    profile = actor(profile, 'launch.manage')
    return {'mode': 'dry_run_only', 'items': [deepcopy(x) for x in namespace(read_state())['outbox'].values() if x['schoolId'] == profile['schoolId']]}


class DryRun(Strict):
    expectedRevision: Annotated[int, Field(ge=0)]


@router.post('/outbox/{outbox_id}/dry-run')
def outbox_dry_run(outbox_id: str, body: DryRun, profile=Depends(session_profile)):
    profile = actor(profile, 'launch.manage')
    data = namespace(read_state())
    item = data['outbox'].get(outbox_id)
    if not item or item['schoolId'] != profile['schoolId']:
        raise HTTPException(404, 'Запись не найдена')
    revision(item, body.expectedRevision)
    return {'sent': False, 'state': item['state'], 'enabled': False, 'reason': 'MAX offline: договор и рабочий канал не подтверждены', 'externalCalls': 0}
