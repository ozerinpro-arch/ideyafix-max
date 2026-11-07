from pathlib import Path
import json
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from fastapi import HTTPException
from app.main import create_app
from app.max_settings import MaxSettings
from app.polling import Poller, exclusive_worker
from app.vendor.max_transport import TransportOutcome
from app.vendor.max_wire import normalize_update

class FakeClient:
    def __init__(self):
        self.updates = []
        self.marker = 7
        self.subscriptions = []
        self.sends = []
        self.send_status = 'accepted'

    def get_me(self):
        return TransportOutcome('accepted','bot_identity',200,data=dict(username='test_bot',isBot=True))

    def list_subscriptions(self):
        return TransportOutcome('accepted','subscriptions',200,data={'subscriptions':self.subscriptions})

    def get_updates(self, **_):
        return TransportOutcome('accepted','updates',200,data=dict(updates=self.updates,marker=self.marker))

    def send_message(self, chat, text):
        self.sends.append((chat,text))
        return TransportOutcome(self.send_status, 'fixture', 200,
                                provider_message_id='fake-receipt' if self.send_status=='accepted' else None)

    def close(self):
        pass

class PollingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir='/private/tmp',prefix='ideyafix-poll-test.')
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)
        self.settings=MaxSettings(True,'test_bot',self.path/'not-read-token',None,self.path/'demo.sqlite3')
        self.client=FakeClient()
        self.worker=Poller(self.settings,client=self.client)

    def start(self, payload):
        return dict(update_type='bot_started',timestamp=int(time.time()*1000),
                    chat_id=5678,user=dict(user_id=1234,is_bot=False),payload=payload)

    def message(self,text):
        return dict(update_type='message_created',timestamp=int(time.time()*1000),message=dict(
            sender=dict(user_id=1234,is_bot=False),recipient=dict(type='dialog',chat_id=5678),
            body=dict(mid=str(uuid4()),text=text)))

    def test_native_start_and_commands_commit_before_reply_and_replay_once(self):
        invitation=self.worker.store.issue_invite(710001)
        self.client.updates=[self.start(invitation['payload']),self.message('/team Нативная команда')]
        self.worker.poll_once()
        state=self.worker.store.view(710001)
        self.assertEqual(state['teams'][0]['name'],'Нативная команда')
        self.assertEqual(self.worker.store.get_cursor(),7)
        self.assertEqual(len(self.client.sends),1)
        self.worker.poll_once()
        self.assertEqual(len(self.client.sends),2)
        self.assertEqual(self.worker.store.safe_status()['accepted'],2)
        self.worker.poll_once()
        self.assertEqual(len(self.client.sends),2)

    def test_partial_storage_failure_never_advances_marker_and_prefix_replays(self):
        invitation=self.worker.store.issue_invite(710001)
        self.client.updates=[self.start(invitation['payload']),self.message('/team Надёжная команда')]
        original=self.worker.store.ingest
        calls=0
        def interrupt(event):
            nonlocal calls
            calls+=1
            if calls==2:raise ValueError('simulated storage capacity')
            return original(event)
        with patch.object(self.worker.store,'ingest',side_effect=interrupt):
            with self.assertRaises(ValueError):self.worker.poll_once()
        self.assertIsNone(self.worker.store.get_cursor())
        self.assertEqual(self.worker.store.safe_status()['events'],1)
        self.assertEqual(len(self.client.sends),0)
        self.worker.poll_once()
        self.assertEqual(self.worker.store.get_cursor(),7)
        self.assertEqual(self.worker.store.safe_status()['events'],2)
        self.assertEqual(len(self.worker.store.view(710001)['teams']),1)

    def test_webhook_refused_and_unknown_send_never_retried(self):
        self.client.subscriptions=[dict(url='https://example.org/hook')]
        with self.assertRaisesRegex(ValueError,'active_webhook'):self.worker.poll_once()
        self.assertEqual(self.client.sends,[])
        self.client.subscriptions=[]
        self.client.send_status='unknown'
        self.client.updates=[self.message('/start')]
        self.worker.poll_once()
        self.assertEqual(self.worker.store.safe_status()['unknown'],1)
        self.worker.poll_once()
        self.assertEqual(len(self.client.sends),1)
        restarted=Poller(self.settings,client=self.client)
        restarted.poll_once()
        self.assertEqual(len(self.client.sends),1)

    def test_local_invite_api_requires_worker_and_does_not_use_real_client(self):
        with TestClient(create_app(db_path=self.settings.db_path,max_settings=self.settings),base_url='http://127.0.0.1:5211') as web:
            request=dict(actor_id=710001)
            self.assertEqual(web.post('/api/max/invite',json=request,headers={'Origin':'http://127.0.0.1:5211'}).status_code,409)
            self.worker.store.heartbeat()
            response=web.post('/api/max/invite',json=request,headers={'Origin':'http://127.0.0.1:5211'})
            self.assertEqual(response.status_code,200)
            self.assertTrue(response.json()['url'].startswith('https://max.ru/test_bot?start='))
            safe=web.get('/api/max/status').json()
            self.assertTrue(safe['workerAlive'])
            self.assertNotIn('payload',json.dumps(safe))
            self.assertEqual(self.client.sends,[])

    def test_one_consumer_lock_and_cursor_never_decreases(self):
        with exclusive_worker(self.settings.db_path):
            with self.assertRaisesRegex(ValueError,'already_running'):
                with exclusive_worker(self.settings.db_path):pass
        self.worker.store.save_cursor(10)
        self.client.marker=9
        with self.assertRaisesRegex(ValueError,'backwards'):self.worker.poll_once()
        self.assertEqual(self.worker.store.get_cursor(),10)

    def test_permanent_input_rejection_does_not_poison_following_events(self):
        self.client.updates=[self.message('/start\r\n'),self.message('/start')]
        original=self.worker.store.ingest
        def reject_one(event):
            if event['text']=='/start\r\n':raise HTTPException(422,'permanent native validation')
            return original(event)
        with patch.object(self.worker.store,'ingest',side_effect=reject_one):
            result=self.worker.poll_once()
        self.assertEqual(result['skipped'],1)
        self.assertEqual(result['accepted'],1)
        self.assertEqual(self.worker.store.get_cursor(),7)
        self.assertEqual(len(self.client.sends),1)

if __name__=='__main__':unittest.main()
