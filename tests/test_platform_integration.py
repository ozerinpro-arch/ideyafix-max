"""Whole local MAX chain against the separate authenticated platform engine.

Only MAX's network is substituted. Wire parsing, durable personal binding,
callbacks, conversations, native authorization, domain writes and receipts run.
"""
from pathlib import Path
from base64 import b64decode
from io import BytesIO
import tempfile
import time
import unittest
from uuid import uuid4
from unittest.mock import patch
from PIL import Image

from app.max_settings import MaxSettings
from app.platform_assistant import PlatformAssistant
from app.platform_runtime import PlatformRuntime
from app.polling import Poller
from app.vendor.max_transport import TransportOutcome


class LocalMax:
    def __init__(self):
        self.updates=[];self.marker=0;self.sent=[];self.answers=[];self.status='accepted'
    def get_me(self):return TransportOutcome('accepted','bot_identity',200,data={'username':'synthetic_bot','isBot':True})
    def list_subscriptions(self):return TransportOutcome('accepted','subscriptions',200,data={'subscriptions':[]})
    def get_updates(self,**kwargs):
        self.marker+=1;updates,self.updates=self.updates,[]
        return TransportOutcome('accepted','updates',200,data={'updates':updates,'marker':self.marker})
    def send_reply(self,chat,text,**kwargs):
        guard=kwargs.pop('image_guard',None)
        if guard is not None and not guard():
            return TransportOutcome('rejected','image_access_changed')
        self.sent.append({'chat':chat,'text':text,**kwargs})
        return TransportOutcome(self.status,'synthetic_receipt',200,provider_message_id=str(uuid4())if self.status=='accepted'else None)
    def answer_callback(self,identity):self.answers.append(identity)
    def close(self):pass


class FullPlatformIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='ifx-full-chain-',dir='/private/tmp')
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.root.chmod(0o700)
        self.runtime=PlatformRuntime(self.root/'core')
        self.assistant=PlatformAssistant(self.runtime)
        self.client=LocalMax()
        self.settings=MaxSettings(True,'synthetic_bot',self.root/'never-read-token',None,self.root/'max.sqlite3',platform_enabled=True)
        self.worker=Poller(self.settings,client=self.client,platform_assistant=self.assistant)
        self.addCleanup(self.worker.close)

    def incoming(self,text=None,*,sender=123,chat=456,start=None,callback=None):
        at=int(time.time()*1000)
        if start is not None:
            return {'update_type':'bot_started','timestamp':at,'chat_id':chat,'user':{'user_id':sender},'payload':start}
        if callback is not None:
            return {'update_type':'message_callback','timestamp':at,'callback':{'callback_id':str(uuid4()),'user':{'user_id':sender},'payload':callback},
                    'message':{'recipient':{'chat_type':'dialog','chat_id':chat}}}
        return {'update_type':'message_created','timestamp':at,'message':{'sender':{'user_id':sender},
                'recipient':{'chat_type':'dialog','chat_id':chat},'body':{'mid':str(uuid4()),'text':text}}}

    def poll(self,update):
        self.client.updates=[update]
        result=self.worker.poll_once()
        self.assertEqual(result['skipped'],0)
        return self.client.sent[-1]

    def bind(self,actor,**identity):
        invitation=self.worker.store.issue_invite(actor)
        return self.poll(self.incoming(start=invitation['payload'],**identity))

    def test_role_menus_callback_native_schedule_and_private_student_boundary(self):
        self.bind(710007)
        menu=self.client.sent[-1]
        self.assertIn('Славик Демо',menu['text'])
        self.assertIn({'label':'Открыть школу','url':'https://taskfloww.site/school-staff/'},menu['buttons'])
        labels={b['label']:b['payload']for b in menu['buttons'] if 'payload' in b}
        self.assertNotIn('Люди и доступ',labels)
        self.assertFalse(any('Хакатон' in label or 'MAX' in label for label in labels))
        with Image.open(BytesIO(b64decode(menu['image']['base64']))) as cover:
            self.assertEqual(cover.size, (960, 640))
        reply=self.poll(self.incoming(callback=labels['Моё расписание']))
        self.assertTrue(reply['buttons'])
        with Image.open(BytesIO(b64decode(self.client.sent[-1]['image']['base64']))) as card:
            self.assertGreater(card.width, 500)
        self.assertIn('Математика',self.client.sent[-1]['text'])
        week=next(b['payload'] for b in self.client.sent[-1]['buttons'] if b['label']=='Вся неделя')
        self.assertEqual(len(self.client.answers),1)
        self.poll(self.incoming(callback=week))
        self.assertEqual(self.client.sent[-1]['image']['name'],'schedule-week.jpg')
        self.poll(self.incoming('/role роман'))
        self.assertIn('закреплён личный профиль',self.client.sent[-1]['text'])
        self.assertEqual(self.runtime.request(710007,'GET','/api/staff/workspace')['status'],403)
        self.assertEqual(self.runtime.request(710007,'GET','/api/student/schedule')['status'],200)
        # Another MAX account cannot reuse a personal action capability.
        self.poll(self.incoming(callback=labels['Мои задания'],sender=777,chat=888))
        self.assertIn('одноразовую ссылку',self.client.sent[-1]['text'])

    def test_max_material_conversation_confirmation_replay_restart_and_unknown_send(self):
        self.bind(710003)
        self.poll(self.incoming('/materials new'))
        for answer in ['1','План из MAX','Доли','ИФ-7А','Математика','Сравнивать доли','40','1','Содержимое плана','Закрытые ответы']:
            reply=self.poll(self.incoming(answer))
        confirm=next(b['payload']for b in reply['buttons']if b['label']=='Подтвердить')
        update=self.incoming(callback=confirm)
        self.client.status='unknown'
        self.poll(update)
        data=self.runtime.request(710003,'GET','/api/teacher/preparation/materials')['data']
        self.assertEqual(len(data['materials']),1)
        sent_count=len(self.client.sent)
        self.worker.poll_once()
        self.assertEqual(len(self.client.sent),sent_count)
        self.client.status='accepted'
        self.poll(update)  # Duplicate provider event does not rerun/write/send.
        self.assertEqual(len(self.client.sent),sent_count)
        self.assertEqual(self.worker.store.safe_status()['unknown'],1)
        self.assistant.close()
        self.runtime=PlatformRuntime(self.root/'core')
        self.assistant=PlatformAssistant(self.runtime)
        self.worker=Poller(self.settings,client=self.client,platform_assistant=self.assistant)
        self.addCleanup(self.worker.close)
        self.poll(self.incoming('/materials list'))
        self.assertIn('План из MAX',self.client.sent[-1]['text'])
        self.assertEqual(len(self.runtime.request(710003,'GET','/api/teacher/preparation/materials')['data']['materials']),1)

    def test_all_native_profile_menus_and_dispatch_after_session_restart(self):
        profiles=self.runtime.profiles()
        self.assertEqual(len(profiles),9)
        for profile in profiles:
            reply=self.assistant.handle(profile['id'],'/start',str(uuid4()),session_key='synthetic-menus')
            self.assertIn(profile['name'],reply['text'])
            self.assertTrue(reply['buttons'])
            self.assertLessEqual(len(reply['buttons']),6)
            for item in reply['buttons']:
                command=item['command']
                self.assertNotIn(command,{'/hackathon','/max'})
                if command.startswith('/section '):
                    section=self.assistant.handle(profile['id'],command,str(uuid4()),session_key='synthetic-menus')
                    actions={button['command']for button in section['buttons']}
                    self.assertGreater(len(actions),1)
                    self.assertIn('/home',actions)
                    self.assertIn('image',section)
                    self.assertFalse(actions & {'/hackathon','/max'})
                    if '/max notices' in actions:
                        self.assertEqual(profile['role'],'director')
        self.bind(710006)
        self.poll(self.incoming('/calendar'))
        self.assertIn('buttons',self.client.sent[-1])
        self.assertEqual(self.runtime.request(710006,'GET','/api/school/calendar')['status'],200)
        self.assertEqual(self.runtime.request(710004,'GET','/api/school/calendar')['status'],403)

    def test_planner_opens_replacement_material_status_from_role_menu(self):
        self.bind(710006)
        for label in ('Учебный день','Материалы замен','Текущие передачи'):
            action=next(button['payload'] for button in self.client.sent[-1]['buttons'] if button['label']==label)
            self.poll(self.incoming(callback=action))
        self.assertIn('Передача материалов для опубликованных замен',self.client.sent[-1]['text'])
        self.assertNotIn('Операция недоступна',self.client.sent[-1]['text'])

    def test_new_native_invitation_is_claimed_in_max_and_lost_rpc_keeps_owner(self):
        self.bind(710005)
        nomination=self.runtime.request(710005,'POST','/api/staff/nominations',{
            'username':'ifx.max.new','name':'Новый завуч Демо','role':'planner'},str(uuid4()))
        self.assertEqual(nomination['status'],200)
        actions=[a for a in self.assistant.handlers['schedule'].actions if a['namespace']=='/admin']
        index=next(i+1 for i,a in enumerate(actions)if a['nativeEndpoint']=='/api/staff/invites'and a['method']=='POST')
        self.poll(self.incoming('/admin action '+str(index)))
        self.poll(self.incoming(nomination['data']['id']))
        reply=self.poll(self.incoming('/admin confirm'))
        self.assertIn('https://max.ru/synthetic_bot?start=',reply['text'])
        self.assertNotIn('/__native/',reply['text'])
        token=reply['text'].split('?start=')[1].strip()
        original=self.assistant.claim_native_invite
        calls=[]
        def lose_response(capability,event_id):
            result=original(capability,event_id);calls.append(result)
            if len(calls)==1:raise RuntimeError('Synthetic lost reply after genuine native acceptance')
            return result
        with patch.object(self.assistant,'claim_native_invite',side_effect=lose_response):
            with self.assertRaises(RuntimeError):self.poll(self.incoming(start=token,sender=777,chat=888))
            # The source invitation is now consumed, but the MAX commit was lost.
            # A different account must not claim its cached native profile.
            from app.platform_wire import normalize_platform_update
            self.worker.store.ingest(normalize_platform_update(self.incoming(start=token,sender=999,chat=999)))
            self.assertEqual(len(calls),1)
            # The receiver recovers its durable original batch before fetching
            # newer provider updates, including on the first marker=None batch.
            self.worker.poll_once()
            other=next(row for row in self.client.sent if row['chat']==999)
            self.assertIn('одноразовую ссылку',other['text'])
            own=self.client.sent[-1]
        self.assertIn('Новый завуч Демо',own['text'])
        self.assertEqual(calls[0]['data']['profile'],calls[1]['data']['profile'])
        profile=calls[1]['data']['profile']
        self.assertGreaterEqual(profile['id'],720001)
        denied=self.poll(self.incoming('/role роман',sender=777,chat=888))
        self.assertIn('закреплён личный профиль',denied['text'])
        self.assertEqual(self.runtime.request(profile['id'],'GET','/api/planning/config')['status'],200)
        self.assertEqual(self.runtime.request(profile['id'],'GET','/api/teacher/preparation')['status'],403)
        self.assertIn(profile['id'],{p['id']for p in self.worker.store.profiles()})

    def test_revocation_blocks_cached_pages_and_already_queued_private_reply(self):
        from app.platform_wire import normalize_platform_update
        self.bind(710009)
        actions=[a for a in self.assistant.handlers['schedule'].actions if a['namespace']=='/admin']
        index=next(i+1 for i,a in enumerate(actions)if a['nativeEndpoint']=='/api/staff/directory'and a['method']=='GET')
        command='/admin action '+str(index)
        prior=self.assistant.handle(710009,command,'private-directory-read',session_key='private-direct-check')
        self.assertIn('Марина Демо',prior['text'])
        self.worker.store.ingest(normalize_platform_update(self.incoming(command)))
        member=self.runtime.request(710009,'GET','/api/staff/me')['data']['profile']['membershipId']
        revoked=self.runtime.request(710005,'POST','/api/staff/members/'+member+'/revoke',{},str(uuid4()))
        self.assertEqual(revoked['status'],200)
        self.assertEqual(self.runtime.authorize(710009)['status'],401)
        for command,event in [('/admin result 1','new-page-event'),('/admin action '+str(index),'private-directory-read')]:
            denied=self.assistant.handle(710009,command,event,session_key='private-direct-check')
            self.assertIn('отозван',denied['text'])
            self.assertNotIn('Марина Демо',denied['text'])
            self.assertNotIn('document',denied)
        self.assertIsNone(self.worker.store.claim_next())
        self.assertGreaterEqual(self.worker.store.safe_status()['rejected'],1)
        reply=self.poll(self.incoming('/admin result 1'))
        self.assertIn('отозван',reply['text'])
        self.assertNotIn('Марина Демо',reply['text'])

    def test_max_schedule_notice_uses_current_binding_and_exactly_one_outbox(self):
        from app.platform_max_features import SCHEDULE_NOTICE_TEXT
        self.bind(710003)
        self.bind(710005,sender=777,chat=888)
        def director(command):return self.poll(self.incoming(command,sender=777,chat=888))
        director('/max prepare');director('/max confirm')
        selected=director('/max choose 710003')
        self.assertIn('Марина Демо',selected['text'])
        director('/max confirm')
        self.worker.send_one()  # The initiating director's separate receipt.
        messages=[r for r in self.client.sent if r['chat']==456 and r['text']==SCHEDULE_NOTICE_TEXT]
        self.assertEqual(len(messages),1)
        self.assertEqual(len(messages[0]['buttons']),1)
        director('/max choose 710003');director('/max confirm')
        self.worker.send_one()
        self.assertEqual(len([r for r in self.client.sent if r['chat']==456 and r['text']==SCHEDULE_NOTICE_TEXT]),1)
        with self.worker.store.connection()as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM platform_notice_queue').fetchone()[0],1)
        director('/max revoke');reply=director('/max confirm')
        self.assertIn('Отвязка',reply['text'])
        unbound=director('/start')
        self.assertIn('одноразовую ссылку',unbound['text'])


if __name__=='__main__':unittest.main()
