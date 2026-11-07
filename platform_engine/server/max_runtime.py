"""Explicit MAX worker service. No routes, env reads, automatic enable or retries.

Ingress durably queues authenticated native events without model/provider calls.
The worker uses the existing school-request command receipts, not a new workflow.
An outbound attempt is committed before the transport call; a crash leaves an
unknown outcome which an automatic worker never sends again.
"""
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime,timezone
import hashlib
import hmac
import secrets

from fastapi import HTTPException
from . import db, launch_runtime as authority, school_requests as requests, staff_access
from .max_identity import MaxIdentity, binding_current, revoke_binding, token_hash
from .schedule_notifications import SCHEDULE_NOTICE_TEXT, ScheduleNotificationService


_IDENTITY_EVENTS = {'bot_started', 'bot_stopped'}


class MaxRuntime:
    def __init__(self, *, integration_id, school_id, db_path, webhook_secret=None,
                 transport=None, enabled=False, extraction_service=None, schedule_notices_enabled=False,
                 replacement_storage=None, replacement_domain=None, replacement_staff=None, max_store=None):
        if (replacement_storage is None) != (replacement_domain is None):
            raise ValueError('Expected paired PostgreSQL storage and domain')
        valid_secret = isinstance(webhook_secret,str) and 32 <= len(webhook_secret) <= 256 and webhook_secret.isascii()
        self.identity=MaxIdentity(integration_id=integration_id,school_id=school_id,db_path=db_path,
            enabled=enabled is True and valid_secret,replacement_domain=replacement_domain,
            replacement_staff=replacement_staff,store=max_store)
        self.store=self.identity.store
        self.school_id,self.integration_id=school_id,integration_id
        self._secret=webhook_secret if valid_secret else None
        self.transport,self.extraction_service=transport,extraction_service
        self.schedule_notices_enabled = schedule_notices_enabled is True and transport is not None
        self.replacement_domain = replacement_domain
        self.notices = ScheduleNotificationService(staff_access.own_timetable, storage=replacement_storage)

    def status(self):
        result={'enabled':self.store.enabled,'webhookCallable':True,'senderConfigured':self.transport is not None,
            'publicWebhookVerified':False,'deliveryVerified':False,'readVerified':False,
            'inbox':{},'outbox':{},'blockers':[]}
        if not self.store.enabled:
            result['blockers']=['max_disabled_or_secret_missing']; return result
        data=self.store.read()
        for kind in ('inbox','outbox'):
            for item in data[kind].values():
                state=item['state']; result[kind][state]=result[kind].get(state,0)+1
        if self.transport is None: result['blockers'].append('sender_not_configured')
        return result

    def ingest(self, body:bytes, headers:list[tuple[str,str]]):
        self.store.require()
        credentials=[v for k,v in headers if k.lower()=='x-max-bot-api-secret']
        if len(credentials)!=1 or not isinstance(credentials[0],str) or not credentials[0].isascii() or len(credentials[0])>256 or not secrets.compare_digest(credentials[0],self._secret):
            raise HTTPException(403,'Событие MAX не подтверждено')
        if not isinstance(body,bytes) or len(body)>65536:
            raise HTTPException(413,'Превышен размер события MAX')
        from .max_wire import normalize_update,MaxWireError
        try: native=normalize_update(body)
        except MaxWireError: raise HTTPException(422,'Неподдерживаемое событие MAX') from None
        event=native.as_dict()
        if self.replacement_domain is not None and event['eventType'] not in _IDENTITY_EVENTS:
            raise HTTPException(422, 'Для стенда расписания доступны только события личной связи')
        key=authority.digest([self.school_id,self.integration_id,native.event_key])
        # Persist capabilities only as hashes, including the native start payload.
        for name,target in (('startPayload','startHash'),('callbackPayload','callbackHash')):
            value=event.pop(name,None)
            if value is not None:
                try: event[target]=token_hash(value)
                except HTTPException: event[target]='invalid'
        event.pop('text',None) if event['eventType'] in {'bot_started','bot_stopped','message_callback'} else None
        now=authority.now_ms()
        if event['timestamp']>now:
            raise HTTPException(403,'Событие MAX из будущего')
        if event['eventType']=='message_callback' and event.get('callbackTimestamp')is not None:
            clicked=event['callbackTimestamp']
            if type(clicked)is not int or not 0<=clicked<=event['timestamp']:
                raise HTTPException(422,'Противоречивое время выбора вопроса MAX')
        if event['eventType']=='message_created':
            created=event.get('messageTimestamp')
            if created is None: created=event['timestamp']
            if type(created)is not int or not 0<=created<=event['timestamp']:
                raise HTTPException(422,'Противоречивое время сообщения MAX')
            try: datetime.fromtimestamp(created/1000,timezone.utc)
            except (ValueError,OverflowError,OSError): raise HTTPException(422,'Недопустимое время сообщения MAX') from None
            event['createdAt']=created
        with self.store.edit() as data:
            current=next((b for b in data['bindings'].values() if b['state']=='verified'
                and b['senderId']==event['senderId'] and b['chatId']==event['chatId']),None)
            old=data['inbox'].get(key)
            if old:
                if old['event']['payloadHash']!=event['payloadHash']:
                    raise HTTPException(409,'Нативное событие MAX повторено иначе')
                if old.get('bindingId'):
                    binding_current(data,old['bindingId'],self.school_id,generation=old['generation'])
                return {'acceptedLocally':True,'duplicate':True}
            item={'id':key,'event':event,'state':'queued','receivedAt':now,'leaseUntil':None,'claim':None,
                'bindingId':current['id'] if current else None,'generation':current['generation'] if current else None,
                'command':None,'result':None}
            data['inbox'][key]=item
        return {'acceptedLocally':True,'duplicate':False}

    def _event_binding(self,data,item):
        b=binding_current(data,item.get('bindingId'),self.school_id,generation=item.get('generation'))
        e=item['event']
        occurred=e.get('createdAt',e.get('callbackTimestamp') if e.get('callbackTimestamp')is not None else e['timestamp'])
        if (b['senderId']!=e['senderId'] or b['chatId']!=e['chatId'] or occurred<=b['verifiedAt']):
            raise HTTPException(403,'Событие относится к другой личной привязке')
        return b

    def _question(self,binding,request_id,question_id,revision):
        response=requests.get_request(request_id,binding['actor'])
        request=response['request']
        if request['revision']!=revision:
            raise HTTPException(409,'Заявка изменилась; нужен новый вопрос')
        # A revoked/revised owner cannot leak old request content through an active recipient.
        raw=db.read_state().get('_schoolRequests',{}).get('schools',{}).get(self.school_id,{}).get('requests',{}).get(request_id)
        if not raw: raise HTTPException(404,'Заявка отсутствует')
        requests._current_binding(raw['ownerBinding'],binding['actor'])
        q=next((q for q in request['questions'] if q['id']==question_id and q['state']=='open'
            and q['respondentMembershipId']==binding['actor']['membershipId']
            and q['respondentRevision']==binding['actor']['membershipRevision']),None)
        if not q: raise HTTPException(409,'Вопрос закрыт или адресат изменился')
        return response,q

    def _callback_token(self,key,generation):
        return hmac.new(self._secret.encode(),('MAX-question:'+key+':'+generation).encode(),hashlib.sha256).hexdigest()

    def _outgoing_current(self,data,item):
        binding=binding_current(data,item['bindingId'],self.school_id,generation=item['generation'])
        if item.get('replacementDomain') != self.replacement_domain or (
                self.replacement_domain is not None and not item.get('scheduleNoticeId')):
            raise HTTPException(409, 'Отправка относится к другому контуру расписания')
        if item.get('scheduleNoticeId'):
            notice = self._schedule_notice(binding['actor'], item['schedulePublicationVersion'])
            if (notice['id'] != item['scheduleNoticeId'] or notice['status'] != 'available'
                    or binding['chatId'] != item['chatId'] or item['text'] != SCHEDULE_NOTICE_TEXT):
                raise HTTPException(409, 'Адресат или версия уведомления расписания изменились')
        elif item['questionId']:
            self._question(binding,item['schoolRequestId'],item['questionId'],item['requestRevision'])
        elif item['schoolRequestId']:
            response=requests.get_request(item['schoolRequestId'],binding['actor'])
            raw=db.read_state()['_schoolRequests']['schools'][self.school_id]['requests'][item['schoolRequestId']]
            requests._current_binding(raw['ownerBinding'],binding['actor'])
            if response['request']['revision']!=item['requestRevision']: raise HTTPException(409,'Ответ устарел')
        return binding

    def _enqueue(self,data,binding,text,business_key,*,request_id=None,question=None,revision=None,schedule_notice=None):
        if question:
            existing=next((o for o in data['outbox'].values() if o['bindingId']==binding['id']
                and o['generation']==binding['generation'] and o['schoolRequestId']==request_id
                and o['questionId']==question['id'] and o['requestRevision']==revision),None)
            if existing: return existing
        key=authority.digest([self.school_id,self.integration_id,business_key,binding['id'],binding['generation']])
        text=text.strip()[:800]
        if not text: raise HTTPException(422,'Пустой ответ MAX')
        item={'id':key,'bindingId':binding['id'],'generation':binding['generation'],'chatId':binding['chatId'],
            'state':'pending','text':text,'schoolRequestId':request_id,'questionId':question['id'] if question else None,
            'requestRevision':revision,'providerMessageId':None,'attemptId':None,'createdAt':authority.now_ms()}
        if schedule_notice is not None:
            item.update(scheduleNoticeId=schedule_notice['id'], schedulePublicationVersion=schedule_notice['publicationVersion'])
            if self.replacement_domain is not None:
                item['replacementDomain'] = self.replacement_domain
        if question:
            token=self._callback_token(key,binding['generation'])
            digest=token_hash(token)
            item['callbackHash']=digest
            data['callbacks'].setdefault(digest,{'state':'open','bindingId':binding['id'],'generation':binding['generation'],
                'schoolRequestId':request_id,'questionId':question['id'],'requestRevision':revision})
        old=data['outbox'].get(key)
        if old:
            if any(old.get(k)!=item.get(k) for k in ('text','schoolRequestId','questionId','requestRevision','generation',
                                                   'scheduleNoticeId','schedulePublicationVersion','replacementDomain')):
                raise HTTPException(409,'Конверт MAX изменился')
            return old
        data['outbox'][key]=item
        return item

    def _schedule_notice(self, recipient, expected_version):
        if not self.schedule_notices_enabled:
            raise HTTPException(503, 'Передача уведомлений расписания в MAX выключена')
        view = self.notices.own(recipient)
        if view['publicationVersion'] != expected_version:
            raise HTTPException(409, 'Опубликованная версия расписания изменилась')
        if len(view['items']) != 1:
            raise HTTPException(404, 'У учителя нет адресного уведомления для этой публикации')
        notice = view['items'][0]
        active = self.notices.active_publication(self.school_id)
        if not active or active['publicationId'] != notice['publicationId']:
            raise HTTPException(409, 'Опубликованная версия расписания изменилась')
        return notice

    def _schedule_director(self, profile):
        actor = authority.actor(profile, 'launch.manage')
        if actor['schoolId'] != self.school_id or actor.get('staffRole') != 'director':
            raise HTTPException(403, 'Передачу уведомления в MAX подтверждает директор школы')
        return actor

    def schedule_notice_targets(self, profile):
        actor = self._schedule_director(profile)
        if not self.schedule_notices_enabled:
            return {'enabled': False, 'publicationVersion': None, 'recipients': []}
        self.store.require()
        active = self.notices.active_publication(self.school_id)
        recipients = []
        if active and self.store.read()['bindings']:
            current, rows = self.notices.current_teacher_notices(actor)
            if current != active:
                raise HTTPException(409, 'Опубликованная версия расписания изменилась')
            data = self.store.read()
            checked_publication, checked_rows = self.notices.current_teacher_notices(actor)
            if checked_publication != current or checked_rows != rows:
                raise HTTPException(409, 'Публикация или получатели изменились во время проверки')
            outbox = {item['scheduleNoticeId']: item['state'] for item in data['outbox'].values()
                      if item.get('scheduleNoticeId') and item.get('replacementDomain') == self.replacement_domain}
            verified = {item['actor']['membershipId']: item['id'] for item in data['bindings'].values()
                        if item['state'] == 'verified'}
            for row in rows:
                state = outbox.get(row['id'])
                ident = verified.get(row['membershipId'])
                if ident is None or (state is None and row['status'] != 'available'):
                    continue
                try:
                    binding_current(data, ident, self.school_id)
                except HTTPException:
                    continue
                recipients.append({'membershipId': row['membershipId'], 'name': row['name'],
                                   'queueState': state or 'ready'})
        if self.notices.active_publication(self.school_id) != active:
            raise HTTPException(409, 'Опубликованная версия расписания изменилась')
        self._schedule_director(actor)
        return {'enabled': True, 'publicationVersion': active['version'] if active else None,
                'recipients': sorted(recipients, key=lambda row: (row['name'].casefold(), row['membershipId']))}

    def queue_schedule_notice(self, profile, membership_id, expected_version, *, check_session):
        actor = self._schedule_director(profile)
        self.store.require()
        recipient = staff_access.resolve_membership(self.school_id, membership_id)
        if not recipient or recipient.get('staffRole') != 'teacher':
            raise HTTPException(404, 'Действующий учитель школы не найден')
        self._schedule_notice(recipient, expected_version)
        with self.store.edit() as data:
            authority.actor(actor, 'launch.manage')
            notice = self._schedule_notice(recipient, expected_version)
            item = next((row for row in data['outbox'].values() if row.get('scheduleNoticeId') == notice['id']
                         and row.get('replacementDomain') == self.replacement_domain), None)
            if item is None:
                binding = next((row for row in data['bindings'].values() if row['state'] == 'verified'
                                and row['actor']['membershipId'] == membership_id), None)
                if binding is None:
                    raise HTTPException(409, 'У учителя нет подтверждённой личной привязки MAX')
                binding = binding_current(data, binding['id'], self.school_id)
                bound_notice = self._schedule_notice(binding['actor'], expected_version)
                if bound_notice['id'] != notice['id']:
                    raise HTTPException(409, 'Личная привязка MAX относится к другой записи')
                if bound_notice['status'] != 'available':
                    raise HTTPException(409, 'Учитель уже подтвердил уведомление в кабинете')
                authority.actor(actor, 'launch.manage')
                check_session()
                item = self._enqueue(data, binding, SCHEDULE_NOTICE_TEXT, 'schedule_notice_' + notice['id'],
                                     schedule_notice=bound_notice)
            else:
                check_session()
        return {'publicationVersion': expected_version, 'queueState': item['state'],
                'deliveryVerified': False, 'readVerified': False}

    def _plan_command(self,data,item,binding):
        e=item['event']
        if e['eventType']=='message_callback':
            callback=data['callbacks'].get(e.get('callbackHash'))
            if not callback or callback['state']!='open' or callback['bindingId']!=binding['id'] or callback['generation']!=binding['generation']:
                raise HTTPException(403,'Кнопка MAX не относится к текущему вопросу')
            self._question(binding,callback['schoolRequestId'],callback['questionId'],callback['requestRevision'])
            clicked=e['callbackTimestamp'] if e.get('callbackTimestamp')is not None else e['timestamp']
            last=binding.get('lastSelectionAt',binding.get('selection',{}).get('selectedAt'))
            if last is not None and clicked<=last:
                raise HTTPException(409,'Запоздалый выбор не меняет актуальный вопрос')
            binding['lastSelectionAt']=clicked
            binding['selection']={**deepcopy(callback),'selectedAt':clicked}
            self._enqueue(data,binding,'Напишите ответ на выбранный вопрос следующим сообщением.',item['id']+'_select')
            item['state']='completed'; return None
        if e['eventType'] in {'bot_stopped'}:
            revoke_binding(data,binding); item['state']='completed'; return None
        selection=binding.get('selection')
        if selection:
            self._question(binding,selection['schoolRequestId'],selection['questionId'],selection['requestRevision'])
            if type(selection.get('selectedAt'))is not int:
                binding.pop('selection',None)
                self._enqueue(data,binding,'Выберите кнопку актуального вопроса перед новым ответом.',item['id']+'_selection_time_required')
                item['state']='completed';return None
            if e['createdAt']<=selection['selectedAt']:
                self._enqueue(data,binding,'Это сообщение отправлено до выбора вопроса. Напишите ответ новым сообщением после выбора кнопки.',item['id']+'_old_text')
                item['state']='completed';return None
            return {'operation':'reply','requestId':selection['schoolRequestId'],'questionId':selection['questionId'],
                'body':{'requestId':'max_'+item['id'],'expectedRevision':selection['requestRevision'],'text':e['text']}}
        # Do not guess which open question a free-text message answers.
        owned=requests.list_requests(binding['actor'])['requests']
        if any(q['state']=='open' and q['respondentMembershipId']==binding['actor']['membershipId']
                for r in owned for q in r['questions']):
            self._enqueue(data,binding,'Выберите кнопку конкретного вопроса перед ответом. Новая заявка не создана.',item['id']+'_choose')
            item['state']='completed'; return None
        return {'operation':'create','requestId':None,'questionId':None,'receivedAt':e['createdAt'],
            'body':{'requestId':'max_'+item['id'],'text':e['text']}}

    def process_one(self):
        self.store.require(); now=authority.now_ms(); claim=secrets.token_urlsafe(18)
        with self.store.edit() as data:
            item=next((v for v in data['inbox'].values() if v['state']=='queued'
                or v['state']=='processing' and v['leaseUntil']<=now),None)
            if item is None: return {'processed':False}
            item.update(state='processing',leaseUntil=now+60000,claim=claim)
            key=item['id']
        try:
            with self.store.edit() as data:
                item=data['inbox'][key]
                if item['claim']!=claim: raise HTTPException(409,'Задание MAX уже занято')
                if self.replacement_domain is not None and item['event']['eventType'] not in _IDENTITY_EVENTS:
                    raise HTTPException(422, 'Для стенда расписания доступны только события личной связи')
                if item['event']['eventType']=='bot_started':
                    binding=self.identity._consume(data,item['event'])
                    if self.replacement_domain is None:
                        self._enqueue(data,binding,'Личная связь подтверждена. Теперь можно отправить обращение в этом диалоге.',key+'_bound')
                    item['state']='completed'
                    return {'processed':True,'state':'completed'}
                binding=self._event_binding(data,item)
                if item['command'] is None: item['command']=self._plan_command(data,item,binding)
                if item['state']=='completed': return {'processed':True,'state':'completed'}
                command=deepcopy(item['command']); actor=deepcopy(binding['actor'])
            # Optional model preparation is outside BOTH sidecar and domain transactions.
            if self.extraction_service is not None:
                from .request_extraction import bound_service
                context=bound_service(self.extraction_service)
            else: context=nullcontext()
            with context:
                anchor=datetime.fromtimestamp(command['receivedAt']/1000,timezone.utc) if command['operation']=='create' else None
                receipt=requests.prepare_extraction(command['operation'],command['body'],actor,
                    request_id=command['requestId'],question_id=command['questionId'],received_at=anchor)
                with self.store.edit() as data:
                    item=data['inbox'][key]
                    if item['claim']!=claim: raise HTTPException(409,'Задание MAX уже занято')
                    binding=self._event_binding(data,item)
                    with db.edit_state() as domain:
                        # Current identity/CAS and receipt matching are checked again by domain helpers.
                        kwargs={'extraction_receipt':receipt}
                        if command['operation']=='create':
                            result=requests.create_request_in_state(domain,command['body'],binding['actor'],received_at=anchor,**kwargs)
                        else:
                            result=requests.reply_request_in_state(domain,command['requestId'],command['body'],binding['actor'],
                                question_id=command['questionId'],**kwargs)
                    # If the process dies after the domain commit, the deterministic
                    # school command receipt recovers it without a second domain write.
                    request=result['request']
                    open_q=[q for q in request['questions'] if q['state']=='open'
                        and q['respondentMembershipId']==binding['actor']['membershipId']]
                    question=open_q[0] if len(open_q)==1 else None
                    self._enqueue(data,binding,result['serverReply'],key+'_reply',request_id=request['id'],
                        question=question,revision=request['revision'])
                    if command['operation']=='reply': binding.pop('selection',None)
                    item.update(state='completed',result={'schoolRequestId':request['id'],'revision':request['revision']})
            return {'processed':True,'state':'completed'}
        except Exception as exc:
            with self.store.edit() as data:
                item=data['inbox'][key]
                if item['claim']==claim:
                    item.update(state='failed',failureCode=getattr(exc,'status_code',500))
                    if (self.replacement_domain is None and getattr(exc,'status_code',None) in {409,422}
                            and item['event']['eventType'] in {'message_created','message_callback'}):
                        try:
                            binding=self._event_binding(data,item)
                            feedback='Не удалось разобрать дату или данные обращения. Укажите корректную дату ГГГГ-ММ-ДД и номер урока новым сообщением.'
                            if exc.status_code==409:
                                feedback='Вопрос или обращение изменились. Выберите кнопку актуального вопроса и отправьте ответ новым сообщением.'
                                selection=binding.get('selection')
                                if selection:
                                    try:self._question(binding,selection['schoolRequestId'],selection['questionId'],selection['requestRevision'])
                                    except HTTPException as stale:
                                        if stale.status_code==409:binding.pop('selection',None)
                                        else:raise
                            self._enqueue(data,binding,feedback,key+'_validation_feedback')
                        except HTTPException:
                            pass # Unverified/revoked/mismatched actors receive no school content.
            return {'processed':True,'state':'failed'}

    def queue_questions(self,limit=100):
        self.store.require()
        if self.replacement_domain is not None:
            return {'queued': 0}
        if type(limit)is not int or not 1<=limit<=500: raise HTTPException(422,'Недопустимый размер очереди')
        domain=db.read_state().get('_schoolRequests',{}).get('schools',{}).get(self.school_id,{}).get('requests',{})
        queued=0
        with self.store.edit() as data:
            # LIMIT bounds inspection as well as writes, not just successful rows.
            records=sorted(domain.values(),key=lambda r:r['id'])
            cursor=data.get('questionCursor',0)%max(len(records),1)
            batch=(records[cursor:]+records[:cursor])[:limit]
            data['questionCursor']=(cursor+len(batch))%max(len(records),1)
            for request in batch:
                for question in request['questions'][:200]:
                    if question['state']!='open': continue
                    b=next((b for b in data['bindings'].values() if b['state']=='verified'
                        and b['actor']['membershipId']==question['respondentMembershipId']),None)
                    if not b: continue
                    try:
                        binding_current(data,b['id'],self.school_id)
                        self._question(b,request['id'],question['id'],request['revision'])
                    except HTTPException: continue
                    before=len(data['outbox'])
                    self._enqueue(data,b,question['text'],'question_'+question['id']+'_'+str(request['revision']),
                        request_id=request['id'],question=question,revision=request['revision'])
                    queued+=len(data['outbox'])-before
        return {'queued':queued}

    def send_one(self):
        self.store.require()
        if self.transport is None: return {'processed':False,'state':'blocked_sender_missing'}
        with self.store.edit() as data:
            item=next((v for v in data['outbox'].values() if v['state']=='pending'),None)
            if item is None: return {'processed':False}
            try:
                self._outgoing_current(data,item)
            except HTTPException as error:
                if self.replacement_domain is not None and error.status_code == 503:
                    return {'processed':False,'state':'domain_unavailable'}
                item['state']='cancelled'; return {'processed':True,'state':'cancelled'}
            item.update(state='attempt_started',attemptId=secrets.token_urlsafe(18),attemptedAt=authority.now_ms())
            outgoing=deepcopy(item)
        # Durable attempt precedes any provider call. No transaction held here.
        transport_started = False
        try:
            # Immediate recheck closes queued revocation/generation changes before I/O.
            data=self.store.read(); self._outgoing_current(data,outgoing)
            if outgoing.get('scheduleNoticeId'):
                # Checking the school notice can overlap with a native MAX unlink.
                latest = self.store.read()['bindings'].get(outgoing['bindingId'])
                if (not latest or latest['state'] != 'verified' or latest['generation'] != outgoing['generation']
                        or latest['chatId'] != outgoing['chatId']):
                    raise HTTPException(403, 'Личная привязка MAX изменилась во время проверки')
            callback=self._callback_token(outgoing['id'],outgoing['generation']) if outgoing.get('callbackHash') else None
            transport_started = True
            outcome=self.transport.send_message(outgoing['chatId'],outgoing['text'],callback_payload=callback,request_key=outgoing['id'])
            status=outcome.status if hasattr(outcome,'status') else outcome.get('status')
            final={'accepted':'accepted','rejected':'failed','unknown':'outcome_unknown'}.get(status,'outcome_unknown')
            mid=outcome.provider_message_id if hasattr(outcome,'provider_message_id') else outcome.get('provider_message_id')
        except HTTPException as error:
            final = 'cancelled'
            if self.replacement_domain is not None:
                if transport_started:
                    final = 'outcome_unknown'
                elif error.status_code == 503:
                    final = 'pending'
            mid = None
        except Exception:
            final,mid='outcome_unknown',None
        with self.store.edit() as data:
            item=data['outbox'][outgoing['id']]
            if item['attemptId']!=outgoing['attemptId']: raise HTTPException(409,'Попытка MAX изменилась')
            if final == 'pending':
                try:
                    binding_current(data, outgoing['bindingId'], self.school_id, generation=outgoing['generation'])
                except HTTPException:
                    final = 'cancelled'
            item.update(state=final,providerMessageId=mid if isinstance(mid,str) else None)
            if final == 'pending':
                item.update(attemptId=None,attemptedAt=None)
        return {'processed':final != 'pending','state':final if final != 'pending' else 'domain_unavailable'}

    def recover(self):
        """Explicit worker recovery: never resend started/unknown send attempts."""
        self.store.require(); now=authority.now_ms(); recovered=0; unknown=0
        with self.store.edit() as data:
            for item in data['inbox'].values():
                if item['state']=='processing' and item['leaseUntil']<=now:
                    item.update(state='queued',claim=None,leaseUntil=None); recovered+=1
            for item in data['outbox'].values():
                if item['state']=='attempt_started': item['state']='outcome_unknown'; unknown+=1
        return {'requeued':recovered,'unknownAttempts':unknown}
