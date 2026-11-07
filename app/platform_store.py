"""Durable full-platform MAX sessions, fixed personal roles and opaque action buttons."""
import base64
import hashlib
import json
import re
import secrets
import time
from copy import deepcopy

from fastapi import HTTPException
from .live_store import LiveStore,_validate,_text,_identifier
from .platform_profiles import PROFILES,ALIASES
from .platform_chat import ChatState
from .platform_transport import document_bytes, image_bytes
from .school_demo_links import DEMO_URLS, REUSABLE_NAVIGATION, demo_role_reply
from . import domain

def stamp():return int(time.time()*1000)
def digest(text):return hashlib.sha256(text.encode()).hexdigest()

class PlatformStore(LiveStore):
    def __init__(self,path,assistant=None,*,bot_username='ideya_fiks_150_bot'):
        super().__init__(path)
        self.assistant=assistant
        self.bot_username=bot_username
        self.claims=ChatState(self.path.with_name('platform-claims.sqlite3'))
        with self.connection()as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS platform_session_permissions(sender_id TEXT PRIMARY KEY,operator INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS platform_invite_permissions(hash TEXT PRIMARY KEY,operator INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS platform_actions(hash TEXT PRIMARY KEY,sender_id TEXT NOT NULL,chat_id TEXT NOT NULL,
                actor_id INTEGER NOT NULL,command TEXT NOT NULL,expires_at INTEGER NOT NULL,used INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS platform_pages(id TEXT PRIMARY KEY,sender_id TEXT NOT NULL,chat_id TEXT NOT NULL,
                actor_id INTEGER NOT NULL,payload TEXT NOT NULL,expires_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS platform_reply_payloads(outbox_id TEXT PRIMARY KEY,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS platform_reply_owners(outbox_id TEXT PRIMARY KEY,actor_id INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS platform_profile_catalog(actor_id INTEGER PRIMARY KEY,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS platform_native_invites(hash TEXT PRIMARY KEY,capability TEXT NOT NULL,
                issuer INTEGER NOT NULL,expires_at INTEGER NOT NULL,used INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS platform_notice_queue(notice_id TEXT PRIMARY KEY,outbox_id TEXT NOT NULL UNIQUE,
                target_actor INTEGER NOT NULL,publication_version INTEGER NOT NULL);
            ''')
            if db.execute("SELECT 1 FROM live_meta WHERE key='platform_permissions_initialized'").fetchone()is None:
                # Previously admitted demo testers already had all test roles.
                db.execute('INSERT OR IGNORE INTO platform_session_permissions SELECT sender_id,1 FROM live_sessions')
                db.execute("INSERT INTO live_meta VALUES('platform_permissions_initialized','true')")
            for profile in PROFILES:self._save_profile(db,profile)
            if self.assistant is not None and hasattr(self.assistant,'profiles'):
                for profile in self.assistant.profiles():self._save_profile(db,profile)

    def _save_profile(self,db,profile):
        role=profile.get('platformRole',profile.get('role'))
        labels={'teacher':'Преподаватель','student':'Ученик','director':'Директор','planner':'Завуч',
                'platform_admin':'Администратор платформы','access_admin':'Администратор доступа'}
        actor=profile.get('id');name=profile.get('name')
        if type(actor)is not int or actor<710001 or role not in labels or not _text(name,200):
            raise ValueError('invalid_native_profile')
        safe=dict(id=actor,name=name,platformRole=role,role={'teacher':'mentor','student':'learner','director':'organizer'}.get(role,role),label=labels[role])
        db.execute('INSERT INTO platform_profile_catalog VALUES(?,?) ON CONFLICT(actor_id) DO UPDATE SET payload=excluded.payload',
                   (actor,json.dumps(safe,ensure_ascii=False)))
        return safe

    def _profile(self,db,actor):
        row=db.execute('SELECT payload FROM platform_profile_catalog WHERE actor_id=?',(actor,)).fetchone()
        return json.loads(row['payload'])if row else None

    def profiles(self):
        with self.connection()as db:
            return [json.loads(row['payload'])for row in db.execute('SELECT payload FROM platform_profile_catalog ORDER BY actor_id')]

    def _hackathon(self,db,actor_id,text):
        profile=self._profile(db,actor_id)
        role='organizer'if profile['platformRole']in {'director','platform_admin'}else'participant'
        data=json.loads(db.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])
        answer=domain.command(data,actor_id,text,actor_override={'id':actor_id,'name':profile['name'],'role':role})
        db.execute('UPDATE state SET payload=? WHERE id=1',(json.dumps(data,ensure_ascii=False),))
        buttons=[{'label':'Главная','command':'/home'}]
        if role=='organizer':buttons.insert(0,{'label':'Заявки команд','command':'/applications'})
        elif domain.my_team(data,actor_id):buttons.insert(0,{'label':'Отправить заявку','command':'/apply'})
        return {'text':answer,'buttons':buttons}

    def _bound_profiles(self,db):
        result=[]
        for row in db.execute('SELECT DISTINCT actor_id FROM live_sessions'):
            p=self._profile(db,row['actor_id'])
            if p is not None:result.append({'id':p['id'],'role':p['platformRole'],'name':p['name']})
        return result

    def _channel_status(self,db):
        counts={row['state']:row['n']for row in db.execute('SELECT state,COUNT(*) n FROM live_outbox GROUP BY state')}
        states={row['notice_id']:row['state']for row in db.execute('SELECT n.notice_id,o.state FROM platform_notice_queue n JOIN live_outbox o ON o.id=n.outbox_id')}
        return {'bound':len(self._bound_profiles(db)),**{k:counts.get(k,0)for k in ('queued','accepted','unknown')},'noticeStates':states}

    def _queue_notice(self,db,issuer,event,intent):
        role=self._profile(db,issuer)['platformRole']
        if role not in {'director','platform_admin'}or not isinstance(intent,dict):raise ValueError('invalid_notice_authority')
        authority=self.assistant.runtime.request(issuer,'GET','/api/school/schedule-notifications')
        if authority.get('status')!=200 or (authority.get('data')or{}).get('publicationVersion')!=intent.get('publicationVersion'):
            return 'Права или опубликованная версия изменились; отправка не поставлена в очередь.'
        target=intent.get('targetActorId')
        binding=db.execute('SELECT * FROM live_sessions WHERE actor_id=? ORDER BY sender_id LIMIT 1',(target,)).fetchone()
        if binding is None or not self.assistant.max_features.validate_notice(target,intent):
            return 'Привязка или уведомление изменились; отправка не поставлена в очередь.'
        old=db.execute('SELECT outbox_id FROM platform_notice_queue WHERE notice_id=?',(intent['noticeId'],)).fetchone()
        if old:
            state=db.execute('SELECT state FROM live_outbox WHERE id=?',(old['outbox_id'],)).fetchone()['state']
            labels={'queued':'в очереди','started':'попытка начата','accepted':'принято транспортом MAX','unknown':'результат неизвестен, автоматический повтор выключен','rejected':'отменено или отклонено'}
            return 'Уведомление уже зарегистрировано: '+labels[state]+'. Доставка и прочтение этим не подтверждаются.'
        from .platform_max_features import SCHEDULE_NOTICE_TEXT
        native=self._paged(db,binding,{'senderId':binding['sender_id'],'chatId':binding['chat_id']},
            {'text':SCHEDULE_NOTICE_TEXT,'buttons':[{'label':'Мои уведомления','command':'/notifications'}],
             'resourceGuards':[{'kind':'schedule_notice','intent':intent}]})
        notice_event=digest('ideyafix-schedule-notice:'+intent['noticeId']);outbox=digest('ideyafix-live-outbox:'+notice_event)
        db.execute('INSERT INTO live_events VALUES(?,?,?)',(notice_event,digest(json.dumps(intent,sort_keys=True)),'queued'))
        db.execute("INSERT INTO live_outbox VALUES(?,?,?,?,'queued',NULL,NULL)",(outbox,notice_event,binding['chat_id'],native['text']))
        db.execute('INSERT INTO platform_reply_payloads VALUES(?,?)',(outbox,json.dumps(native,ensure_ascii=False)))
        db.execute('INSERT INTO platform_reply_owners VALUES(?,?)',(outbox,target))
        db.execute('INSERT INTO platform_notice_queue VALUES(?,?,?,?)',(intent['noticeId'],outbox,target,intent['publicationVersion']))
        return 'Уведомление поставлено в собственную очередь MAX. Доставка и прочтение пока не подтверждены.'

    def _new_invite(self,db,actor_id,operator=False):
        if type(actor_id)is not int or self._profile(db,actor_id)is None:raise HTTPException(422,'Неизвестный профиль')
        if not operator and db.execute('''SELECT 1 FROM live_sessions s LEFT JOIN platform_session_permissions p
                ON p.sender_id=s.sender_id WHERE s.actor_id=? AND COALESCE(p.operator,0)=0''',(actor_id,)).fetchone():
            raise HTTPException(409,'Этот личный профиль уже подключён к MAX')
        token=secrets.token_urlsafe(32);expires=stamp()+15*60*1000;hashed=digest(token)
        db.execute('INSERT INTO live_invites VALUES(?,?,?,0)',(hashed,actor_id,expires))
        db.execute('INSERT INTO platform_invite_permissions VALUES(?,?)',(hashed,int(operator)))
        return {'payload':token,'expiresAt':expires}

    def issue_invite(self,actor_id,*,operator=False):
        with self.connection()as db:
            db.execute('BEGIN IMMEDIATE')
            return self._new_invite(db,actor_id,operator)

    def _accept(self,db,event):
        token=event.get('startPayload')
        if not isinstance(token,str)or not re.fullmatch(r'[A-Za-z0-9_-]{43}',token):return None
        hashed=digest(token)
        invite=db.execute('SELECT actor_id,expires_at,used FROM live_invites WHERE hash=?',(hashed,)).fetchone()
        if invite is None:
            native=db.execute('SELECT * FROM platform_native_invites WHERE hash=?',(hashed,)).fetchone()
            if native is None or native['used']or native['expires_at']<=stamp():return None
            # A native invitation must never replace an already admitted identity.
            if db.execute('SELECT 1 FROM live_sessions WHERE sender_id=? OR chat_id=?',(event['senderId'],event['chatId'])).fetchone():return None
            if self.assistant is None:return None
            # Commit ownership in a different private journal before crossing IPC.
            # The outer MAX transaction can then roll back without releasing the
            # invitation to another sender after a successful native acceptance.
            try:
                self.claims.run('native-invite-owner',hashed,hashed,event['senderId']+':'+event['chatId'],
                                lambda state:{'text':'reserved'})
            except HTTPException:return None
            result=self.assistant.claim_native_invite(native['capability'],'max-claim:'+event['eventId'])
            if result.get('status')!=200:return None
            profile=(result.get('data')or{}).get('profile')
            if not isinstance(profile,dict):raise ValueError('invalid_native_invite_receipt')
            profile=self._save_profile(db,profile)
            db.execute('UPDATE platform_native_invites SET used=1 WHERE hash=?',(hashed,))
            db.execute('INSERT INTO live_sessions VALUES(?,?,?)',(event['senderId'],event['chatId'],profile['id']))
            db.execute('INSERT INTO platform_session_permissions VALUES(?,0)',(event['senderId'],))
            return profile['id']
        if invite['used']or invite['expires_at']<=stamp()or self._profile(db,invite['actor_id'])is None:return None
        allowed=db.execute('SELECT operator FROM platform_invite_permissions WHERE hash=?',(hashed,)).fetchone()
        operator=bool(allowed and allowed['operator'])
        if not operator and db.execute('''SELECT 1 FROM live_sessions s LEFT JOIN platform_session_permissions p
            ON s.sender_id=p.sender_id WHERE s.actor_id=? AND s.sender_id!=? AND COALESCE(p.operator,0)=0''',
            (invite['actor_id'],event['senderId'])).fetchone():return None
        owner=db.execute('SELECT sender_id FROM live_sessions WHERE chat_id=?',(event['chatId'],)).fetchone()
        if owner and owner['sender_id']!=event['senderId']:return None
        db.execute('UPDATE live_invites SET used=1 WHERE hash=?',(hashed,))
        db.execute('UPDATE platform_actions SET used=1 WHERE sender_id=? AND chat_id=?',(event['senderId'],event['chatId']))
        db.execute('INSERT INTO live_sessions VALUES(?,?,?) ON CONFLICT(sender_id) DO UPDATE SET chat_id=excluded.chat_id,actor_id=excluded.actor_id',
            (event['senderId'],event['chatId'],invite['actor_id']))
        db.execute('INSERT INTO platform_session_permissions VALUES(?,?) ON CONFLICT(sender_id) DO UPDATE SET operator=excluded.operator',
            (event['senderId'],int(operator)))
        return invite['actor_id']

    def _validate_platform(self,normalized):
        event=deepcopy(normalized);document=event.pop('file',None)
        callback=event['eventType']=='message_callback'
        if callback:
            if event.get('text')is not None or not _text(event.get('nativeId'),200)or not isinstance(event.get('callbackPayload'),str)or not re.fullmatch(r'[A-Za-z0-9_-]{43}',event['callbackPayload']):
                raise HTTPException(422,'Некорректная кнопка MAX')
            if event.get('callbackTimestamp')is not None and(type(event['callbackTimestamp'])is not int or not 0<=event['callbackTimestamp']<=event['timestamp']):
                raise HTTPException(422,'Некорректное время кнопки')
            checked={**event,'eventType':'message_created','text':'Кнопка','callbackPayload':None,'callbackTimestamp':None}
            _validate(checked)
        else:_validate(event)
        if document is not None:
            if event['eventType']!='message_created':raise HTTPException(422,'Файл не относится к сообщению')
            if document!={'error':'download_unavailable'}:document_bytes(document)
        return event,document,callback

    def _paged(self,db,session,event,reply,page=0,page_id=None):
        if not isinstance(reply,dict)or not isinstance(reply.get('text'),str):raise ValueError('invalid_platform_response')
        buttons=reply.get('buttons')or[]
        if not isinstance(buttons,list)or len(buttons)>500:raise ValueError('invalid_platform_buttons')
        if page_id is None and len(buttons)>18:
            page_id=secrets.token_hex(16)
            db.execute('INSERT INTO platform_pages VALUES(?,?,?,?,?,?)',(page_id,event['senderId'],event['chatId'],session['actor_id'],
                json.dumps(reply,ensure_ascii=False),stamp()+15*60*1000))
        selected=buttons[page*18:(page+1)*18]
        if page>0:selected.append({'label':'← Назад','command':f'/page {page_id} {page-1}'})
        if len(buttons)>(page+1)*18:selected.append({'label':'Далее →','command':f'/page {page_id} {page+1}'})
        native=[]
        for button in selected:
            if not isinstance(button,dict) or not _text(button.get('label'),128):
                raise ValueError('invalid_platform_button')
            if set(button)=={'label','url'} and isinstance(button['url'],str) and button['url'] in DEMO_URLS:
                native.append(button.copy())
                continue
            if not _text(button.get('command'),2000):
                raise ValueError('invalid_platform_button')
            token=secrets.token_urlsafe(32)
            db.execute('INSERT INTO platform_actions VALUES(?,?,?,?,?,?,0)',(digest(token),event['senderId'],event['chatId'],session['actor_id'],button['command'],stamp()+15*60*1000))
            native.append({'label':button['label'],'payload':token})
        text=reply['text'];document=reply.get('document')if page==0 else None
        if document is not None:document_bytes(document)
        if len(text)>3500:
            if document is None:
                document={'name':'Подробный ответ.txt','contentType':'text/plain; charset=utf-8','base64':base64.b64encode(text.encode()).decode()}
            text=text[:3350]+'\n\nПолные сведения сохранены в приложенном документе.'
        result={'text':text,'buttons':native,'document':document,'resourceGuards':reply.get('resourceGuards',[])}
        image=reply.get('image') if page==0 and document is None else None
        if image is not None:
            try:
                image_bytes(image)
            except ValueError:
                result['text']+='\n\nФото временно недоступно.'
            else:
                result['image']=image
        return result

    def ingest(self,normalized):
        event,document,callback=self._validate_platform(normalized)
        if event['eventType']=='bot_stopped':return super().ingest(event)
        with self.connection()as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT payload_hash FROM live_events WHERE event_id=?',(event['eventId'],)).fetchone()
            if old:
                if old['payload_hash']!=event['payloadHash']:raise HTTPException(409,'Событие повторено иначе')
                return {'duplicate':True,'queued':0}
            session=db.execute('SELECT * FROM live_sessions WHERE sender_id=? AND chat_id=?',(event['senderId'],event['chatId'])).fetchone()
            if event['eventType']=='bot_started'and event['startPayload']is not None:
                actor=self._accept(db,event)
                session=db.execute('SELECT * FROM live_sessions WHERE sender_id=? AND chat_id=?',(event['senderId'],event['chatId'])).fetchone()if actor is not None else None
            permission=db.execute('SELECT operator FROM platform_session_permissions WHERE sender_id=?',(event['senderId'],)).fetchone()
            operator=bool(permission and permission['operator'])
            incoming='/start'if event['eventType']=='bot_started'else event.get('text')
            reply=None
            if session is None:
                reply={'text':'Идеяфикс · личный кабинет в MAX. Для первого входа получите одноразовую ссылку у администратора. После входа все действия выполняются здесь.','buttons':[]}
            elif self.assistant is None:raise ValueError('platform_assistant_required')
            elif self.assistant.authorize(session['actor_id']).get('status')!=200:
                reply={'text':'Личный доступ изменился или отозван. Обратитесь к администратору.','buttons':[],'accessDenied':True}
            else:
                if callback:
                    action=db.execute('SELECT * FROM platform_actions WHERE hash=?',(digest(event['callbackPayload']),)).fetchone()
                    if (action is None or action['used']or action['expires_at']<=stamp()or action['sender_id']!=event['senderId']
                            or action['chat_id']!=event['chatId']or action['actor_id']!=session['actor_id']):
                        reply={'text':'Эта кнопка уже использована, устарела или относится к другому профилю. Откройте актуальный кабинет.','buttons':[{'label':'Личный кабинет','command':'/home'}]}
                    else:
                        incoming=action['command']
                # Rejecting a stale tap must not invalidate a newer keyboard.
                # Only exact, stateless navigation can survive a successful tap.
                if reply is None:
                    slots = ','.join('?' for _ in REUSABLE_NAVIGATION)
                    db.execute(f'UPDATE platform_actions SET used=1 WHERE sender_id=? AND chat_id=? AND command NOT IN ({slots})',
                               (event['senderId'],event['chatId'],*REUSABLE_NAVIGATION))
                if reply is None:
                    verb,_,argument=incoming.strip().partition(' ')
                    if verb.casefold() == '/demo':
                        reply = demo_role_reply(argument.casefold())
                    if verb=='/role':
                        target=ALIASES.get(argument.casefold())
                        if target is None and argument.isascii()and argument.isdigit():target=int(argument)
                        if argument and not operator:
                            reply={'text':'За вами закреплён личный профиль. Менять роль может администратор, тестовое переключение доступно только оператору.','buttons':[{'label':'Мой кабинет','command':'/home'}]}
                        elif not argument:
                            reply={'text':'Ваш профиль: '+self._profile(db,session['actor_id'])['name'],
                                'buttons':[{'label':p['name']+' · '+p['label'],'command':'/role '+str(p['id'])}for p in [json.loads(r['payload'])for r in db.execute('SELECT payload FROM platform_profile_catalog ORDER BY actor_id')]]if operator else[{'label':'Мой кабинет','command':'/home'}]}
                            reply['buttons'].append({'label':'Демо-роли','command':'/demo'})
                        elif self._profile(db,target)is None:
                            reply={'text':'Такого тестового профиля нет.','buttons':[{'label':'Профили','command':'/role'}]}
                        else:
                            db.execute('UPDATE platform_actions SET used=1 WHERE sender_id=? AND chat_id=?',(event['senderId'],event['chatId']))
                            db.execute('UPDATE live_sessions SET actor_id=? WHERE sender_id=?',(target,event['senderId']))
                            session=db.execute('SELECT * FROM live_sessions WHERE sender_id=?',(event['senderId'],)).fetchone()
                            incoming='/start'
                    if reply is None and verb=='/page':
                        pieces=argument.split()
                        if len(pieces)==2 and pieces[1].isdigit():
                            stored=db.execute('SELECT * FROM platform_pages WHERE id=?',(pieces[0],)).fetchone()
                            page=int(pieces[1])
                            if stored and stored['sender_id']==event['senderId']and stored['chat_id']==event['chatId']and stored['actor_id']==session['actor_id']and stored['expires_at']>stamp()and page<30:
                                reply=self._paged(db,session,event,json.loads(stored['payload']),page,pieces[0]);reply['_native']=True
                        if reply is None:reply={'text':'Меню устарело.','buttons':[{'label':'Личный кабинет','command':'/home'}]}
                    if reply is None and verb.casefold()in {'/hackathon','/team','/join','/idea','/apply','/applications','/approve','/return'}:
                        reply=self._hackathon(db,session['actor_id'],incoming)
                    if reply is None and verb.casefold()=='/max':
                        reply=self.assistant.handle_max(session['actor_id'],incoming,event['eventId'],
                            bound_profiles=self._bound_profiles(db),channel_status=self._channel_status(db),
                            session_key=digest(event['senderId']+':'+event['chatId']))
                        intent=reply.pop('scheduleNoticeIntent',None)
                        if intent is not None:reply['text']=self._queue_notice(db,session['actor_id'],event,intent)
                        if reply.pop('unlinkMax',False):
                            db.execute('UPDATE platform_actions SET used=1 WHERE sender_id=? AND chat_id=?',(event['senderId'],event['chatId']))
                            db.execute('DELETE FROM live_sessions WHERE sender_id=? AND chat_id=?',(event['senderId'],event['chatId']))
                            db.execute('DELETE FROM platform_session_permissions WHERE sender_id=?',(event['senderId'],))
                            db.execute("UPDATE live_outbox SET state='rejected',error_code='dialog_stopped' WHERE chat_id=? AND state='queued'",(event['chatId'],))
                            db.execute("UPDATE live_events SET state='rejected' WHERE event_id IN (SELECT event_id FROM live_outbox WHERE chat_id=? AND error_code='dialog_stopped')",(event['chatId'],))
                            reply['accessDenied']=True
                    if reply is None:
                        if self.assistant is None:raise ValueError('platform_assistant_required')
                        scope=digest(event['senderId']+':'+event['chatId'])
                        if document=={'error':'download_unavailable'}:
                            reply={'text':'Не удалось получить файл из MAX. Отправьте его ещё раз; введённые параметры сохранены.','buttons':[{'label':'Личный кабинет','command':'/home'}]}
                        elif document is not None:
                            reply=self.assistant.handle_file(session['actor_id'],document,event['eventId'],session_key=scope)
                        else:
                            if incoming.strip()=='/education':incoming='/learning'
                            reply=self.assistant.handle(session['actor_id'],incoming,event['eventId'],session_key=scope)
                        if reply is None:reply={'text':'Выберите действие в личном кабинете.','buttons':[{'label':'Личный кабинет','command':'/home'}]}
                        if incoming.strip().casefold() in {'/start','/home','/help'} and not reply.get('accessDenied'):
                            reply.setdefault('buttons',[]).append({'label':'Демо-роли','command':'/demo'})
                        target=reply.pop('inviteActorId',None)
                        if target is not None:
                            role=self._profile(db,session['actor_id'])['platformRole'];target_profile=self._profile(db,target)
                            if target_profile is None or role not in {'director','platform_admin'}or(role=='director'and target_profile['platformRole']not in {'teacher','student'}):
                                reply={'text':'Нет права приглашать этот профиль.','buttons':[{'label':'Личный кабинет','command':'/home'}]}
                            else:
                                try:
                                    invitation=self._new_invite(db,target,False)
                                    reply['text']+='\n\nОдноразовая ссылка на 15 минут. Передайте её только этому человеку:\nhttps://max.ru/'+self.bot_username+'?start='+invitation['payload']
                                except HTTPException as error:
                                    reply={'text':str(error.detail),'buttons':[{'label':'Пользователи','command':'/people'}]}
                        native_token=reply.pop('nativeInviteToken',None)
                        if native_token is not None:
                            if not _text(native_token,1000):raise ValueError('invalid_native_invitation')
                            role=self._profile(db,session['actor_id'])['platformRole']
                            if role not in {'director','access_admin','platform_admin'}:raise ValueError('invalid_native_invitation_role')
                            capability=secrets.token_urlsafe(32)
                            db.execute('INSERT INTO platform_native_invites VALUES(?,?,?,?,0)',
                                (digest(capability),native_token,session['actor_id'],stamp()+15*60*1000))
                            reply['text']+='\n\nОдноразовая ссылка на 15 минут для входа в MAX:\nhttps://max.ru/'+self.bot_username+'?start='+capability
            if session is not None and hasattr(self.assistant,'validate_reply'):
                reply=self.assistant.validate_reply(session['actor_id'],reply)
            denied=reply.pop('accessDenied',False)
            if reply.pop('_native',False):native=reply
            else:native=self._paged(db,session or {'actor_id':0},event,reply)
            outbox_id=digest('ideyafix-live-outbox:'+event['eventId'])
            db.execute("INSERT INTO live_outbox VALUES(?,?,?,?,'queued',NULL,NULL)",(outbox_id,event['eventId'],event['chatId'],native['text']))
            db.execute('INSERT INTO platform_reply_payloads VALUES(?,?)',(outbox_id,json.dumps(native,ensure_ascii=False)))
            db.execute('INSERT INTO platform_reply_owners VALUES(?,?)',(outbox_id,session['actor_id']if session is not None and not denied else 0))
            db.execute('INSERT INTO live_events VALUES(?,?,?)',(event['eventId'],event['payloadHash'],'queued'))
            return {'duplicate':False,'queued':1}

    def claim_next(self):
        item=super().claim_next()
        if item is None:return None
        with self.connection()as db:
            row=db.execute('SELECT payload FROM platform_reply_payloads WHERE outbox_id=?',(item['id'],)).fetchone()
            if row:item['reply']=json.loads(row['payload'])
            owner=db.execute('SELECT actor_id FROM platform_reply_owners WHERE outbox_id=?',(item['id'],)).fetchone()
            current=db.execute('SELECT actor_id FROM live_sessions WHERE chat_id=?',(item['chat_id'],)).fetchone()
        if owner and owner['actor_id']:
            try:allowed=bool(current and current['actor_id']==owner['actor_id']and self.assistant is not None and self.assistant.authorize(owner['actor_id']).get('status')==200)
            except Exception:allowed=False
            if not allowed:
                self.finish(item['id'],{'status':'rejected','code':'personal_access_changed','provider_message_id':None})
                return None
            if item.get('reply')is not None and hasattr(self.assistant,'validate_reply'):
                validated=self.assistant.validate_reply(owner['actor_id'],item['reply'])
                if validated.get('resourceDenied'):
                    if any(g.get('kind')=='schedule_notice'for g in item['reply'].get('resourceGuards',[])):
                        self.finish(item['id'],{'status':'rejected','code':'schedule_notice_stale','provider_message_id':None})
                        return None
                    # Native keyboard capabilities are unnecessary for this
                    # terminal notice; never pass internal command buttons.
                    item['reply']={'text':validated['text'],'buttons':[],'document':None,'resourceGuards':[]}
                    item['text']=validated['text']
                    with self.connection()as db:
                        db.execute('UPDATE platform_reply_payloads SET payload=? WHERE outbox_id=?',(json.dumps(item['reply'],ensure_ascii=False),item['id']))
                        db.execute('UPDATE live_outbox SET text=? WHERE id=?',(item['text'],item['id']))
        return item

    def image_is_current(self,item):
        """Recheck access across the separate image upload and message POST."""
        try:
            with self.connection() as db:
                row=db.execute("""
                    SELECT owner.actor_id FROM platform_reply_owners owner
                    JOIN live_outbox outbox ON outbox.id=owner.outbox_id
                    JOIN live_sessions session ON session.chat_id=outbox.chat_id AND session.actor_id=owner.actor_id
                    WHERE owner.outbox_id=? AND outbox.state='started' AND owner.actor_id>0
                """,(item['id'],)).fetchone()
            if row is None or self.assistant.authorize(row['actor_id']).get('status')!=200:
                return False
            return not self.assistant.validate_reply(row['actor_id'],item['reply']).get('resourceDenied')
        except Exception:
            return False
