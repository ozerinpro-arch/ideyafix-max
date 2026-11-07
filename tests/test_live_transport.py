"""Only temporary synthetic token files and MockTransport provider responses."""
from contextlib import ExitStack
from pathlib import Path
import json
import os
import ssl
import tempfile
import unittest
from unittest.mock import patch

import certifi
import httpx

from app.live_transport import LiveMaxClient
from app.vendor.max_transport import MaxClient


class LiveTransportTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(
            dir='/private/tmp', prefix='ideyafix-live-client-test.')))
        self.token = 'SYNTHETIC-LIVE-CLIENT-TEST-NOT-A-CREDENTIAL'
        self.token_file = self.root / 'synthetic-token'
        self.token_file.write_text(self.token + '\n', encoding='ascii')
        self.token_file.chmod(0o600)
        for target in ('socket.socket.connect', 'socket.create_connection', 'socket.getaddrinfo'):
            self.stack.enter_context(patch(target, side_effect=AssertionError('Tests forbid real network')))

    def client(self, handler):
        client = LiveMaxClient(self.token_file, transport=httpx.MockTransport(handler))
        self.stack.callback(client.close)
        return client

    def test_native_operations_fixed_https_origin_and_default_poll_types(self):
        observed = []

        def respond(request):
            self.assertEqual((request.url.scheme, request.url.host, request.url.port),
                             ('https', 'platform-api2.max.ru', None))
            self.assertEqual(request.headers['Authorization'], self.token)
            self.assertNotIn(self.token, str(request.url))
            observed.append((request.method, request.url.path, dict(request.url.params)))
            if request.url.path == '/me':
                return httpx.Response(200, json={'user_id': 980001, 'is_bot': True, 'username': 'synthetic_demo_bot'})
            if request.url.path == '/subscriptions':
                return httpx.Response(200, json={'subscriptions': []})
            if request.url.path == '/updates':
                return httpx.Response(200, json={'updates': [], 'marker': 41})
            self.assertEqual(json.loads(request.content), {'text': 'Тестовый ответ'})
            return httpx.Response(200, json={'message': {
                'body': {'mid': 'synthetic-provider-mid'},
                'recipient': {'type': 'dialog', 'chat_id': 980002},
            }})

        client = self.client(respond)
        self.assertEqual(client.get_me().status, 'accepted')
        self.assertEqual(client.list_subscriptions().data, {'subscriptions': []})
        self.assertEqual(client.get_updates(marker=40).data, {'updates': [], 'marker': 41})
        sent = client.send_message(980002, 'Тестовый ответ')
        self.assertEqual((sent.status, sent.provider_message_id), ('accepted', 'synthetic-provider-mid'))
        self.assertEqual(observed, [
            ('GET', '/me', {}), ('GET', '/subscriptions', {}),
            ('GET', '/updates', {'limit': '20', 'timeout': '5',
                                 'types': 'bot_started,bot_stopped,message_created', 'marker': '40'}),
            ('POST', '/messages', {'chat_id': '980002'}),
        ])

    def test_webhooks_direct_user_send_and_arbitrary_paths_never_reach_transport(self):
        requests = []
        client = self.client(lambda request: requests.append(request))
        results = [client.register_subscription('https://synthetic.example/cb', 'x' * 32),
                   client.send_text(980002, 'Не разрешено'),
                   client._call('DELETE', '/subscriptions'),
                   client._call('POST', '/subscriptions', body={}),
                   client._call('GET', 'https://foreign.example/me'),
                   client._call('GET', '//foreign.example/me'),
                   client._call('POST', '/messages', params={'user_id': 980002}, body={'text': 'Нельзя'})]
        self.assertTrue(all(result.status == 'rejected' and result.code == 'invalid_operation' for result in results))
        self.assertEqual(requests, [])

    def test_live_constructor_explicitly_verifies_tls_and_disables_proxy_retry_redirect(self):
        real_transport, real_client = httpx.HTTPTransport, httpx.Client
        with patch.dict(os.environ, {'HTTPS_PROXY': 'http://127.0.0.1:9', 'SSL_CERT_FILE': '/nonexistent-env-ca'}), \
                patch('app.live_transport.httpx.HTTPTransport', wraps=real_transport) as boundary, \
                patch('app.live_transport.httpx.Client', wraps=real_client) as factory:
            client = LiveMaxClient(self.token_file)
            self.stack.callback(client.close)
        transport_args = boundary.call_args.kwargs
        client_args = factory.call_args.kwargs
        context = transport_args['verify']
        self.assertIsInstance(context, ssl.SSLContext)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertIs(client_args['verify'], context)
        self.assertEqual(transport_args['retries'], 0)
        self.assertFalse(transport_args['trust_env'])
        self.assertFalse(client_args['trust_env'])
        self.assertFalse(client_args['follow_redirects'])
        self.assertEqual(client_args['timeout'].read, 10)
        self.assertEqual(client_args['timeout'].connect, 5)

    def test_scoped_additional_ca_and_invalid_ca_fail_closed(self):
        ca = self.root / 'synthetic-trust-bundle.pem'
        ca.write_bytes(Path(certifi.where()).read_bytes())
        ca.chmod(0o600)
        client = LiveMaxClient(self.token_file, ca, transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={'subscriptions': []})))
        self.stack.callback(client.close)
        self.assertEqual(client.list_subscriptions().status, 'accepted')
        ca.chmod(0o666)
        with self.assertRaisesRegex(ValueError, '^invalid_additional_ca_file$'):
            LiveMaxClient(self.token_file, ca, transport=httpx.MockTransport(lambda _: None))

    def test_private_token_file_permissions_size_format_and_links(self):
        directory = self.root / 'directory'
        directory.mkdir()
        missing = self.root / 'missing'
        link = self.root / 'symbolic-token'
        link.symlink_to(self.token_file)
        parent = self.root / 'symbolic-parent'
        parent.symlink_to(self.root, target_is_directory=True)
        for path in (missing, directory, link, parent / self.token_file.name, Path('relative-token')):
            with self.subTest(case=path.name):
                with self.assertRaisesRegex(ValueError, '^invalid_max_token_file$'):
                    LiveMaxClient(path, transport=httpx.MockTransport(lambda _: None))
        for body, mode in ((b'', 0o600), (b'x' * 8192, 0o600), (b'x' * 4097, 0o600),
                           (b'first\nsecond', 0o600), (b'\xff', 0o600), (b'test', 0o640),
                           (b'test', 0o400), (b'test', 0o700)):
            self.token_file.chmod(0o600)
            self.token_file.write_bytes(body)
            self.token_file.chmod(mode)
            with self.subTest(length=len(body), mode=mode):
                with self.assertRaisesRegex(ValueError, '^invalid_max_token_file$'):
                    LiveMaxClient(self.token_file, transport=httpx.MockTransport(lambda _: None))
        self.token_file.chmod(0o600)
        self.token_file.write_text(self.token)
        hardlink = self.root / 'hard-token'
        os.link(self.token_file, hardlink)
        with self.assertRaisesRegex(ValueError, '^invalid_max_token_file$'):
            LiveMaxClient(hardlink, transport=httpx.MockTransport(lambda _: None))
        hardlink.unlink()
        with patch('app.live_transport.os.getuid', return_value=os.getuid() + 1):
            with self.assertRaisesRegex(ValueError, '^invalid_max_token_file$'):
                LiveMaxClient(self.token_file, transport=httpx.MockTransport(lambda _: None))

    def test_invalid_injection_does_not_weaken_synthetic_client_guard(self):
        with self.assertRaisesRegex(ValueError, '^invalid_test_transport$'):
            LiveMaxClient(self.token_file, transport=object())
        with self.assertRaisesRegex(ValueError, '^synthetic_transport_required$'):
            MaxClient(self.token)

    def test_timeout_redirect_and_credential_reflection_are_safe_and_not_retried(self):
        for case in ('timeout', 'redirect', 'reflection'):
            observed = []

            def respond(request):
                observed.append(request.url.path)
                if case == 'timeout':
                    raise httpx.ReadTimeout('SYNTHETIC ERROR ' + self.token)
                if case == 'redirect':
                    return httpx.Response(302, headers={'Location': 'https://foreign.example/'})
                return httpx.Response(200, json={'message': {'body': {'mid': self.token}}})

            with self.subTest(case=case):
                client = self.client(respond)
                result = client.send_message(980002, 'Синтетический ответ')
                self.assertEqual(result.status, 'unknown')
                self.assertEqual(observed, ['/messages'])
                self.assertNotIn(self.token, repr(client) + json.dumps(result.as_dict()))
                self.assertNotIn('SYNTHETIC ERROR', json.dumps(result.as_dict()))

    def test_input_and_native_response_validation_are_retained(self):
        requests = []

        def respond(request):
            requests.append(request.url.path)
            return httpx.Response(200, content=b'{"updates":[],"marker":1,"marker":2}')

        client = self.client(respond)
        self.assertEqual(client.get_updates(marker=True).status, 'rejected')
        self.assertEqual(client.get_updates(update_types=('not_native',)).status, 'rejected')
        self.assertEqual(client.send_message(980002, 'x' * 4001).status, 'rejected')
        self.assertEqual(requests, [])
        result = client.get_updates(marker=0)
        self.assertEqual((result.status, result.code), ('unknown', 'transport_or_response_error'))
        self.assertEqual(requests, ['/updates'])

    def test_official_and_legacy_receipt_types_nullable_address_and_conflicts(self):
        accepted = [
            {'chat_type': 'dialog', 'chat_id': 980002, 'user_id': None},
            {'chat_type': 'dialog', 'chat_id': None, 'user_id': 980003},
            {'chat_type': 'dialog', 'user_id': None},
            {'type': 'dialog', 'chat_id': 980002},
            {'chat_type': 'dialog', 'type': 'dialog', 'chat_id': 980002},
        ]
        rejected = [
            {'chat_type': 'chat', 'chat_id': 980002},
            {'chat_type': 'channel', 'chat_id': 980002},
            {'chat_type': 'dialog', 'type': 'chat', 'chat_id': 980002},
            {'chat_type': 'chat', 'type': 'dialog', 'chat_id': 980002},
            {'chat_type': None, 'type': 'dialog', 'chat_id': 980002},
            {'chat_type': 'dialog', 'chat_id': 980099},
            {'chat_type': 'dialog', 'chat_id': True},
            {'chat_id': 980002},
        ]
        for recipient in accepted + rejected:
            with self.subTest(recipient=recipient):
                client = self.client(lambda request: httpx.Response(200, json={
                    'message': {'body': {'mid': 'synthetic-official-mid'}, 'recipient': recipient}}))
                outcome = client.send_message(980002, 'Синтетический ответ')
                self.assertEqual(outcome.status, 'accepted' if recipient in accepted else 'unknown')
                if recipient in rejected:
                    self.assertEqual(outcome.code, 'malformed_message_receipt')


if __name__ == '__main__':
    unittest.main()
