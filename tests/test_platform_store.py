import base64
from pathlib import Path
import tempfile
import time
import json
import unittest
from unittest.mock import patch
from uuid import uuid4
from fastapi import HTTPException
from app.platform_store import PlatformStore
from app.live_store import LiveStore
from app.school_demo_links import DEMO_ROLES, SCHOOL_STAFF_URL
from app.vendor.max_wire import normalize_update

def native(kind='message_created',sender=123,chat=456,text='/start',payload=None):
    at=int(time.time()*1000)
    if kind=='message_callback':
        raw=dict(update_type=kind,timestamp=at,callback=dict(callback_id=str(uuid4()),user={'user_id':sender},payload=payload),
                 message={'recipient':{'chat_type':'dialog','chat_id':chat}})
    elif kind=='bot_started':raw=dict(update_type=kind,timestamp=at,chat_id=chat,user={'user_id':sender},payload=payload)
    else:raw=dict(update_type=kind,timestamp=at,message={'sender':{'user_id':sender},'recipient':{'chat_type':'dialog','chat_id':chat},'body':{'mid':str(uuid4()),'text':text}})
    return normalize_update(json.dumps(raw).encode()).as_dict()

class FakeAssistant:
    def __init__(self):self.calls=[]
    def authorize(self,actor):return {'status':200}
    def handle(self,actor,text,event_id,**kwargs):
        self.calls.append((actor,text))
        return {'text':'Профиль '+str(actor),'buttons':[{'label':'Расписание','command':'/schedule'}]}
    def handle_file(self,actor,document,event_id,**kwargs):
        self.calls.append((actor,'file'))
        return {'text':'Файл принят','buttons':[],'document':document}

class PlatformSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir='/private/tmp',prefix='ifx-platform-store.')
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'outer.sqlite3';self.assistant=FakeAssistant()
        self.store=PlatformStore(self.path,self.assistant)

    def bind(self,actor=710007,operator=False,sender=123,chat=456):
        invite=self.store.issue_invite(actor,operator=operator)
        event=native('bot_started',sender,chat,payload=invite['payload'])
        self.store.ingest(event)
        return event

    def enable_web_demo(self):
        accounts = {role: {'login': f'ifx.demo.{role}', 'password': f'sample_{role}_password'}
                    for role in DEMO_ROLES}
        self.store = PlatformStore(self.path, self.assistant, web_demo_accounts=accounts)
        return accounts

    def test_personal_profile_cannot_switch_roles_or_bind_twice(self):
        self.bind()
        self.assertEqual(self.assistant.calls[-1],(710007,'/start'))
        self.store.ingest(native(text='/role роман'))
        self.assertEqual(len(self.assistant.calls),1)
        self.store.ingest(native(text='/schedule'))
        self.assertEqual(self.assistant.calls[-1],(710007,'/schedule'))
        with self.assertRaises(HTTPException):self.store.issue_invite(710007)

    def test_unbound_start_offers_school_link_without_platform_access(self):
        self.store.ingest(native('bot_started'))
        reply = self.store.claim_next()['reply']
        self.assertEqual(reply['buttons'], [
            {'label': 'Открыть школу', 'url': 'https://taskfloww.site/school-staff/'},
        ])
        self.assertEqual(self.assistant.calls, [])
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM live_sessions').fetchone()[0], 0)

    def test_public_demo_role_button_returns_only_that_roles_login(self):
        accounts = self.enable_web_demo()
        self.store.ingest(native('bot_started'))
        welcome_item = self.store.claim_next()
        welcome = welcome_item['reply']
        self.assertEqual(welcome['image']['name'], 'hackathon-school.jpg')
        self.assertTrue(self.store.image_is_current(welcome_item))
        self.assertEqual([button['label'] for button in welcome['buttons']],
                         ['Ученик', 'Преподаватель', 'Завуч', 'Директор'])
        token = welcome['buttons'][0]['payload']

        self.store.ingest(native('message_callback', sender=124, chat=457, payload=token))
        self.assertNotIn(accounts['student']['password'], self.store.claim_next()['text'])

        self.store.ingest(native('message_callback', payload=token))
        reply = self.store.claim_next()['reply']
        self.assertEqual(reply['buttons'][:3], [
            {'label': 'Скопировать логин', 'copy': accounts['student']['login']},
            {'label': 'Скопировать пароль', 'copy': accounts['student']['password']},
            {'label': 'Перейти в школу', 'url': SCHOOL_STAFF_URL},
        ])
        self.assertIn(accounts['student']['password'], reply['text'])
        self.assertNotIn(accounts['director']['password'], reply['text'])
        self.assertEqual(self.assistant.calls, [])

    def test_public_help_and_typed_demo_roles_are_text_only(self):
        accounts = self.enable_web_demo()
        self.store.ingest(native(text='/help'))
        help_reply = self.store.claim_next()['reply']
        self.assertIn('напишите название роли', help_reply['text'])
        self.assertEqual(len(help_reply['buttons']), len(DEMO_ROLES))
        self.assertNotIn('image', help_reply)
        for text, role in (
            ('  УЧЕНИК ', 'student'), ('/demo    teacher', 'teacher'),
            ('/demo Завуч', 'planner'), ('Директор', 'director'),
        ):
            with self.subTest(role=role):
                self.store.ingest(native(text=text))
                reply = self.store.claim_next()['reply']
                self.assertEqual(reply['buttons'][0]['copy'], accounts[role]['login'])
                self.assertEqual(reply['buttons'][1]['copy'], accounts[role]['password'])
                self.assertEqual(reply['buttons'][2]['url'], SCHOOL_STAFF_URL)
                self.assertIn('введите их на сайте', reply['text'])
                self.assertNotIn('image', reply)
        self.assertEqual(self.assistant.calls, [])
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM live_sessions').fetchone()[0], 0)

    def test_old_public_button_still_works_and_unknown_button_offers_recovery(self):
        accounts = self.enable_web_demo()
        with patch('app.platform_store.stamp') as now:
            initial = int(time.time() * 1000)
            now.return_value = initial
            self.store.ingest(native(text='/start'))
            first = self.store.claim_next()['reply']
            self.assertIn('image', first)
            expired = first['buttons'][0]['payload']

            now.return_value = initial + 16 * 60 * 1000
            self.store.ingest(native('message_callback', payload=expired))
            chosen = self.store.claim_next()['reply']
            self.assertEqual(chosen['buttons'][0]['copy'], accounts['student']['login'])

            self.store.ingest(native('message_callback', payload='x'*43))
            recovery = self.store.claim_next()['reply']
            self.assertIn('Кнопка устарела', recovery['text'])
            self.assertNotIn('image', recovery)
            fresh = recovery['buttons'][0]['payload']
            self.assertNotEqual(fresh, expired)
            self.store.ingest(native('message_callback', payload=fresh))
            self.assertEqual(self.store.claim_next()['reply']['buttons'][0]['copy'],
                             accounts['student']['login'])

            self.store.ingest(native('bot_started', payload='invalid-invite'))
            invalid = self.store.claim_next()['reply']
            self.assertIn('Ссылка для личного входа недействительна', invalid['text'])
            self.assertEqual(len(invalid['buttons']), len(DEMO_ROLES))
            self.assertNotIn('image', invalid)
        self.assertEqual(self.assistant.calls, [])

    def test_old_demo_menu_survives_private_revocation_without_granting_access(self):
        accounts = self.enable_web_demo()
        with patch('app.platform_store.stamp') as now:
            initial = int(time.time() * 1000)
            now.return_value = initial
            self.bind()
            self.store.claim_next()
            self.store.ingest(native(text='/start'))
            menu = self.store.claim_next()['reply']
            teacher = next(button['payload'] for button in menu['buttons'] if button['label'] == 'Преподаватель')
            home = next(button['payload'] for button in menu['buttons'] if button['label'] == 'Мой кабинет MAX')

            self.assistant.authorize = lambda actor: {'status': 401}
            now.return_value = initial + 9 * 3600 * 1000
            self.store.ingest(native('message_callback', payload=teacher))
            chosen = self.store.claim_next()['reply']
            self.assertEqual(chosen['buttons'][0]['copy'], accounts['teacher']['login'])
            self.assertNotIn('Мой кабинет MAX', [button['label'] for button in chosen['buttons']])

            self.store.ingest(native('message_callback', payload=home))
            denied = self.store.claim_next()['reply']
            self.assertIn('отозван', denied['text'])
            self.store.ingest(native('message_callback', payload=denied['buttons'][0]['payload']))
            recovered = self.store.claim_next()['reply']
            self.assertEqual([button['label'] for button in recovered['buttons']],
                             ['Ученик', 'Преподаватель', 'Завуч', 'Директор'])
            self.store.ingest(native(text='/demo Завуч'))
            self.assertEqual(self.store.claim_next()['reply']['buttons'][0]['copy'],
                             accounts['planner']['login'])
        self.assertEqual(self.assistant.calls, [(710007, '/start')])

    def test_bound_demo_selection_can_return_to_same_private_cabinet(self):
        self.enable_web_demo()
        self.bind()
        private = self.store.claim_next()['reply']
        demo = next(button['payload'] for button in private['buttons'] if button['label'] == 'Открыть школу')
        self.store.ingest(native('message_callback', payload=demo))
        menu = self.store.claim_next()['reply']
        self.assertNotIn('image', menu)
        director = next(button['payload'] for button in menu['buttons'] if button['label'] == 'Директор')
        self.store.ingest(native('message_callback', payload=director))
        chosen = self.store.claim_next()['reply']
        home = next(button['payload'] for button in chosen['buttons'] if button['label'] == 'Мой кабинет MAX')
        self.store.ingest(native('message_callback', payload=home))
        self.assertEqual(self.store.claim_next()['text'], 'Профиль 710007')
        self.assertEqual(self.assistant.calls, [(710007, '/start'), (710007, '/home')])

    def test_operator_can_switch_but_old_button_does_not_change_new_profile(self):
        self.bind(710001,True)
        first=self.store.claim_next();button=first['reply']['buttons'][0]['payload']
        self.store.ingest(native(text='/role роман'))
        self.assertEqual(self.assistant.calls[-1],(710005,'/start'))
        calls=len(self.assistant.calls)
        self.store.ingest(native('message_callback',payload=button))
        self.assertEqual(len(self.assistant.calls),calls)

    def test_callback_is_scoped_one_time_and_replay_deduplicated(self):
        self.bind();first=self.store.claim_next();token=first['reply']['buttons'][0]['payload']
        click=native('message_callback',payload=token)
        self.store.ingest(click)
        self.assertEqual(self.assistant.calls[-1],(710007,'/schedule'))
        calls=len(self.assistant.calls)
        self.assertTrue(self.store.ingest(click)['duplicate'])
        self.store.ingest(native('message_callback',payload=token))
        self.assertEqual(len(self.assistant.calls),calls)
        self.store.ingest(native('message_callback',sender=999,chat=999,payload=token))
        self.assertEqual(len(self.assistant.calls),calls)

    def test_role_switch_retires_expired_button_even_if_clock_moves_back(self):
        with patch('app.platform_store.stamp') as now:
            now.return_value = int(time.time() * 1000)
            original = now.return_value
            self.bind(operator=True)
            token = self.store.claim_next()['reply']['buttons'][0]['payload']
            now.return_value = original + 16 * 60 * 1000
            self.store.ingest(native(text='/role 710005'))
            self.store.ingest(native(text='/role 710007'))
            calls = len(self.assistant.calls)
            now.return_value = original + 1
            self.store.ingest(native('message_callback', payload=token))
            self.assertEqual(len(self.assistant.calls), calls)

    def test_stale_tap_preserves_current_action_and_home_is_reusable(self):
        self.assistant.handle=lambda actor,text,event,**kwargs:{
            'text':text,'buttons':[{'label':'Главная','command':'/home'},
                                   {'label':'Текущий шаг','command':'/schedule confirm'}]}
        self.bind()
        first=self.store.claim_next()['reply']['buttons']
        home, stale=(item['payload'] for item in first[:2])
        self.store.ingest(native('message_callback',payload=home))
        current=self.store.claim_next()['reply']['buttons'][1]['payload']
        self.store.ingest(native('message_callback',payload=stale))
        self.assertIn('устарела',self.store.claim_next()['text'])
        self.store.ingest(native('message_callback',payload=current))
        self.assertEqual(self.store.claim_next()['text'],'/schedule confirm')
        self.store.ingest(native('message_callback',payload=home))
        self.assertEqual(self.store.claim_next()['text'],'/home')

    def test_demo_role_changes_target_link_without_rebinding_profile(self):
        self.bind()
        first=self.store.claim_next()['reply']
        demo=next(button['payload'] for button in first['buttons'] if button['label']=='Демо-роли')
        self.store.ingest(native('message_callback',payload=demo))
        choices=self.store.claim_next()['reply']['buttons']
        director=next(button['payload'] for button in choices if button['label']=='Директор')
        teacher=next(button['payload'] for button in choices if button['label']=='Преподаватель')
        for token,role in ((director,'director'),(teacher,'teacher')):
            self.store.ingest(native('message_callback',payload=token))
            reply=self.store.claim_next()['reply']
            self.assertEqual(reply['buttons'][0]['url'],f'http://127.0.0.1:5212/?role={role}')
        with self.store.connection()as db:
            self.assertEqual(db.execute('SELECT actor_id FROM live_sessions').fetchone()[0],710007)
            self.assertEqual(db.execute('SELECT operator FROM platform_session_permissions').fetchone()[0],0)
        self.assertEqual(self.assistant.calls,[(710007,'/start')])

    def test_existing_demo_operator_permission_migrates_but_new_invite_is_locked(self):
        with self.store.connection()as db:
            db.execute("DELETE FROM live_meta WHERE key='platform_permissions_initialized'")
            db.execute('INSERT INTO live_sessions VALUES(?,?,?)',('111','222',710003))
        again=PlatformStore(self.path,self.assistant)
        again.ingest(native(sender=111,chat=222,text='/role завуч'))
        self.assertEqual(self.assistant.calls[-1],(710006,'/start'))
        self.bind(710007)
        self.store.ingest(native(text='/role администратор'))
        self.assertNotEqual(self.assistant.calls[-1][0],710008)

    def test_document_payload_and_pagination_are_durable(self):
        self.bind();first=self.store.claim_next();self.store.finish(first['id'],{'status':'accepted','code':'ok','provider_message_id':'fake'})
        event=native(text='Файл')
        event['file']={'name':'импорт.csv','contentType':'text/csv','base64':base64.b64encode('ученик\nСлавик'.encode()).decode()}
        self.store.ingest(event)
        item=self.store.claim_next()
        self.assertEqual(item['reply']['document']['name'],'импорт.csv')
        self.assertEqual(self.assistant.calls[-1],(710007,'file'))

    def test_native_invite_reserves_owner_before_lost_rpc_and_dynamic_profile_is_locked(self):
        self.bind(710005)
        original=self.assistant.handle
        self.assistant.handle=lambda *args,**kwargs:{'text':'Приглашение','buttons':[],'nativeInviteToken':'synthetic-private-native-capability'}
        self.store.ingest(native(text='/admin'))
        with self.store.connection()as db:
            row=db.execute('SELECT payload FROM platform_reply_payloads ORDER BY rowid DESC LIMIT 1').fetchone()
        token=json.loads(row['payload'])['text'].split('?start=')[1]
        self.assistant.handle=original
        calls=[]
        def claim(capability,event_id):
            calls.append(capability)
            if len(calls)==1:raise RuntimeError('Synthetic lost RPC reply')
            return {'status':200,'data':{'accepted':True,'profile':{'id':720001,'name':'Новый Демо','role':'teacher'}},'file':None}
        self.assistant.claim_native_invite=claim
        with self.assertRaises(RuntimeError):self.store.ingest(native('bot_started',777,888,payload=token))
        self.store.ingest(native('bot_started',999,999,payload=token))
        self.assertEqual(len(calls),1)
        self.store.ingest(native('bot_started',777,888,payload=token))
        self.assertEqual(self.assistant.calls[-1],(720001,'/start'))
        self.store.ingest(native(sender=777,chat=888,text='/role роман'))
        self.assertEqual(self.assistant.calls[-1],(720001,'/start'))
        restored=PlatformStore(self.path,self.assistant)
        self.assertIn(720001,{p['id']for p in restored.profiles()})
        self.assertNotIn('synthetic-private-native-capability',json.dumps(restored.safe_status()))

    def test_hackathon_survives_full_platform_and_is_personal_for_new_profiles(self):
        self.bind(710007)
        for command in ['/team Демо Славик','/idea Проверка | Вымышленная проверка гипотезы.','/apply']:
            self.store.ingest(native(text=command))
        with self.store.connection()as db:
            state=json.loads(db.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])
        self.assertEqual(state['teams'][0]['memberIds'],[710007])
        self.assertEqual(state['teams'][0]['application']['attempt'],1)
        self.store.ingest(native(text='/approve ИФ-001'))
        with self.store.connection()as db:
            state=json.loads(db.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])
        self.assertEqual(state['teams'][0]['application']['status'],'submitted')

    def test_inviting_already_bound_profile_returns_reply_and_commits_event(self):
        self.bind(710007)
        self.bind(710005,sender=777,chat=888)
        self.assistant.handle=lambda *args,**kwargs:{'text':'Приглашение','buttons':[],'inviteActorId':710007}
        event=native(sender=777,chat=888,text='/invite 710007')
        result=self.store.ingest(event)
        self.assertEqual(result['queued'],1)
        self.assertTrue(self.store.ingest(event)['duplicate'])

if __name__=='__main__':unittest.main()
