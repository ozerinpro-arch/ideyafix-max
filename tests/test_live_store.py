"""Private live-store contracts using native MAX shapes and synthetic IDs only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException

from app.live_store import LiveStore
from app.store import Store
from app.vendor.max_wire import normalize_update


class LiveStoreTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir='/private/tmp', prefix='ideyafix-live-store.')))
        self.path = self.root / 'demo.sqlite3'
        for name in ('socket.socket.connect', 'socket.create_connection', 'socket.getaddrinfo'):
            self.stack.enter_context(patch(name, side_effect=AssertionError('All live-store test network forbidden')))
        self.store = LiveStore(self.path)

    def native(self, kind='message_created', *, sender=900101, chat=900201, text='/start', payload=None):
        timestamp = int(time.time() * 1000)
        if kind == 'message_created':
            value = dict(update_type=kind, timestamp=timestamp, message=dict(
                sender={'user_id': sender, 'name': 'SYNTHETIC_REAL_NAME_TO_IGNORE'},
                recipient={'type': 'dialog', 'chat_id': chat}, timestamp=timestamp,
                body={'mid': str(uuid4()), 'text': text}))
        else:
            value = dict(update_type=kind, timestamp=timestamp, chat_id=chat,
                         user={'user_id': sender, 'name': 'SYNTHETIC_REAL_NAME_TO_IGNORE'})
            if payload is not None: value['payload'] = payload
        return normalize_update(json.dumps(value).encode()).as_dict()

    def accepted(self):
        item = self.store.claim_next()
        self.assertIsNotNone(item)
        self.store.finish(item['id'], dict(status='accepted', provider_message_id='synthetic-' + item['id'], code='sent'))
        return item

    def bind(self, actor=710001, *, sender=900101, chat=900201):
        invite = self.store.issue_invite(actor)
        result = self.store.ingest(self.native('bot_started', sender=sender, chat=chat, payload=invite['payload']))
        self.assertEqual(result, dict(duplicate=False, queued=1))
        self.accepted()
        return invite

    def test_invite_native_bind_and_command_update_same_demo_and_redact_capability(self):
        invite = self.bind()
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(invite['payload'].encode(), self.path.read_bytes())
        self.assertNotIn(b'SYNTHETIC_REAL_NAME_TO_IGNORE', self.path.read_bytes())
        event = self.native(text='/team Живая тестовая команда')
        self.assertEqual(self.store.ingest(event), dict(duplicate=False, queued=1))
        # The regular UI store observes the identical committed demo state.
        view = Store(self.path).view(710001)
        self.assertEqual([t['name'] for t in view['teams']], ['Живая тестовая команда'])
        reply = self.store.claim_next()
        self.assertEqual(reply['chat_id'], '900201')
        self.assertIn('Живая тестовая команда', reply['text'])
        self.assertLessEqual(len(reply['text']), 4000)
        self.assertEqual(set(reply), {'id', 'chat_id', 'text'})

    def test_unknown_sender_cannot_change_domain_or_select_role(self):
        before = self.store.view(710001)
        for text in ('/team Чужая команда', '/role лера', '/start'):
            self.store.ingest(self.native(text=text))
            item = self.accepted()
            self.assertIn('одноразов', item['text'])
            self.assertEqual(self.store.view(710001), before)
        self.assertEqual(self.store.safe_status()['bindings'], 0)

    def test_expired_and_used_invites_do_not_authorize_another_sender(self):
        invite = self.bind()
        self.store.ingest(self.native('bot_started', sender=900102, chat=900202, payload=invite['payload']))
        self.assertIn('одноразов', self.accepted()['text'])
        expires = self.store.issue_invite(710002)
        with patch('app.live_store._now_ms', return_value=expires['expiresAt']):
            self.store.ingest(self.native('bot_started', sender=900103, chat=900203, payload=expires['payload']))
        self.assertIn('одноразов', self.accepted()['text'])
        self.assertEqual(self.store.safe_status()['bindings'], 1)

    def test_existing_bound_start_without_payload_shows_current_role(self):
        self.bind(710004)
        self.store.ingest(self.native('bot_started'))
        reply = self.accepted()['text']
        self.assertIn('Саша Демо', reply)
        self.assertNotIn('Без неё команды', reply)
        self.assertEqual(self.store.safe_status()['bindings'], 1)

    def test_native_crlf_command_commits_and_queues_normal_reply(self):
        self.bind()
        event = self.native(text='/start\r\n')
        self.assertEqual(self.store.ingest(event), dict(duplicate=False, queued=1))
        reply = self.accepted()['text']
        self.assertIn('/hackathon', reply)
        self.assertIn('/education', reply)
        self.assertIn('настоящий MAX', reply)
        self.assertNotIn('не отправляются', reply)
        self.assertEqual(self.store.ingest(event), dict(duplicate=True, queued=0))

    def test_live_help_describes_real_transport_and_fictional_roles(self):
        self.bind()
        self.store.ingest(self.native(text='/help'))
        reply = self.accepted()['text']
        self.assertIn('настоящий MAX', reply)
        self.assertIn('данные тестовые', reply)
        self.assertIn('/role', reply)
        self.assertNotIn('не отправляются', reply)

    def test_replay_has_one_action_transcript_and_outbox_conflict_is_409(self):
        self.bind()
        event = self.native(text='/team Единственная команда')
        self.store.ingest(event)
        view, counts = self.store.view(710001), self.store.safe_status()
        self.assertEqual(self.store.ingest(event), dict(duplicate=True, queued=0))
        self.assertEqual(self.store.view(710001), view)
        self.assertEqual(self.store.safe_status(), counts)
        bad = deepcopy(event); bad['payloadHash'] = 'f' * 64
        with self.assertRaises(HTTPException) as error: self.store.ingest(bad)
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(self.store.view(710001), view)

    def test_concurrent_native_replay_commits_exactly_one_new_event(self):
        self.bind()
        event = self.native(text='/team Параллельная команда')
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(lambda _: self.store.ingest(event), range(5)))
        self.assertEqual(sum(r['queued'] for r in results), 1)
        self.assertEqual(sum(r['duplicate'] for r in results), 4)
        self.assertEqual(len(self.store.view(710001)['teams']), 1)
        self.assertEqual(self.store.safe_status()['queued'], 1)

    def test_role_selection_is_per_native_sender_and_fixed_demo_roles_only(self):
        self.bind()
        self.bind(710002, sender=900102, chat=900202)
        self.store.ingest(self.native(text='/role марина')); self.accepted()
        self.store.ingest(self.native(text='/task Из MAX | Только вымышленное задание')); self.accepted()
        self.assertTrue(any(t['title'] == 'Из MAX' for t in self.store.view(710004)['assignments']))
        self.store.ingest(self.native(sender=900102, chat=900202, text='/task Не разрешено | Это участник'))
        self.assertIn('преподаватель', self.accepted()['text'])
        before = self.store.view(710004)['assignments']
        self.store.ingest(self.native(text='/role 999999')); self.accepted()
        self.store.ingest(self.native(text='/role'))
        self.assertIn('Марина Демо', self.accepted()['text'])
        self.assertEqual(self.store.view(710004)['assignments'], before)

    def test_native_education_complete_revision_cycle(self):
        self.bind(710004)
        for text in ('/submit Первая тестовая попытка', '/role марина',
                     '/review доработать | Добавь вывод', '/role саша',
                     '/submit Вторая попытка с выводом', '/role марина',
                     '/review принять | Проверено на вымышленных данных'):
            self.store.ingest(self.native(text=text)); self.accepted()
        task = self.store.view(710004)['assignments'][0]
        self.assertEqual(task['state'], 'accepted')
        self.assertEqual(task['submission']['attempt'], 2)
        self.assertEqual(task['review']['decision'], 'accepted')

    def test_wrong_dialog_cannot_use_existing_sender_binding(self):
        self.bind()
        self.store.ingest(self.native(chat=900999, text='/team Не та переписка'))
        self.assertIn('одноразов', self.accepted()['text'])
        self.assertEqual(self.store.view(710001)['teams'], [])
        self.assertEqual(self.store.safe_status()['bindings'], 1)

    def test_stopped_revokes_binding_and_rejects_queued_reply_without_new_send(self):
        self.bind()
        self.store.ingest(self.native(text='/start'))
        self.assertEqual(self.store.safe_status()['queued'], 1)
        stopped = self.native('bot_stopped')
        self.assertEqual(self.store.ingest(stopped), dict(duplicate=False, queued=0))
        self.assertEqual(self.store.safe_status()['bindings'], 0)
        self.assertIsNone(self.store.claim_next())
        self.assertEqual(self.store.safe_status()['rejected'], 1)
        self.store.ingest(self.native(text='/role лера'))
        self.assertIn('одноразов', self.accepted()['text'])

    def test_restart_preserves_domain_cursor_and_never_retries_started_unknown(self):
        self.bind()
        event = self.native(text='/team Сохранённая команда')
        self.store.ingest(event)
        item = self.store.claim_next()
        self.store.save_cursor(12345)
        restarted = LiveStore(self.path)
        self.assertEqual(restarted.get_cursor(), 12345)
        self.assertEqual(restarted.safe_status()['started'], 1)
        self.assertEqual(restarted.recover(), {'unknown': 1})
        self.assertEqual(restarted.recover(), {'unknown': 0})
        self.assertIsNone(restarted.claim_next())
        self.assertEqual(restarted.ingest(event), dict(duplicate=True, queued=0))
        self.assertEqual(restarted.safe_status()['unknown'], 1)
        self.assertEqual(len(restarted.view(710001)['teams']), 1)
        self.assertEqual(restarted.finish(item['id'], dict(status='accepted', provider_message_id='late', code='late')), {'state': 'unknown'})

    def test_finish_terminal_receipts_are_idempotent_and_not_requeued(self):
        self.bind()
        self.store.ingest(self.native(text='/start'))
        item = self.store.claim_next()
        result = self.store.finish(item['id'], dict(status='rejected', provider_message_id=None, code='native_rejected'))
        self.assertEqual(result, {'state': 'rejected'})
        self.assertEqual(self.store.finish(item['id'], dict(status='unknown', code='late')), result)
        self.assertIsNone(self.store.claim_next())

    def test_claim_is_atomic_across_workers(self):
        self.bind()
        self.store.ingest(self.native(text='/start'))
        with ThreadPoolExecutor(max_workers=5) as pool:
            rows = list(pool.map(lambda _: self.store.claim_next(), range(5)))
        self.assertEqual(sum(row is not None for row in rows), 1)
        self.assertEqual(self.store.safe_status()['started'], 1)

    def test_status_counts_cursor_validation_and_heartbeat_have_no_private_ids(self):
        self.assertIsNone(self.store.get_cursor())
        self.assertFalse(self.store.worker_alive())
        with patch('app.live_store.time.time', return_value=1000):
            self.store.heartbeat(); self.assertTrue(self.store.worker_alive())
        with patch('app.live_store.time.time', return_value=1041): self.assertFalse(self.store.worker_alive())
        self.store.save_cursor(5)
        for bad in (True, -1, 2 ** 63, '10'):
            with self.assertRaises(HTTPException): self.store.save_cursor(bad)
        with self.assertRaises(HTTPException): self.store.save_cursor(4)
        self.bind()
        status = self.store.safe_status()
        self.assertTrue(all(type(value) is int for value in status.values()))
        self.assertNotIn('900101', json.dumps(status))
        self.assertNotIn('900201', json.dumps(status))

    def test_malformed_or_nonprivate_normalized_events_are_refused_before_mutation(self):
        event = self.native()
        before = self.store.safe_status(), self.store.view(710001)
        for changes in ({'isPrivate': False}, {'senderId': 'arbitrary'}, {'chatId': []},
                        {'eventType': ['bot_started']}, {'timestamp': True}, {'extra': 'untrusted'},
                        {'payloadHash': 'bad'}, {'eventType': 'message_callback'}, {'text': '\u0000'}):
            with self.subTest(changes=changes), self.assertRaises(HTTPException) as error:
                self.store.ingest({**event, **changes})
            self.assertEqual(error.exception.status_code, 422)
        self.assertEqual((self.store.safe_status(), self.store.view(710001)), before)


if __name__ == '__main__':
    unittest.main()
