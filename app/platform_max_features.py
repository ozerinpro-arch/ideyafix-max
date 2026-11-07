"""MAX channel controls over the independent native authority and inbox.

This module never sends messages and never receives provider identifiers or
credentials. The outer MAX store owns confirmed bindings and delivery receipts.
"""
from __future__ import annotations

import hashlib
import re

from .platform_chat import ChatState


SCHEDULE_NOTICE_TEXT = 'Опубликована новая версия вашего расписания. Проверьте занятия в личном расписании.'
NOTICE_ID = re.compile(r'notice_[a-f0-9]{32}')
MANAGERS = {'director', 'platform_admin'}
DELIVERY_STATES = {'queued':'В очереди', 'started':'Отправка начата', 'accepted':'Принято MAX',
                   'unknown':'Результат неизвестен', 'rejected':'Отклонено'}

# The six source UI operations are deliberately implemented by the current
# Long Polling channel. The old integration runtime/token is never activated.
INVENTORY = [
    {'sourceEndpoint':'GET /api/school/integrations/status', 'command':'/max status',
     'implementation':'current channel safe counters; no old integration resources'},
    {'sourceEndpoint':'GET /api/school/integrations/max/me', 'command':'/max me',
     'implementation':'current confirmed MAX binding supplied by the outer store'},
    {'sourceEndpoint':'POST /api/school/integrations/max/link', 'command':'/max link',
     'implementation':'native MAX invite acceptance; /people creates a one-time invitation'},
    {'sourceEndpoint':'POST /api/school/integrations/max/revoke', 'command':'/max revoke → /max confirm',
     'implementation':'private unlinkMax intent; outer store revokes its current binding'},
    {'sourceEndpoint':'GET /api/school/integrations/max/schedule-notices', 'command':'/max notices',
     'implementation':'current native publication and inbox; confirmed bound teachers only'},
    {'sourceEndpoint':'POST /api/school/integrations/max/schedule-notices/queue',
     'command':'/max choose <profile> → /max confirm',
     'implementation':'private scheduleNoticeIntent; outer durable outbox rechecks before send'},
]


def _button(label, command):
    return {'label':str(label)[:100], 'command':command}


def _reply(text, buttons=()):
    return {'text':str(text)[:3900], 'buttons':list(buttons)[:500]}


