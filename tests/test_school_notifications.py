import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx

from app.max_settings import MaxSettings
from app.polling import Poller
from app.school_demo_links import SCHOOL_STAFF_URL
from app.school_notification_client import (
    WORKER_URL, SchoolNotificationClient, SchoolNotificationsUnavailable,
)
from tests.test_platform_integration import LocalMax
from tests.test_platform_store import FakeAssistant


TOKEN = 'sdn_' + 'b' * 32
CLAIM = 'c' * 32
SCHOOL_CREDENTIAL = 'SYNTHETIC-SCHOOL-WORKER-CREDENTIAL'


def start(payload):
    return {'update_type': 'bot_started', 'timestamp': int(time.time() * 1000), 'chat_id': 456,
            'user': {'user_id': 123}, 'payload': payload}


def message(text, *, chat_type='dialog'):
    return {'update_type': 'message_created', 'timestamp': int(time.time() * 1000),
            'message': {'sender': {'user_id': 123}, 'recipient': {'chat_type': chat_type, 'chat_id': 456},
                        'body': {'mid': str(uuid4()), 'text': text}}}


class Assistant(FakeAssistant):
    def close(self):
        pass


class SchoolNotificationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir='/private/tmp', prefix='ifx-school-notifications.')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.token_file = self.root / 'worker-token'
        self.token_file.write_text(SCHOOL_CREDENTIAL + '\n')
        self.token_file.chmod(0o600)
        self.requests = []
        self.replies = []
        self.provider = LocalMax()
        self.assistant = Assistant()
        self.settings = MaxSettings(
            True, 'synthetic_bot', self.root / 'unused-max-token', None, self.root / 'max.sqlite3',
            platform_enabled=True, school_notifications_token_file=self.token_file,
        )
        sleeper = patch('time.sleep')
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def school_client(self):
        def request_handler(request):
            self.assertEqual(str(request.url), WORKER_URL)
            self.assertEqual(request.method, 'POST')
            self.assertEqual(request.headers.get('X-Taskflow-Max-Worker'), SCHOOL_CREDENTIAL)
            payload = json.loads(request.content)
            self.assertNotIn(SCHOOL_CREDENTIAL.encode(), request.content)
            self.requests.append(payload)
            if not self.replies:
                self.assertEqual(payload, {'action': 'claim'})
                return httpx.Response(200, json={'item': None})
            action, response = self.replies.pop(0)
            self.assertEqual(payload['action'], action)
            return response

        client = SchoolNotificationClient(self.token_file, transport=httpx.MockTransport(request_handler))
        self.addCleanup(client.close)
        return client

    def worker(self):
        result = Poller(self.settings, client=self.provider, platform_assistant=self.assistant,
                        school_client=self.school_client())
        self.addCleanup(result.close)
        return result

    def test_settings_enable_fixed_school_worker_only_in_platform_mode(self):
        path = self.root / 'config.json'
        value = {'enabled': True, 'botUsername': 'synthetic_bot', 'tokenFile': str(self.settings.token_file),
                 'caFile': None, 'dbPath': str(self.settings.db_path), 'activatedAtMs': 0,
                 'platformEnabled': True, 'schoolNotificationsTokenFile': str(self.token_file)}
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        self.assertEqual(MaxSettings.load(path).school_notifications_token_file, self.token_file)
        for patch_value in (
            {'schoolNotificationsTokenFile': 'relative'}, {'platformEnabled': False},
            {'schoolNotificationsTokenFile': str(self.root / 'nested' / '..' / self.settings.token_file.name)},
        ):
            with self.subTest(patch_value=patch_value):
                path.write_text(json.dumps({**value, **patch_value}))
                with self.assertRaisesRegex(ValueError, '^invalid_private_max_config$'):
                    MaxSettings.load(path)

    def test_bad_credential_and_remote_responses_fail_closed(self):
        self.token_file.chmod(0o644)
        with self.assertRaisesRegex(ValueError, '^invalid_max_token_file$'):
            self.school_client()
        self.token_file.chmod(0o600)
        client = self.school_client()
        self.replies.extend([
            ('claim', httpx.Response(302, headers={'Location': 'https://example.invalid/'})),
            ('claim', httpx.Response(200, json={'item': {'claimId': CLAIM, 'chatId': '1',
                                                        'text': SCHOOL_CREDENTIAL}})),
            ('link', httpx.Response(200, json={'linked': 'true'})),
        ])
        for action in (client.claim, client.claim, lambda: client.link(TOKEN, '1', '2')):
            with self.assertRaisesRegex(SchoolNotificationsUnavailable, '^school_notifications_unavailable$'):
                action()
        self.assertEqual(len(self.requests), 3)

    def test_optional_credential_unavailable_does_not_stop_the_max_worker(self):
        self.token_file.chmod(0o644)
        worker = Poller(self.settings, client=self.provider, platform_assistant=self.assistant)
        self.addCleanup(worker.close)
        self.provider.updates = [message('/help'), start(TOKEN)]
        with patch('socket.socket.connect', side_effect=AssertionError('Unexpected network')):
            result = worker.poll_once()
            self.assertEqual(result['accepted'], 2)
            self.assertEqual(worker.store.get_cursor(), 1)
            worker.poll_once()
        self.assertIn('Откройте школу', self.provider.sent[0]['text'])
        self.assertIn('временно недоступна', self.provider.sent[1]['text'])
        self.assertIsNone(worker.school_client)

    def test_transient_link_survives_restart_without_blocking_other_max_events(self):
        worker = self.worker()
        linked_update = start(TOKEN)
        self.provider.updates = [linked_update, message('/help')]
        self.replies.append(('link', httpx.Response(503)))
        result = worker.poll_once()
        self.assertEqual(result['accepted'], 2)
        self.assertEqual(worker.store.get_cursor(), 1)
        self.assertIsNone(worker.store.pending_batch())
        self.assertIn('Откройте школу', self.provider.sent[-1]['text'])
        self.assertEqual(self.requests, [{'action': 'link', 'token': TOKEN, 'chatId': '456', 'senderId': '123'}])
        worker.poll_once()
        self.assertIn('временно недоступна', self.provider.sent[-1]['text'])
        self.assertEqual(len(self.requests), 1)
        worker.close()

        self.replies.append(('link', httpx.Response(200, json={'linked': True})))
        restarted = self.worker()
        self.provider.updates = [linked_update]
        with patch('app.platform_store.stamp', return_value=int(time.time() * 1000) + 11000):
            restarted.poll_once()
        restarted.poll_once()
        links = [request for request in self.requests if request['action'] == 'link']
        self.assertEqual(links, [self.requests[0], self.requests[0]])
        self.assertIn('подключены к этому чату', self.provider.sent[-1]['text'])
        self.assertEqual(self.provider.sent[-1]['buttons'], [{'label': 'Открыть школу', 'url': SCHOOL_STAFF_URL}])
        self.assertEqual(restarted.store.safe_status()['bindings'], 0)
        self.assertNotIn(TOKEN, str(self.provider.sent))

    def test_typed_private_start_gets_invalid_reply_and_other_invites_still_work(self):
        worker = self.worker()
        invite = worker.store.issue_invite(710007)
        self.provider.updates = [message('/start ' + TOKEN, chat_type='chat'),
                                 message('/start ' + TOKEN), start(invite['payload'])]
        self.replies.append(('link', httpx.Response(200, json={'linked': False})))
        result = worker.poll_once()
        self.assertEqual((result['accepted'], result['skipped']), (2, 1))
        worker.poll_once()
        self.assertEqual(self.assistant.calls, [(710007, '/start')])
        self.assertEqual(worker.store.safe_status()['bindings'], 1)
        self.assertEqual(len([request for request in self.requests if request['action'] == 'link']), 1)
        self.assertIn('Ссылка недействительна', self.provider.sent[-1]['text'])

    def test_stop_cancels_pending_link_without_cancelling_a_new_start_on_replay(self):
        worker = self.worker()
        first = start(TOKEN)
        stop = {key: value for key, value in first.items() if key != 'payload'}
        stop.update(update_type='bot_stopped', timestamp=first['timestamp'] + 1)
        self.provider.updates = [first, stop]
        worker.poll_once()
        self.assertEqual(self.requests, [{'action': 'claim'}])
        self.assertIsNone(worker.store.next_school_link())
        self.assertEqual(self.provider.sent, [])

        second = {**first, 'timestamp': stop['timestamp'] + 1}
        self.provider.updates = [second, stop]
        self.replies.append(('link', httpx.Response(200, json={'linked': True})))
        worker.poll_once()
        worker.poll_once()
        self.assertEqual(self.requests[-1]['action'], 'link')
        self.assertIn('подключены к этому чату', self.provider.sent[-1]['text'])

    def test_lost_finish_replays_receipt_without_sending_notice_twice(self):
        notice = {'claimId': CLAIM, 'chatId': '456', 'text': 'Расписание обновлено.'}
        claim_response = httpx.Response(200, json={'item': notice})
        self.replies.extend([('claim', claim_response), ('finish', httpx.Response(503))])
        worker = self.worker()
        worker.poll_once()
        self.assertEqual(len(self.provider.sent), 1)
        self.assertEqual(self.provider.sent[0], {
            'chat': 456, 'text': notice['text'], 'buttons': [{'label': 'Открыть школу', 'url': SCHOOL_STAFF_URL}],
        })
        worker.close()
        restarted = self.worker()
        self.replies.extend([
            ('finish', httpx.Response(200, json={'ok': True})),
            ('claim', httpx.Response(200, json={'item': notice})),
        ])
        restarted.poll_once()
        restarted.poll_once()
        self.assertEqual(len(self.provider.sent), 1)
        receipt = {'action': 'finish', 'claimId': CLAIM, 'status': 'accepted'}
        self.assertEqual(self.requests, [{'action': 'claim'}, receipt, receipt, {'action': 'claim'}])

    def test_incomplete_send_is_reported_unknown_without_resending(self):
        worker = self.worker()
        self.assertTrue(worker.store.start_school_delivery(CLAIM))
        worker.close()
        restarted = self.worker()
        self.replies.append(('finish', httpx.Response(200, json={'ok': False})))
        restarted.poll_once()
        self.assertEqual(self.provider.sent, [])
        self.assertEqual(self.requests, [{'action': 'finish', 'claimId': CLAIM, 'status': 'unknown'}])
        self.assertIsNone(restarted.store.pending_school_receipt())
