"""Recover a staged MAX batch before a changing latest-only provider is polled.

The provider network and every identity are synthetic. The durable store,
parser, domain effects and worker are the actual implementation.
"""
from pathlib import Path
import json
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from app.max_settings import MaxSettings
from app.polling import Poller
from app.vendor.max_transport import TransportOutcome
from app.vendor.max_wire import normalize_update


def message(text):
    return {'update_type':'message_created','timestamp':int(time.time()*1000),
            'message':{'sender':{'user_id':1234},'recipient':{'chat_type':'dialog','chat_id':5678},
                       'body':{'mid':str(uuid4()),'text':text}}}


class ChangingLatestProvider:
    def __init__(self,batches):
        self.batches=list(batches)
        self.fetches=[]
        self.sent=[]

    def get_me(self):
        return TransportOutcome('accepted','synthetic_identity',200,data={'username':'batch_test_bot','isBot':True})

    def list_subscriptions(self):
        return TransportOutcome('accepted','synthetic_subscriptions',200,data={'subscriptions':[]})

    def get_updates(self,**kwargs):
        self.fetches.append(kwargs)
        if not self.batches:raise AssertionError('Unexpected provider fetch while staged recovery is pending')
        updates,marker=self.batches.pop(0)
        return TransportOutcome('accepted','synthetic_updates',200,data={'updates':updates,'marker':marker})

    def send_message(self,chat,text):
        self.sent.append((chat,text))
        return TransportOutcome('accepted','synthetic_send',200,provider_message_id='synthetic-'+str(uuid4()))

    def close(self):
        pass


class DurablePollingBatchRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(dir='/private/tmp',prefix='ifx-staged-max-test-')
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.root.chmod(0o700)
        self.settings=MaxSettings(True,'batch_test_bot',self.root/'never-read-token',None,self.root/'max.sqlite3')
        self.provider=ChangingLatestProvider([])
        self.worker=self.make_worker(self.provider)
        invitation=self.worker.store.issue_invite(710001)
        raw={'update_type':'bot_started','timestamp':int(time.time()*1000),'chat_id':5678,
             'user':{'user_id':1234},'payload':invitation['payload']}
        # Represent an already admitted tester before the first polling cursor.
        self.worker.store.ingest(normalize_update(json.dumps(raw).encode()).as_dict())
        first=self.worker.store.claim_next()
        self.worker.store.finish(first['id'],{'status':'accepted','code':'synthetic','provider_message_id':'already-admitted'})

    def make_worker(self,provider):
        worker=Poller(self.settings,client=provider)
        self.addCleanup(worker.close)
        return worker

    def assert_team(self,worker):
        teams=worker.store.view(710001)['teams']
        self.assertEqual(len(teams),1)
        self.assertEqual(teams[0]['name'],'Долговечная команда')
        return teams[0]

    def test_first_batch_latest_changes_but_retry_uses_staged_event_without_new_fetch(self):
        self.provider.batches=[([message('/team Долговечная команда')],7),
                               ([message('/idea Проверка | Вымышленное описание идеи.')],8)]
        with patch.object(self.worker.store,'ingest',side_effect=ValueError('Synthetic intake interruption')):
            with self.assertRaises(ValueError):self.worker.poll_once()
        self.assertIsNone(self.worker.store.get_cursor())
        self.assertEqual(len(self.provider.fetches),1)
        self.worker.poll_once()
        self.assertEqual(len(self.provider.fetches),1,'Recovery must not ask for a different latest update')
        self.assert_team(self.worker)
        self.assertEqual(self.worker.store.get_cursor(),7)
        self.worker.poll_once()
        self.assertEqual(len(self.provider.fetches),2)
        self.assertEqual(self.provider.fetches[-1]['marker'],7)
        self.assertEqual(self.assert_team(self.worker)['idea']['title'],'Проверка')
        self.assertEqual(self.worker.store.get_cursor(),8)

    def test_process_restart_recovers_first_batch_before_polling_new_latest(self):
        self.provider.batches=[([message('/team Долговечная команда')],7)]
        with patch.object(self.worker.store,'ingest',side_effect=ValueError('Synthetic worker interruption')):
            with self.assertRaises(ValueError):self.worker.poll_once()
        self.worker.close()
        # The remote latest update has advanced while the local worker was down.
        newer=ChangingLatestProvider([([message('/idea После перезапуска | Вымышленная идея.')],8)])
        restarted=self.make_worker(newer)
        restarted.poll_once()
        self.assertEqual(newer.fetches,[],'A restart must consume the durable batch before network polling')
        self.assert_team(restarted)
        self.assertEqual(restarted.store.get_cursor(),7)
        restarted.poll_once()
        self.assertEqual(len(newer.fetches),1)
        self.assertEqual(newer.fetches[0]['marker'],7)
        self.assertEqual(self.assert_team(restarted)['idea']['title'],'После перезапуска')
        self.assertEqual(restarted.store.get_cursor(),8)

    def test_committed_prefix_replays_once_and_unprocessed_tail_survives_restart(self):
        self.provider.batches=[([message('/team Долговечная команда'),message('/idea Хвост пакета | Вымышленная идея.')],9)]
        original=self.worker.store.ingest
        calls=[]
        def interrupted(event):
            calls.append(event['eventId'])
            if len(calls)==2:raise ValueError('Synthetic tail interruption')
            return original(event)
        with patch.object(self.worker.store,'ingest',side_effect=interrupted):
            with self.assertRaises(ValueError):self.worker.poll_once()
        self.assert_team(self.worker)
        self.assertIsNone(self.worker.store.get_cursor())
        self.worker.close()
        no_network=ChangingLatestProvider([])
        restarted=self.make_worker(no_network)
        restarted.poll_once()
        self.assertEqual(no_network.fetches,[])
        self.assertEqual(self.assert_team(restarted)['idea']['title'],'Хвост пакета')
        self.assertEqual(restarted.store.get_cursor(),9)
        # Binding plus each unique command once, despite prefix recovery.
        self.assertEqual(restarted.store.safe_status()['events'],3)


if __name__=='__main__':
    unittest.main()
