"""Independent whole-channel critic: actual native API, only MAX I/O mocked."""
import json
import hashlib
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx

from app.max_settings import MaxSettings
from app.platform_assistant import PlatformAssistant
from app.platform_max_features import SCHEDULE_NOTICE_TEXT
from app.platform_runtime import PlatformRuntime
from app.platform_transport import PlatformMaxClient
from app.platform_wire import normalize_platform_update
from app.polling import Poller
from tests.test_platform_schedule import prepare_published_replacement


class NativeNoticeDeliveryTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(dir='/private/tmp',prefix='ifx-notice-delivery-critic.')
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.runtime=PlatformRuntime(self.root/'core')
        self.assistant=PlatformAssistant(self.runtime)
        self.api=[];self.fail_notice=False;self.committed_before_io=[]
        token=self.root/'synthetic-token'
        token.write_text('SYNTHETIC-NOTICE-DELIVERY-NOT-A-CREDENTIAL');token.chmod(0o600)
        self.client=PlatformMaxClient(token,transport=httpx.MockTransport(self.respond))
        settings=MaxSettings(True,'synthetic_notice_bot',token,None,self.root/'max.sqlite3',platform_enabled=True)
        self.worker=Poller(settings,client=self.client,platform_assistant=self.assistant)
        self.addCleanup(self.worker.close)
        for target in ('socket.socket.connect','socket.create_connection','socket.getaddrinfo'):
            context=patch(target,side_effect=AssertionError('Critic forbids real network'))
            blocked=context.start();self.addCleanup(context.stop)
            self.addCleanup(lambda mock=blocked:self.assertEqual(mock.call_count,0,'No real network may be attempted'))
        self.bind(710003,sender=980003,chat=990003)
        self.bind(710005,sender=980005,chat=990005)

    def respond(self,request):
        self.assertEqual((request.url.scheme,request.url.host),('https','platform-api2.max.ru'))
        self.assertEqual(request.url.path,'/messages')
        body=json.loads(request.content);chat=int(request.url.params['chat_id'])
        self.api.append({'chat':chat,'body':body})
        if body['text']==SCHEDULE_NOTICE_TEXT:
            with self.worker.store.connection() as db:
                rows=db.execute('''SELECT o.state,n.target_actor FROM platform_notice_queue n
                    JOIN live_outbox o ON o.id=n.outbox_id''').fetchall()
            self.assertEqual([(r['state'],r['target_actor']) for r in rows],[('started',710003)])
            self.committed_before_io.append(True)
            if self.fail_notice:
                raise httpx.ReadTimeout('synthetic outcome unknown',request=request)
        return httpx.Response(200,json={'message':{'body':{'mid':'synthetic-'+str(len(self.api))},
            'recipient':{'chat_type':'dialog','chat_id':chat}}})

    def incoming(self,text=None,*,sender=980005,chat=990005,start=None,callback=None):
        at=int(time.time()*1000)
        if start is not None:
            raw={'update_type':'bot_started','timestamp':at,'chat_id':chat,'user':{'user_id':sender},'payload':start}
        elif callback is not None:
            raw={'update_type':'message_callback','timestamp':at,
                'callback':{'callback_id':str(uuid4()),'user':{'user_id':sender},'payload':callback},
                'message':{'recipient':{'chat_type':'dialog','chat_id':chat}}}
        else:
            raw={'update_type':'message_created','timestamp':at,'message':{'sender':{'user_id':sender},
                'recipient':{'chat_type':'dialog','chat_id':chat},'body':{'mid':str(uuid4()),'text':text}}}
        return normalize_platform_update(raw)

    def ingest(self,text=None,**kwargs):
        event=self.incoming(text,**kwargs)
        self.worker.store.ingest(event)
        with self.worker.store.connection() as db:
            row=db.execute('''SELECT p.payload FROM platform_reply_payloads p JOIN live_outbox o
                ON o.id=p.outbox_id WHERE o.event_id=?''',(event['eventId'],)).fetchone()
        return event,json.loads(row['payload']) if row else None

    def drain(self):
        with patch('app.polling.time.sleep'):
            for _ in range(30):
                if not self.worker.store.safe_status()['queued']:
                    return
                self.worker.send_one()
        self.fail('Synthetic queue did not drain within the known fixture bound')

    def bind(self,actor,*,sender,chat,operator=False):
        invitation=self.worker.store.issue_invite(actor,operator=operator)
        self.ingest(start=invitation['payload'],sender=sender,chat=chat)
        self.drain()

    def command(self,text,**kwargs):
        event,reply=self.ingest(text,**kwargs);self.drain()
        return event,reply

    def native(self,actor,method,path,payload=None,event=None):
        value=self.runtime.request(actor,method,path,payload,event or (str(uuid4()) if method!='GET' else None))
        self.assertEqual(value['status'],200,value.get('data'))
        return value['data']

    def prepare_and_queue(self):
        self.command('/max prepare');self.command('/max confirm')
        self.command('/max choose 710003')
        event,receipt=self.ingest('/max confirm')
        self.assertNotIn('scheduleNoticeIntent',receipt)
        with self.worker.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM live_sessions').fetchone()[0],2)
        self.assertEqual(self.notice_rows()[0]['state'],'queued')
        self.assertEqual(self.notice_requests(),[])
        return event

    def notice_rows(self):
        with self.worker.store.connection() as db:
            return [dict(r) for r in db.execute('''SELECT n.*,o.state,o.error_code FROM platform_notice_queue n
                JOIN live_outbox o ON o.id=n.outbox_id ORDER BY n.notice_id''')]

    def notice_requests(self):
        return [r for r in self.api if r['body']['text']==SCHEDULE_NOTICE_TEXT]

    def test_confirmed_director_to_teacher_commits_before_mock_io_exactly_once(self):
        event=self.prepare_and_queue()
        self.assertEqual(self.worker.store.ingest(event),{'duplicate':True,'queued':0})
        self.drain()
        self.assertEqual(self.notice_rows()[0]['state'],'accepted')
        self.assertEqual(self.committed_before_io,[True])
        self.assertEqual(len(self.notice_requests()),1)
        self.assertEqual(self.notice_requests()[0]['chat'],990003)
        self.assertEqual(self.notice_requests()[0]['body']['text'],SCHEDULE_NOTICE_TEXT)
        self.command('/max choose 710003');self.command('/max confirm')
        self.assertEqual(len(self.notice_rows()),1)
        self.assertEqual(len(self.notice_requests()),1)

    def test_native_acknowledgement_before_claim_cancels_without_send(self):
        self.prepare_and_queue()
        row=self.native(710003,'GET','/api/staff/schedule-notifications')['items'][0]
        self.native(710003,'POST','/api/staff/schedule-notifications/'+row['id']+'/acknowledge',
                    {'expectedRevision':row['revision']})
        self.drain()
        self.assertEqual(self.notice_rows()[0]['state'],'rejected')
        self.assertEqual(self.notice_rows()[0]['error_code'],'schedule_notice_stale')
        self.assertEqual(self.notice_requests(),[])

    def test_actual_publication_change_before_claim_cancels_without_send(self):
        self.prepare_and_queue()
        old=self.notice_rows()[0]['publication_version']
        new=prepare_published_replacement(self.runtime)['publication']['version']
        self.assertGreater(new,old)
        self.drain()
        self.assertEqual(self.notice_rows()[0]['state'],'rejected')
        self.assertEqual(self.notice_requests(),[])

    def test_native_teacher_revoke_before_claim_cancels_without_send(self):
        self.prepare_and_queue()
        member=self.native(710003,'GET','/api/staff/me')['profile']['membershipId']
        self.native(710005,'POST','/api/staff/members/'+member+'/revoke',{})
        self.drain()
        self.assertEqual(self.notice_rows()[0]['state'],'rejected')
        self.assertEqual(self.notice_requests(),[])

    def test_teacher_unlink_before_claim_cancels_without_send(self):
        self.prepare_and_queue()
        self.ingest('/max revoke',sender=980003,chat=990003)
        self.ingest('/max confirm',sender=980003,chat=990003)
        self.drain()
        self.assertEqual(self.notice_rows()[0]['state'],'rejected')
        self.assertEqual(self.notice_requests(),[])
        with self.worker.store.connection() as db:
            self.assertIsNone(db.execute('SELECT actor_id FROM live_sessions WHERE sender_id=?',('980003',)).fetchone())

    def test_unknown_transport_receipt_cannot_be_forced_into_new_enqueue(self):
        self.prepare_and_queue();self.fail_notice=True
        self.drain()
        self.assertEqual(self.notice_rows()[0]['state'],'unknown')
        self.assertEqual(len(self.notice_requests()),1)
        self.worker.store.recover();self.fail_notice=False
        self.command('/max choose 710003');_,reply=self.command('/max confirm')
        self.assertIn('автоматический повтор выключен',reply['text'])
        self.assertEqual(len(self.notice_rows()),1)
        self.assertEqual(self.notice_rows()[0]['state'],'unknown')
        self.assertEqual(len(self.notice_requests()),1)

    def test_started_attempt_recovers_unknown_without_resend_or_second_enqueue(self):
        self.prepare_and_queue()
        item=self.worker.store.claim_next()
        self.assertEqual(item['text'],SCHEDULE_NOTICE_TEXT)
        self.assertEqual(self.worker.store.recover()['unknown'],1)
        self.drain()
        self.command('/max choose 710003');self.command('/max confirm')
        self.assertEqual(len(self.notice_rows()),1)
        self.assertEqual(self.notice_rows()[0]['state'],'unknown')
        self.assertEqual(self.notice_requests(),[])

    def test_stale_confirmation_callback_after_role_away_and_back_never_queues(self):
        self.bind(710005,sender=980005,chat=990005,operator=True)
        self.command('/max prepare');self.command('/max confirm')
        _,reply=self.command('/max choose 710003')
        payload=next(b['payload'] for b in reply['buttons'] if b['label']=='Передать')
        self.command('/role марина');self.command('/role роман')
        _,rejected=self.ingest(callback=payload)
        self.assertIn('кнопка уже использована',rejected['text'])
        self.drain()
        self.assertEqual(self.notice_rows(),[])
        self.assertEqual(self.notice_requests(),[])

    def test_teacher_private_assignment_outbox_is_denied_after_native_pupil_archive(self):
        marker='SYNTHETIC_PRIVATE_ASSIGNMENT_MUST_NOT_SEND_AFTER_ARCHIVE'
        assignment=self.native(710003,'POST','/api/teacher/learning/assignments',{
            'studentId':'ifx-student-1','subject':'Математика','topic':'Доли','gap':'Практика',
            'content':marker,'answers':'Вымышленный ответ преподавателя','generate':False})['assignment']
        self.command('/learning list',sender=980003,chat=990003)
        event,reply=self.ingest('/learning choose 1',sender=980003,chat=990003)
        self.assertIn(marker,reply['text'])
        revision=self.native(710008,'GET','/api/school/workspace')['revision']
        self.native(710008,'POST','/api/school/records/students/ifx-student-1/archive',{'expectedRevision':revision})
        self.assertEqual(self.runtime.authorize(710003)['status'],200)
        self.assertEqual(self.runtime.request(710003,'GET','/api/teacher/learning/assignments/'+assignment['id'])['status'],404)
        scope=hashlib.sha256(b'980003:990003').hexdigest()
        cached=self.assistant.handle(710003,'/learning choose 1',event['eventId'],session_key=scope)
        self.assertNotIn(marker,cached['text'])
        sent_before=len(self.api)
        self.drain()
        self.assertFalse(any(marker in request['body']['text'] for request in self.api[sent_before:]))
        revision=self.native(710008,'GET','/api/school/workspace')['revision']
        self.native(710008,'POST','/api/school/records/students/ifx-student-1/restore',{'expectedRevision':revision})
        restored=self.assistant.handle(710003,'/learning choose 1',event['eventId'],session_key=scope)
        self.assertIn(marker,restored['text'])


if __name__=='__main__':
    unittest.main()
