"""Synthetic storage and HTTP failure boundaries, without provider network."""
from contextlib import ExitStack
from pathlib import Path
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx

from app.main import create_app
from app.store import Store
from app.transport import SyntheticMax


ORIGIN = 'http://127.0.0.1:5211'
ACTOR = 710001


class StoreBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(
            dir='/private/tmp', prefix='ideyafix-store-boundary.')))
        for name in ('socket.socket.connect', 'socket.create_connection', 'socket.getaddrinfo'):
            self.stack.enter_context(patch(name, side_effect=AssertionError('Synthetic test forbids network')))

    def open_app(self):
        self.path = self.root / 'demo.sqlite3'
        self.transport = SyntheticMax()
        self.stack.callback(self.transport.close)
        self.client = self.stack.enter_context(TestClient(
            create_app(db_path=self.path, transport=self.transport),
            base_url=ORIGIN, raise_server_exceptions=False))

    def state(self):
        response = self.client.get('/api/demo/state', params={'actor_id': ACTOR})
        self.assertEqual(response.status_code, 200)
        return response.json()

    def post(self, event_id):
        return self.client.post('/api/demo/events', json={
            'actor_id': ACTOR, 'text': '/team Команда атомарности', 'event_id': event_id,
        }, headers={'Origin': ORIGIN})

    def stored_counts(self):
        with sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True) as database:
            return tuple(database.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                         for table in ('receipts', 'messages'))

    def fingerprint(self, path):
        info = path.stat()
        return hashlib.sha256(path.read_bytes()).hexdigest(), info.st_mode, info.st_nlink

    def test_symlink_database_and_symlink_parent_are_rejected_without_target_changes(self):
        target = self.root / 'owned-synthetic.sqlite3'
        Store(target)
        link = self.root / 'linked.sqlite3'
        link.symlink_to(target)
        directory = self.root / 'directory-link'
        directory.symlink_to(self.root, target_is_directory=True)
        before = self.fingerprint(target), set(self.root.iterdir())
        for path in (link, directory / target.name):
            with self.subTest(path=path.name):
                with self.assertRaises(ValueError):
                    Store(path)
                self.assertEqual((self.fingerprint(target), set(self.root.iterdir())), before)

    def test_hardlink_database_is_rejected_without_chmod_or_schema_changes(self):
        target = self.root / 'owned-synthetic.sqlite3'
        Store(target)
        link = self.root / 'hardlinked.sqlite3'
        os.link(target, link)
        target.chmod(0o640)
        before = self.fingerprint(target), set(self.root.iterdir())
        for path in (target, link):
            with self.subTest(path=path.name):
                with self.assertRaises(ValueError):
                    Store(path)
                self.assertEqual((self.fingerprint(target), set(self.root.iterdir())), before)

    def test_failed_actual_mock_boundary_rolls_back_then_same_event_can_succeed_once(self):
        self.open_app()
        identifier = str(uuid4())
        before = self.state()
        before_file = self.path.read_bytes()
        with patch.object(httpx.MockTransport, 'handle_request',
                          side_effect=httpx.ConnectError('SYNTHETIC FAILURE')) as boundary:
            response = self.post(identifier)
            self.assertEqual(response.status_code, 500)
            self.assertEqual(boundary.call_count, 1)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.path.read_bytes(), before_file)
        self.assertEqual(self.stored_counts(), (0, 0))
        self.assertEqual(self.transport.call_count, 0)
        accepted = self.post(identifier)
        self.assertEqual(accepted.status_code, 200)
        self.assertFalse(accepted.json()['duplicate'])
        self.assertEqual(self.stored_counts(), (1, 2))
        self.assertEqual(self.transport.call_count, 1)
        replay = self.post(identifier)
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json()['duplicate'])
        self.assertEqual(self.stored_counts(), (1, 2))
        self.assertEqual(self.transport.call_count, 1)

    def test_rejected_simulator_receipt_rolls_back_without_persisted_command(self):
        self.open_app()
        identifier = str(uuid4())
        before = self.state()
        with patch.object(self.transport, 'deliver', return_value={'accepted': False}) as deliver:
            response = self.post(identifier)
            self.assertEqual(response.status_code, 503)
            self.assertEqual(deliver.call_count, 1)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.stored_counts(), (0, 0))
        self.assertEqual(self.transport.call_count, 0)
        self.assertEqual(self.post(identifier).status_code, 200)
        self.assertEqual(self.stored_counts(), (1, 2))

    def test_malformed_json_utf8_and_strict_types_do_not_mutate_or_send(self):
        self.open_app()
        before = self.state()
        for body, status in ((b'{', 422), (b'\xff', 400), (b'[]', 422), (b'null', 422)):
            with self.subTest(body_length=len(body), expected_status=status):
                response = self.client.post('/api/demo/events', content=body,
                    headers={'Origin': ORIGIN, 'Content-Type': 'application/json'})
                self.assertEqual(response.status_code, status)
        for changes in ({'actor_id': True}, {'actor_id': str(ACTOR)}, {'text': 123},
                        {'event_id': str(uuid4()).upper()}, {'unknown': 'field'}):
            value = {'actor_id': ACTOR, 'text': '/start', 'event_id': str(uuid4()), **changes}
            response = self.client.post('/api/demo/events', content=json.dumps(value),
                headers={'Origin': ORIGIN, 'Content-Type': 'application/json'})
            self.assertEqual(response.status_code, 422)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.stored_counts(), (0, 0))
        self.assertEqual(self.transport.call_count, 0)


if __name__ == '__main__':
    unittest.main()