class MaxFeatures:
    def __init__(self, runtime):
        self.runtime = runtime
        self.state = ChatState(runtime.root / 'max-features.sqlite3')

    def _profiles(self):
        return {row['id']:row for row in self.runtime.profiles()}

    def _bound(self, supplied, profiles):
        """A caller-confirmed binding must still describe the current profile."""
        result = {}
        if not isinstance(supplied, list):
            return result
        for row in supplied:
            if not isinstance(row, dict) or type(row.get('id')) is not int:
                continue
            current=profiles.get(row['id'])
            if current and row.get('role')==current['role'] and row.get('name')==current['name']:
                result[row['id']]=current
        return result

    def _get(self, actor, path):
        return self.runtime.request(actor,'GET',path)

    def _denied(self):
        return _reply('Личный доступ изменился или отозван. Получите новый доступ у администратора.')

    def _changed(self):
        return _reply('Привязка, публикация или уведомление изменились. Обновите список перед подтверждением.',
                      [_button('Обновить уведомления','/max notices')])

    def _manager_status(self, actor, profile):
        if profile.get('role') not in MANAGERS:
            return None
        response=self._get(actor,'/api/school/schedule-notifications')
        return response.get('data') if response.get('status')==200 else None

    def validate_notice(self, targetactor, intent):
        """Uncached native guard, also used immediately before outer delivery."""
        if (type(targetactor) is not int or not isinstance(intent,dict)
                or set(intent)!={'targetActorId','publicationVersion','noticeId'}
                or type(intent.get('targetActorId')) is not int
                or intent['targetActorId']!=targetactor
                or type(intent.get('publicationVersion')) is not int
                or intent['publicationVersion']<1
                or not isinstance(intent.get('noticeId'),str)
                or not NOTICE_ID.fullmatch(intent['noticeId'])):
            return False
        profile=self._profiles().get(targetactor)
        if profile is None or profile['role']!='teacher':
            return False
        if self.runtime.authorize(targetactor).get('status')!=200:
            return False
        response=self._get(targetactor,'/api/staff/schedule-notifications')
        if response.get('status')!=200 or not isinstance(response.get('data'),dict):
            return False
        data=response['data']
        if data.get('publicationVersion')!=intent['publicationVersion']:
            return False
        return any(isinstance(row,dict) and row.get('id')==intent['noticeId']
                   and row.get('publicationVersion')==intent['publicationVersion']
                   and row.get('status')=='available' and row.get('text')==SCHEDULE_NOTICE_TEXT
                   for row in data.get('items',[]))

    def _candidates(self, actor, profile, bound):
        status=self._manager_status(actor,profile)
        if not status:
            return None, []
        version=status.get('publicationVersion')
        candidates=[]
        if type(version) is int and version>0:
            for target in bound.values():
                if target['role']!='teacher':
                    continue
                response=self._get(target['id'],'/api/staff/schedule-notifications')
                data=response.get('data') or {}
                if response.get('status')!=200 or data.get('publicationVersion')!=version:
                    continue
                for row in data.get('items',[]):
                    intent={'targetActorId':target['id'],'publicationVersion':version,'noticeId':row.get('id')}
                    if self.validate_notice(target['id'],intent):
                        candidates.append({'profile':target,'intent':intent})
        return status,candidates

    def _notices(self, actor, profile, bound, channel_status):
        status,candidates=self._candidates(actor,profile,bound)
        if status is None:
            return _reply('Передачу уведомления подтверждает директор с действующим школьным доступом.',
                          [_button('Настройки MAX','/max')])
        if not status.get('publicationVersion'):
            return _reply('Активной публикации расписания пока нет.',[_button('Настройки MAX','/max')])
        states=channel_status.get('noticeStates',{}) if isinstance(channel_status,dict) else {}
        if not isinstance(states,dict):
            states={}
        buttons=[];totals={}
        for item in candidates:
            state=states.get(item['intent']['noticeId'])
            label=DELIVERY_STATES.get(state,'Не поставлено в очередь') if isinstance(state,str) else 'Не поставлено в очередь'
            totals[label]=totals.get(label,0)+1
            buttons.append(_button(label+' · '+item['profile']['name'],'/max choose '+str(item['profile']['id'])))
        # The outer store paginates native buttons. Preserve every recipient
        # within the confirmed-profile limit, including the 500th teacher.
        if len(buttons)<=498:
            buttons += [_button('Подготовить уведомления','/max prepare'),_button('Настройки MAX','/max')]
        summary='\n'.join(label+': '+str(count) for label,count in totals.items())
        return {**_reply('Уведомления · публикация '+str(status['publicationVersion'])+'\n'
                      'Доступных получателей с подтверждённой привязкой: '+str(len(candidates))+'.\n'
                      +(summary+'\n' if summary else '')+
                      'Получатель получит только предложение проверить личное расписание. Выберите преподавателя.',buttons),
                '_noticeList':{'version':status['publicationVersion'],'intents':[row['intent'] for row in candidates]}}

    def handle(self, actor_id, text, event_id, *, bound_profiles, channel_status, session_key=None):
        if not isinstance(text,str) or not re.match(r'^/max(?:\s|$)',text.strip(),re.I):
            return None
        if self.runtime.authorize(actor_id).get('status')!=200:
            return self._denied()
        profiles=self._profiles();profile=profiles.get(actor_id)
        if profile is None:
            return self._denied()
        bound=self._bound(bound_profiles,profiles)
        words=text.strip().split();action=words[1].casefold() if len(words)>1 else 'status'

        def route(state):
            if action in {'status','me','link'}:
                state.clear()
                linked=actor_id in bound
                status='Личная привязка MAX подтверждена.' if linked else 'Подтверждённой личной привязки MAX нет.'
                lines=['Идеяфикс · канал MAX',profile['name'],status,'Режим: исходящий Long Polling.']
                labels={'bound':'Привязанных профилей','queued':'Ожидает отправки','accepted':'Принято MAX',
                        'unknown':'Отправок с неизвестным результатом','cancelled':'Отменено до отправки'}
                if isinstance(channel_status,dict):
                    for key,label in labels.items():
                        count=channel_status.get(key)
                        if type(count) is int and 0<=count<2**31:
                            lines.append(label+': '+str(count))
                if action=='link':
                    lines.append('Личная привязка подтверждается при приёме одноразового приглашения в этом боте.')
                buttons=[_button('Мой кабинет','/home'),_button('Отвязать MAX','/max revoke')]
                if profile['role'] in MANAGERS:
                    buttons=[_button('Уведомления расписания','/max notices'),_button('Пригласить пользователя','/people')]+buttons
                return _reply('\n'.join(lines),buttons)
            if action=='cancel':
                state.clear()
                return _reply('Действие отменено.',[_button('Настройки MAX','/max')])
            if action=='revoke':
                state.clear();state['pending']={'kind':'unlink'}
                return _reply('Отвязать этот личный профиль от MAX? Доступ через текущую привязку прекратится.',
                              [_button('Отвязать','/max confirm'),_button('Отмена','/max cancel')])
            if action in {'notices','prepare','choose'}:
                state.clear()
                if profile['role'] not in MANAGERS:
                    return _reply('Передачу уведомлений в MAX подтверждает директор.',[_button('Настройки MAX','/max')])
                if action=='notices':
                    return self._notices(actor_id,profile,bound,channel_status)
                status=self._manager_status(actor_id,profile)
                if not status or type(status.get('publicationVersion')) is not int:
                    return self._changed()
                if action=='prepare':
                    operation='max-prepare-'+hashlib.sha256((str(actor_id)+':'+event_id).encode()).hexdigest()
                    state['pending']={'kind':'prepare','revision':status['revision'],
                                      'version':status['publicationVersion'],'operationEvent':operation}
                    return _reply('Подготовить личные уведомления для публикации '+str(status['publicationVersion'])+'? '
                                  'Это подготовка записей кабинета; отправку в MAX нужно подтвердить отдельно.',
                                  [_button('Подготовить','/max confirm'),_button('Отмена','/max cancel')])
                if len(words)!=3 or not words[2].isdigit():
                    return self._changed()
                target=int(words[2]);_,candidates=self._candidates(actor_id,profile,bound)
                selected=next((row for row in candidates if row['profile']['id']==target),None)
                if selected is None:
                    return self._changed()
                state['pending']={'kind':'notice','intent':selected['intent']}
                return {**_reply('Передать уведомление преподавателю «'+selected['profile']['name']+'»?\n'
                              'Публикация '+str(selected['intent']['publicationVersion'])+'.\n'+SCHEDULE_NOTICE_TEXT,
                              [_button('Передать','/max confirm'),_button('Отмена','/max cancel')]),
                        '_noticePreview':selected['intent']}
            if action=='confirm':
                pending=state.get('pending') or {}
                if pending.get('kind')=='unlink':
                    state.clear()
                    return {**_reply('Отвязка MAX подтверждена. Для нового подключения понадобится приглашение.'),
                            'unlinkMax':True}
                if profile['role'] not in MANAGERS:
                    state.clear();return self._changed()
                status=self._manager_status(actor_id,profile)
                if not status:
                    state.clear();return self._changed()
                if pending.get('kind')=='prepare':
                    if pending.get('version')!=status.get('publicationVersion'):
                        state.clear();return self._changed()
                    result=self.runtime.request(actor_id,'POST','/api/school/schedule-notifications/prepare',
                        {'expectedRevision':pending['revision'],'expectedPublicationVersion':pending['version']},pending['operationEvent'])
                    state.clear()
                    if result.get('status')!=200:
                        return self._changed()
                    return self._notices(actor_id,profile,bound,channel_status)
                if pending.get('kind')=='notice':
                    intent=pending.get('intent') or {};target=bound.get(intent.get('targetActorId'))
                    state.clear()
                    if (not target or target['role']!='teacher'
                            or status.get('publicationVersion')!=intent.get('publicationVersion')
                            or not self.validate_notice(target['id'],intent)):
                        return self._changed()
                    return {**_reply('Передача уведомления подтверждена. Статус доставки учитывается отдельно.',
                                    [_button('Уведомления','/max notices')]),'scheduleNoticeIntent':intent}
                return _reply('Нет действия для подтверждения.',[_button('Настройки MAX','/max')])
            return _reply('Выберите действие MAX.',[_button('Настройки MAX','/max')])

        reply=self.state.run('max-features',str(session_key or 'test')+':'+str(actor_id),event_id,text,route)
        # Replayed intents never retain authority over a changed binding/inbox.
        listing=reply.pop('_noticeList',None)
        if listing:
            status=self._manager_status(actor_id,profile)
            if (not status or status.get('publicationVersion')!=listing['version']
                    or any(intent['targetActorId'] not in bound
                           or not self.validate_notice(intent['targetActorId'],intent) for intent in listing['intents'])):
                return self._changed()
            # Delivery receipts are a live overview, not a cached claim that a
            # past queue/unknown/accepted state is still current.
            reply=self._notices(actor_id,profile,bound,channel_status)
            reply.pop('_noticeList',None)
        intent=reply.pop('_noticePreview',None) or reply.get('scheduleNoticeIntent')
        if intent:
            target=bound.get(intent.get('targetActorId'))
            if (profile['role'] not in MANAGERS or not self._manager_status(actor_id,profile)
                    or not target or target['role']!='teacher'
                    or not self.validate_notice(target['id'],intent)):
                return self._changed()
        return reply
