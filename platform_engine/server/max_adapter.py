"""Offline MAX contracts. Intentionally contains no HTTP client or live sender.

receive() is a local callable, NOT an ASGI/webhook route. The host must supply
verified membership and independently verified two-identity binding proofs.
MAX remains disabled even when a synthetic transport result is recorded.
"""
from __future__ import annotations

from copy import deepcopy
import json
import re
import secrets
from pydantic import ValidationError

from fastapi import HTTPException

from . import launch_runtime as runtime
from .db import edit_state, read_state

MAX_ENABLED = False
MAX_BODY_BYTES = 65536
EVENTS = {'message_created', 'message_callback', 'bot_started', 'bot_stopped',
          'dialog_removed', 'bot_removed', 'bot_admin_permissions_changed', 'message_edited', 'message_removed'}


def no_duplicates(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError('Duplicate JSON field')
        obj[key] = value
    return obj


def native_id(value):
    if type(value) is int and -(2 ** 63) <= value < 2 ** 63:
        return str(value)
    if isinstance(value, str) and 1 <= len(value) <= 180 and re.fullmatch(r'[A-Za-z0-9_.:-]+', value):
        return value
    raise HTTPException(422, 'Некорректный идентификатор MAX')


def identity_id(value):
    raw = native_id(value)
    if not re.fullmatch(r'-?(0|[1-9][0-9]*)', raw) or not -(2 ** 63) <= int(raw) < 2 ** 63:
        raise HTTPException(422, 'Некорректный числовой MAX ID')
    return raw


class OfflineMaxAdapter:
    def __init__(self, *, integration_id: str, school_id: str, webhook_secret: str,
                 binding_proof_provider=None, workflow='launch'):
        if (not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', integration_id) or not school_id
                or not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', webhook_secret)):
            raise ValueError('Explicit bounded offline adapter configuration required')
        if workflow not in {'launch', 'school_requests'}:
            raise ValueError('Unsupported offline workflow')
        self.integration_id, self.school_id = integration_id, school_id
        self._secret = webhook_secret
        self._proof_provider = binding_proof_provider
        self.workflow = workflow

    def _require_workflow_domain(self):
        if self.workflow == 'school_requests':
            from .school_requests import require_bound_school
            require_bound_school(self.school_id)

    def _request_binding(self, data, sender_id, chat_id):
        binding = next((b for b in data['bindings'].values()
            if b['schoolId'] == self.school_id and b['integrationId'] == self.integration_id
            and b['maxUserId'] == sender_id and b['privateChatId'] == chat_id
            and b['state'] == 'verified_offline'), None)
        if (not binding or binding['actor']['schoolId'] != self.school_id
                or binding['actor'].get('staffRole', binding['actor'].get('role')) != 'teacher'
                or not runtime.current(binding['actor'])):
            raise HTTPException(403, 'Нет действующей проверенной личной привязки учителя')
        return binding

    @staticmethod
    def _event_after_binding(value, binding):
        # This is an offline, host-clock fence. No native event may establish a
        # new binding generation, and a future/malformed timestamp grants no rights.
        verified = binding.get('verifiedAt')
        if (type(verified) is not int or not binding.get('generation')
                or value['timestamp'] <= verified or value['timestamp'] > runtime.now_ms()):
            raise HTTPException(403, 'Событие не относится к текущему подтверждённому поколению связи')

    def _receive_request_control(self, value, event_key, fingerprint, sender_id, chat_id):
        with edit_state() as state:
            data = runtime.namespace(state)
            binding = next((b for b in data['bindings'].values() if b['schoolId'] == self.school_id
                and b['integrationId'] == self.integration_id and b['maxUserId'] == sender_id
                and b['privateChatId'] == chat_id and b['state'] == 'verified_offline'), None)
            old = data['inbox'].get(event_key)
            if old and old['payloadHash'] != fingerprint:
                raise HTTPException(409, 'Идентификатор события повторён иначе')
            if old:
                return {'acceptedLocally':True, 'duplicate':True, 'eventId':event_key,
                        'maxEnabled':False, 'httpPublished':False}
            if not binding:
                raise HTTPException(403, 'Нет текущей личной связи для управляющего события')
            self._event_after_binding(value, binding)
            runtime.capacity(data['inbox'])
            binding.update(state='revoked', revision=binding['revision']+1, revokedAt=runtime.now_ms())
            for item in data['outbox'].values():
                if (item['schoolId'] == self.school_id and item['integrationId'] == self.integration_id
                        and item['bindingId'] == binding['id'] and item['state'] == 'pending'):
                    item.update(state='cancelled', revision=item['revision']+1)
            data['inbox'][event_key] = {'id':event_key, 'schoolId':self.school_id,
                'integrationId':self.integration_id, 'workflow':'school_requests', 'eventType':value['update_type'],
                'chatId':chat_id, 'senderId':sender_id, 'bindingId':binding['id'],
                'bindingGeneration':binding['generation'], 'payloadHash':fingerprint,
                'occurredAt':value['timestamp'], 'receivedAt':runtime.now_ms(), 'state':'recorded'}
            runtime.bump(data)
            return {'acceptedLocally':True, 'duplicate':False, 'eventId':event_key,
                    'maxEnabled':False, 'httpPublished':False}

    def _request_reply_outbox(self, data, result, binding, event_key, *, question=None):
        request = result['request']
        if question is not None:
            reply = question['text']
        else:
            reply = result['serverReply']
            open_questions = [q for q in request['questions'] if q['state'] == 'open'
                and q['respondentMembershipId'] == binding['actor']['membershipId']]
            question = open_questions[0] if len(open_questions) == 1 else None
        if not isinstance(reply, str) or not reply.strip():
            raise HTTPException(422, 'Нет короткого ответа школьной заявки')
        text = reply.strip()
        if len(text) > 800:
            text = text[:760].rstrip() + '… Подробнее в школьном кабинете.'
        ident = runtime.digest([self.school_id, self.integration_id, 'school_request_reply', event_key])
        fingerprint = runtime.digest([request['id'], request['revision'], binding['id'], binding['revision'],
                                      text, question['id'] if question else None])
        existing = data['outbox'].get(ident)
        if existing:
            if existing['envelopeHash'] != fingerprint:
                raise HTTPException(409, 'Ответ этому событию уже подготовлен иначе')
            return existing
        runtime.capacity(data['outbox'])
        item = {'id': ident, 'schoolId': self.school_id, 'integrationId': self.integration_id,
            'sourceKind': 'school_request', 'schoolRequestId': request['id'], 'taskId': None,
            'sourceEventKey': event_key, 'requestRevision': request['revision'],
            'bindingId': binding['id'], 'bindingRevision': binding['revision'], 'chatId': binding['privateChatId'],
            'bindingGeneration':binding['generation'],
            'questionId': question['id'] if question else None,
            'callbackContract': {'schoolRequestId': request['id'], 'questionId': question['id'],
                'expectedRevision': request['revision'], 'bindingId':binding['id'],
                'bindingRevision':binding['revision'], 'bindingGeneration':binding['generation']} if question else None,
            'state': 'pending', 'revision': 0, 'envelopeHash': fingerprint, 'text': text,
            'mode': 'dry_run_only', 'externalCalls': 0, 'liveEligible': False,
            'approvalStatus': 'not_applicable_no_school_mutation', 'providerMessageId': None,
            'nextAction': 'Только локальная подготовка ответа; MAX и внешняя сеть выключены'}
        data['outbox'][ident] = item
        return item

    def _receive_school_request(self, value, event_key, fingerprint, sender_id, chat_id):
        """One DB transaction: native inbox receipt + actual request + dry-run reply."""
        from . import school_requests
        message, callback = value.get('message') or {}, value.get('callback') or {}
        if message.get('link'):
            raise HTTPException(403, 'Пересланное сообщение не является заявкой или ответом сотрудника')
        with edit_state() as state:
            data = runtime.namespace(state)
            binding = self._request_binding(data, sender_id, chat_id)
            old = data['inbox'].get(event_key)
            if old:
                if (old.get('bindingId') != binding['id']
                        or old.get('bindingRevision') != binding['revision']):
                    raise HTTPException(403, 'Событие относится к прежней отозванной личной связи')
                if old['payloadHash'] != fingerprint:
                    raise HTTPException(409, 'Идентификатор MAX повторён с другим содержимым')
                if old.get('workflow') != 'school_requests':
                    raise HTTPException(409, 'Событие уже относится к другому локальному процессу')
                result = deepcopy(old['result']); result['duplicate'] = True
                return result
            self._event_after_binding(value, binding)
            runtime.capacity(data['inbox'])
            actor = deepcopy(binding['actor'])
            if value['update_type'] == 'message_created':
                text = (message.get('body') or {}).get('text')
                if (not isinstance(text, str) or not 1 <= len(text.strip()) <= 2000
                        or any(ord(c) < 32 and c not in '\n\r\t' for c in text)):
                    raise HTTPException(422, 'Нужен поддерживаемый текст заявки')
                result = school_requests.create_request_in_state(state,
                    {'requestId': 'max_create_' + event_key, 'text': text}, actor)
            else:
                try:
                    payload = callback.get('payload')
                    if not isinstance(payload, str) or len(payload) > 3000:
                        raise ValueError()
                    answer = json.loads(payload, object_pairs_hook=no_duplicates,
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                    if not isinstance(answer, dict) or set(answer) != {'schoolRequestId', 'questionId', 'text', 'expectedRevision',
                            'bindingId','bindingRevision','bindingGeneration'}:
                        raise ValueError()
                    request_id, question_id = answer['schoolRequestId'], answer['questionId']
                    if (not isinstance(request_id, str) or not 1 <= len(request_id) <= 100
                            or not isinstance(question_id, str) or not 1 <= len(question_id) <= 100):
                        raise ValueError()
                except (ValueError, TypeError):
                    raise HTTPException(422, 'Нужен ответ на конкретный вопрос школьной заявки') from None
                if (answer['bindingId'] != binding['id'] or type(answer['bindingRevision']) is not int
                        or answer['bindingRevision'] != binding['revision'] or answer['bindingGeneration'] != binding['generation']):
                    raise HTTPException(403, 'Кнопка относится к прежней личной привязке')
                envelope = next((item for item in data['outbox'].values()
                    if item.get('sourceKind') == 'school_request' and item['schoolId'] == self.school_id
                    and item['integrationId'] == self.integration_id and item['bindingId'] == binding['id']
                    and item.get('bindingGeneration') == binding['generation']
                    and item.get('callbackContract') == {key:answer[key] for key in answer if key != 'text'}
                    and item['state'] == 'pending'), None)
                if envelope is None:
                    raise HTTPException(403, 'Вопрос не подготовлен для текущей личной связи')
                result = school_requests.reply_request_in_state(state, request_id,
                    {'requestId': 'max_answer_' + event_key, 'expectedRevision': answer['expectedRevision'],
                     'text': answer['text']}, actor, question_id=question_id)
                envelope.update(state='answered_offline', revision=envelope['revision']+1)
            # Recheck after the workflow helper; current membership is never a
            # permanent permission cached when the MAX binding was recorded.
            self._request_binding(data, sender_id, chat_id)
            outbox = self._request_reply_outbox(data, result, binding, event_key)
            response = {'acceptedLocally': True, 'duplicate': False, 'eventId': event_key,
                'taskId': None, 'schoolRequestId': result['request']['id'], 'outboxId': outbox['id'],
                'requestRevision': result['request']['revision'], 'maxEnabled': False, 'httpPublished': False}
            data['inbox'][event_key] = {'id': event_key, 'integrationId': self.integration_id,
                'schoolId': self.school_id, 'workflow': 'school_requests', 'eventType': value['update_type'],
                'chatId': chat_id, 'senderId': sender_id, 'bindingId': binding['id'],
                'bindingRevision': binding['revision'], 'payloadHash': fingerprint,
                'bindingGeneration':binding['generation'],
                'occurredAt': value['timestamp'], 'receivedAt': runtime.now_ms(),
                'state': 'request_created' if value['update_type'] == 'message_created' else 'request_answered',
                'schoolRequestId': result['request']['id'], 'result': deepcopy(response)}
            runtime.bump(data)
            return response

    def receive(self, body: bytes, headers: list[tuple[str, str]]):
        if not isinstance(body, bytes) or len(body) > MAX_BODY_BYTES:
            raise HTTPException(413, 'Превышен размер события')
        credentials = [value for name, value in headers if name.lower() == 'x-max-bot-api-secret']
        if (len(credentials) != 1 or not isinstance(credentials[0], str) or not credentials[0].isascii() or len(credentials[0]) > 256
                or not secrets.compare_digest(credentials[0].encode(), self._secret.encode())):
            raise HTTPException(403, 'Событие не подтверждено')
        try:
            value = json.loads(body, object_pairs_hook=no_duplicates,
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid number')))
            json.dumps(value, ensure_ascii=False).encode('utf-8')
        except (ValueError, UnicodeError, RecursionError):
            raise HTTPException(422, 'Некорректное событие') from None
        if (not isinstance(value, dict) or value.get('update_type') not in EVENTS
                or type(value.get('timestamp')) is not int or value['timestamp'] < 0):
            raise HTTPException(422, 'Неподдерживаемое событие')
        event_type = value['update_type']
        message = value.get('message') or {}
        callback = value.get('callback') or {}
        if not isinstance(message, dict) or not isinstance(callback, dict):
            raise HTTPException(422, 'Некорректный объект события')
        recipient, msg_body = message.get('recipient') or {}, message.get('body') or {}
        sender = callback.get('user') if event_type == 'message_callback' else message.get('sender') if event_type.startswith('message_') else value.get('user')
        if not all(isinstance(v, dict) for v in (recipient, msg_body)) or (sender is not None and not isinstance(sender, dict)):
            raise HTTPException(422, 'Некорректный отправитель')
        chat_id = identity_id(value.get('chat_id', recipient.get('chat_id')))
        if ('chat_id' in value and 'chat_id' in recipient
                and identity_id(value['chat_id']) != identity_id(recipient['chat_id'])):
            raise HTTPException(422, 'Противоречивый адрес события')
        sender_id = identity_id(sender['user_id']) if sender and 'user_id' in sender else None
        native = callback.get('callback_id') if event_type == 'message_callback' else msg_body.get('mid') if event_type == 'message_created' else None
        if event_type in {'message_created', 'message_callback'} and native is None:
            raise HTTPException(422, 'Нет нативного идентификатора события')
        # No root update_id is assumed. Non-native events include timestamp and
        # canonical content so repeats collapse while different changes survive.
        event_key = runtime.digest([self.school_id, self.integration_id, event_type, chat_id,
            native_id(native) if native is not None else [value['timestamp'], runtime.digest(value)]])
        fingerprint = runtime.digest(value)
        self._require_workflow_domain()
        if self.workflow == 'school_requests' and event_type in {'message_created', 'message_callback'}:
            return self._receive_school_request(value, event_key, fingerprint, sender_id, chat_id)
        if self.workflow == 'school_requests' and event_type in {'bot_stopped','dialog_removed','bot_removed','bot_admin_permissions_changed'}:
            return self._receive_request_control(value,event_key,fingerprint,sender_id,chat_id)
        with edit_state() as state:
            data = runtime.namespace(state)
            old = data['inbox'].get(event_key)
            if old:
                if old['payloadHash'] != fingerprint:
                    raise HTTPException(409, 'Идентификатор MAX повторён с другим содержимым')
                return {'acceptedLocally': True, 'duplicate': True, 'eventId': event_key,
                        'maxEnabled': False, 'httpPublished': False}
            runtime.capacity(data['inbox'])
            event = {'id': event_key, 'integrationId': self.integration_id, 'schoolId': self.school_id,
                     'eventType': event_type, 'chatId': chat_id, 'senderId': sender_id,
                     'payloadHash': fingerprint, 'occurredAt': value['timestamp'], 'receivedAt': runtime.now_ms(),
                     'state': 'quarantined', 'reason': 'Нет подтверждённой привязки', 'taskId': None}
            binding = next((b for b in data['bindings'].values() if b['integrationId'] == self.integration_id
                            and b['schoolId'] == self.school_id and b['actor']['schoolId'] == self.school_id and b['maxUserId'] == sender_id
                            and b['privateChatId'] == chat_id and b['state'] == 'verified_offline'), None)
            if event_type in {'bot_stopped', 'dialog_removed', 'bot_removed', 'bot_admin_permissions_changed'}:
                for b in data['bindings'].values():
                    if b['integrationId'] == self.integration_id and b['schoolId'] == self.school_id and b['privateChatId'] == chat_id:
                        b.update(state='revoked', revision=b['revision'] + 1)
                for item in data['outbox'].values():
                    if item['integrationId'] == self.integration_id and item['schoolId'] == self.school_id and item['chatId'] == chat_id and item['state'] == 'pending':
                        item.update(state='cancelled', revision=item['revision'] + 1)
                event.update(state='recorded', reason='Остановка/изменение прав: очередь отозвана')
            elif event_type == 'message_created' and binding and runtime.current(binding['actor']):
                text = msg_body.get('text')
                # Forwarded material is not an actor assertion or an instruction.
                if message.get('link') or not isinstance(text, str) or not 1 <= len(text) <= 2000 or not text.strip() or any(ord(c) < 32 and c not in '\n\r\t' for c in text):
                    event['reason'] = 'Пересланный или неподдерживаемый ввод'
                else:
                    runtime.capacity(data['tasks'])
                    task_id = 'ln_' + secrets.token_urlsafe(18)
                    data['tasks'][task_id] = {'id': task_id, 'schoolId': self.school_id,
                        'ownerId': binding['actor']['userId'], 'actor': deepcopy(binding['actor']),
                        'title': 'Входящий вопрос MAX · локальная подготовка', 'message': text,
                        'state': 'queued', 'revision': 0, 'createdAt': runtime.now_ms(), 'updatedAt': runtime.now_ms(),
                        'deadlineAt': runtime.now_ms() + 86400000, 'nextAction': 'Проверить локальный вход; внешний агент отключён',
                        'attempt': 0, 'leaseUntil': None, 'inputHash': runtime.digest(text), 'sourceEventKey': event_key, 'result': None}
                    event.update(state='queued', reason='Локальная задача создана; внешних вызовов нет', taskId=task_id)
            elif event_type == 'message_callback' and binding and runtime.current(binding['actor']):
                try:
                    payload = callback.get('payload')
                    if not isinstance(payload, str) or len(payload) > 3000:
                        raise ValueError('Invalid callback payload')
                    answer = json.loads(payload, object_pairs_hook=no_duplicates)
                    if not isinstance(answer, dict) or set(answer) != {'questionId', 'text', 'expectedRevision'}:
                        raise ValueError('Invalid callback fields')
                    qid = answer.pop('questionId')
                    candidate = deepcopy(data)
                    result = runtime.answer_in_namespace(candidate, qid,
                        runtime.QuestionAnswer(**answer, requestId='max_' + event_key[:60]), binding['actor'])
                    data.clear()
                    data.update(candidate)
                    event.update(state='answered', reason='Ответ принят на конкретный вопрос; школьные факты ещё не применены', taskId=result['taskId'])
                except (HTTPException, ValueError, ValidationError, TypeError):
                    event['reason'] = 'Ответ не относится к открытому вопросу этого сотрудника'
            data['inbox'][event_key] = event
            runtime.bump(data)
            return {'acceptedLocally': True, 'duplicate': False, 'eventId': event_key,
                    'taskId': event['taskId'], 'maxEnabled': False, 'httpPublished': False}

    def record_verified_binding(self, proof_reference, profile):
        """Host-only proof interface; never trust a MAX ID/admin claim in JSON.

        Proof provider must establish control of both identities, bind this bot,
        and enforce one-use/freshness. No activation for real transport exists.
        """
        profile = runtime.actor(profile, 'launch.read')
        self._require_workflow_domain()
        if profile['schoolId'] != self.school_id or not self._proof_provider:
            raise HTTPException(403, 'Проверенная процедура привязки не подключена')
        proof = self._proof_provider(proof_reference, deepcopy(profile))
        if (not isinstance(proof, dict) or proof.get('verifiedBothIdentities') is not True
                or proof.get('employeeId') != profile['userId'] or proof.get('membershipRevision') != profile['membershipRevision']
                or proof.get('schoolId') != self.school_id or proof.get('integrationId') != self.integration_id
                or not isinstance(proof.get('proofId'), str) or not proof['proofId']
                or type(proof.get('expiresAt')) is not int or proof['expiresAt'] <= runtime.now_ms()):
            raise HTTPException(403, 'Доказательство связи не подтверждено')
        uid, chat = identity_id(proof.get('maxUserId')), identity_id(proof.get('privateChatId'))
        with edit_state() as state:
            runtime.actor(profile, 'launch.read')
            data = runtime.namespace(state)
            if any(b['proofId'] == proof['proofId'] for b in data['bindings'].values()):
                raise HTTPException(409, 'Доказательство уже использовано')
            if any(b['schoolId'] == self.school_id and b['integrationId'] == self.integration_id and b['state'] == 'verified_offline'
                   and (b['maxUserId'] == uid or b['actor']['userId'] == profile['userId']) for b in data['bindings'].values()):
                raise HTTPException(409, 'Связь уже занята; нужен явный отзыв')
            runtime.capacity(data['bindings'])
            identity = 'lb_' + secrets.token_urlsafe(18)
            data['bindings'][identity] = {'id': identity, 'integrationId': self.integration_id, 'schoolId': self.school_id,
                'actor': profile, 'maxUserId': uid, 'privateChatId': chat, 'proofId': proof['proofId'],
                'state': 'verified_offline', 'revision': 0, 'liveEnabled': False}
            if self.workflow == 'school_requests':
                data['bindings'][identity].update(verifiedAt=runtime.now_ms(), generation=secrets.token_urlsafe(24))
            runtime.bump(data)
            return {'id': identity, 'state': 'verified_offline', 'liveEnabled': False}

    def prepare_request_question_outbox(self, request_id, question_id, binding_id, business_key, profile):
        """Host-only manager preparation; actual question/recipient come from the workflow."""
        from . import school_requests
        profile = runtime.actor(profile, 'launch.manage')
        self._require_workflow_domain()
        if (self.workflow != 'school_requests' or profile['schoolId'] != self.school_id
                or not isinstance(business_key, str) or not 16 <= len(business_key) <= 160):
            raise HTTPException(403, 'Контур школьной заявки не подтверждён')
        with edit_state() as state:
            data = runtime.namespace(state)
            target = data['bindings'].get(binding_id)
            if not target:
                raise HTTPException(403, 'Адресат не привязан')
            binding = self._request_binding(data, target['maxUserId'], target['privateChatId'])
            if binding['id'] != binding_id:
                raise HTTPException(403, 'Адресат не совпадает с действующей связью')
            result = school_requests.get_request_in_state(state, request_id, profile)
            question = next((q for q in result['request']['questions'] if q['id'] == question_id), None)
            if (not question or question['state'] != 'open'
                    or question['respondentMembershipId'] != binding['actor']['membershipId']
                    or question['respondentRevision'] != binding['actor']['membershipRevision']):
                raise HTTPException(403, 'Вопрос не назначен текущему адресату')
            count_before = len(data['outbox'])
            item = self._request_reply_outbox(data, result, binding,
                runtime.digest(['manager_question', business_key]), question=question)
            if len(data['outbox']) != count_before:
                runtime.bump(data)
            return deepcopy(item)

    def prepare_outbox(self, task_id, binding_id, text, business_key, profile):
        profile = runtime.actor(profile, 'launch.manage')
        if profile['schoolId'] != self.school_id:
            raise HTTPException(403, 'Школа интеграции не совпадает с действующим доступом')
        if not isinstance(text, str) or not 1 <= len(text) <= 4000 or not isinstance(business_key, str) or not 16 <= len(business_key) <= 160:
            raise HTTPException(422, 'Недопустимый локальный конверт')
        with edit_state() as state:
            data = runtime.namespace(state)
            task = runtime.task_for(data, task_id, profile)
            runtime.active_task(task)
            binding = data['bindings'].get(binding_id)
            if (not binding or binding['schoolId'] != self.school_id or binding['actor']['schoolId'] != self.school_id or binding['integrationId'] != self.integration_id
                    or binding['state'] != 'verified_offline' or not runtime.current(binding['actor'])):
                raise HTTPException(403, 'Действующая проверенная привязка отсутствует')
            key = runtime.digest([self.school_id, self.integration_id, business_key])
            fingerprint = runtime.digest([task_id, binding_id, binding['revision'], text])
            old = data['outbox'].get(key)
            if old:
                if old['envelopeHash'] != fingerprint:
                    raise HTTPException(409, 'Ключ исходящего относится к другому содержимому')
                return deepcopy(old)
            runtime.capacity(data['outbox'])
            item = {'id': key, 'schoolId': self.school_id, 'integrationId': self.integration_id,
                    'taskId': task_id, 'bindingId': binding_id, 'bindingRevision': binding['revision'],
                    'chatId': binding['privateChatId'], 'state': 'pending', 'revision': 0,
                    'envelopeHash': fingerprint, 'text': text, 'mode': 'dry_run_only', 'externalCalls': 0,
                    'approvalStatus': 'not_integrated', 'liveEligible': False,
                    'nextAction': 'Только локальный конверт; MAX выключен', 'providerMessageId': None}
            data['outbox'][key] = item
            runtime.bump(data)
            return deepcopy(item)

    def record_synthetic_outcome(self, outbox_id, outcome, profile, *, provider_message_id=None):
        """Offline contract harness only: never a delivery/production claim."""
        profile = runtime.actor(profile, 'launch.manage')
        if profile['schoolId'] != self.school_id:
            raise HTTPException(403, 'Школа интеграции не совпадает с действующим доступом')
        if outcome not in {'accepted', 'outcome_unknown', 'failed_permanent'}:
            raise HTTPException(422, 'Неизвестный результат')
        with edit_state() as state:
            data = runtime.namespace(state)
            item = data['outbox'].get(outbox_id)
            if not item or item['schoolId'] != profile['schoolId'] or item['integrationId'] != self.integration_id:
                raise HTTPException(404, 'Конверт не найден')
            binding = data['bindings'].get(item['bindingId'])
            runtime.active_task(runtime.task_for(data, item['taskId'], profile))
            if (not binding or binding['schoolId'] != self.school_id or binding['actor']['schoolId'] != self.school_id or binding['integrationId'] != self.integration_id
                    or binding['state'] != 'verified_offline' or binding['revision'] != item['bindingRevision'] or not runtime.current(binding['actor'])):
                raise HTTPException(403, 'Адресат отозван')
            if item['state'] != 'pending':
                raise HTTPException(409, 'Исход уже записан; автоматический повтор запрещён')
            if outcome == 'accepted' and not provider_message_id:
                raise HTTPException(422, 'Синтетический API ACK должен иметь message id')
            item.update(state=outcome, revision=item['revision'] + 1, synthetic=True,
                        providerMessageId=native_id(provider_message_id) if provider_message_id else None,
                        nextAction='MAX не вызывался; это синтетический контракт' if outcome != 'outcome_unknown' else 'Не повторять автоматически; нужен разбор оператора')
            runtime.bump(data)
            return deepcopy(item)

    def record_synthetic_acknowledgement(self, outbox_id, binding_id, profile):
        profile = runtime.actor(profile, 'launch.read')
        if profile['schoolId'] != self.school_id:
            raise HTTPException(403, 'Школа интеграции не совпадает с действующим доступом')
        with edit_state() as state:
            data = runtime.namespace(state)
            item = data['outbox'].get(outbox_id)
            binding = data['bindings'].get(binding_id)
            task = data['tasks'].get(item['taskId']) if item else None
            if (not item or item['schoolId'] != profile['schoolId'] or item['integrationId'] != self.integration_id or item['bindingId'] != binding_id or
                    not task or task['schoolId'] != self.school_id
                    or not binding or binding['schoolId'] != self.school_id or binding['actor']['schoolId'] != self.school_id or binding['integrationId'] != self.integration_id
                    or binding['actor']['userId'] != profile['userId'] or not runtime.current(binding['actor'])
                    or binding['state'] != 'verified_offline' or item['state'] != 'accepted' or item.get('synthetic') is not True):
                raise HTTPException(403, 'Подтверждение адресата недоступно')
            item.update(state='acknowledged', revision=item['revision'] + 1, acknowledgement='synthetic_explicit_employee_action')
            runtime.bump(data)
            return deepcopy(item)
