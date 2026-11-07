"""Boundary checks for the local simulator; no real credential or provider."""
import inspect
import json
import socket
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import httpx

from app.transport import SyntheticMax, get_transport, normalize_demo_event
from app.vendor.max_transport import MaxClient
from app.vendor.max_wire import MaxWireError, normalize_update


class DemoTransportTests(unittest.TestCase):
    def test_native_message_shape_and_reproducible_normalization(self):
        value = normalize_demo_event(101, 'Идея: кружок робототехники\nПосле уроков', 'demo-event-1')
        self.assertEqual(value['eventType'], 'message_created')
        self.assertEqual(value['senderId'], '101')
        self.assertEqual(value['chatId'], '101')
        self.assertEqual(value['nativeId'], 'demo-event-1')
        self.assertTrue(value['isPrivate'])
        self.assertEqual(len(value['eventId']), 64)
        self.assertEqual(value, normalize_demo_event(101, value['text'], 'demo-event-1'))
        native = {
            'update_type': 'message_created', 'timestamp': value['timestamp'],
            'message': {'sender': {'user_id': 101, 'is_bot': False},
                        'recipient': {'type': 'dialog', 'chat_id': 101},
                        'timestamp': value['timestamp'],
                        'body': {'mid': 'demo-event-1', 'text': value['text']}},
        }
        self.assertEqual(value, normalize_update(json.dumps(native, ensure_ascii=True, separators=(',', ':')).encode()).as_dict())

    def test_invalid_native_inputs_never_reach_send(self):
        invalid = [
            (True, 'Текст', 'event'), ('101', 'Текст', 'event'), (2**63, 'Текст', 'event'),
            (101, '', 'event'), (101, '  \n', 'event'), (101, 'я' * 4001, 'event'),
            (101, 'bad\x00text', 'event'), (101, '\ud800', 'event'),
            (101, 'Текст', ''), (101, 'Текст', 'x' * 201), (101, 'Текст', 'bad\x01id'),
        ]
        transport = get_transport()
        self.addCleanup(transport.close)
        for actor, text, event in invalid:
            with self.subTest(actor=actor, text_length=len(text), event_length=len(event)):
                with self.assertRaises(MaxWireError):
                    transport.deliver(actor, text, event)
        self.assertEqual(transport.call_count, 0)

    def test_actual_maxclient_uses_mock_request_without_socket_or_env(self):
        observed = []
        original = httpx.MockTransport.handle_request

        def observe(mock, request):
            observed.append((request.method, request.url.path, dict(request.url.params),
                             json.loads(request.content), 'Authorization' in request.headers))
            return original(mock, request)

        with patch.dict('os.environ', {'MAX_TOKEN': 'UNUSED-SYNTHETIC-ENV', 'HTTPS_PROXY': 'http://127.0.0.1:9'}), \
                patch.object(socket.socket, 'connect', side_effect=AssertionError('network_forbidden')), \
                patch.object(socket, 'getaddrinfo', side_effect=AssertionError('dns_forbidden')), \
                patch.object(httpx.MockTransport, 'handle_request', observe):
            transport = get_transport()
            self.addCleanup(transport.close)
            self.assertIs(type(transport._client), MaxClient)
            self.assertIs(type(transport._client._client._transport), httpx.MockTransport)
            result = transport.deliver(202, 'Синтетическая идея', 'mock-event-1')
        self.assertEqual(observed, [('POST', '/messages', {'user_id': '202'}, {'text': 'Синтетическая идея'}, True)])
        self.assertEqual(set(result), {'accepted', 'providerMessageId'})
        self.assertTrue(result['accepted'])
        self.assertTrue(result['providerMessageId'].startswith('demo-'))
        self.assertNotIn('SYNTHETIC', json.dumps(result) + repr(transport) + repr(transport._client))
        self.assertEqual(transport.call_count, 1)

    def test_receipts_stable_but_each_delivery_counts(self):
        transport = get_transport()
        self.addCleanup(transport.close)
        first = transport.deliver(1, 'Идея', 'same-event')
        self.assertEqual(first, transport.deliver(1, 'Другой текст', 'same-event'))
        self.assertNotEqual(first, transport.deliver(2, 'Идея', 'same-event'))
        self.assertNotEqual(first, transport.deliver(1, 'Идея', 'other-event'))
        self.assertEqual(transport.call_count, 4)

    def test_concurrent_deliveries_keep_their_receipts(self):
        transport = get_transport()
        self.addCleanup(transport.close)
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda actor: transport.deliver(actor, 'Идея', 'concurrent'), range(1, 9)))
        self.assertEqual(len({item['providerMessageId'] for item in results}), 8)
        self.assertEqual(transport.call_count, 8)

    def test_isolated_factory_and_closed_transport(self):
        first, second = get_transport(), get_transport()
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        self.assertIsNot(first, second)
        first.deliver(1, 'Идея', 'event')
        first.close()
        first.close()
        with self.assertRaisesRegex(RuntimeError, '^synthetic_transport_closed$'):
            first.deliver(1, 'Идея', 'event')
        self.assertEqual(first.call_count, 1)
        self.assertEqual(second.call_count, 0)
        second.deliver(2, 'Идея', 'event')
        self.assertEqual(second.call_count, 1)

    def test_no_live_constructor_or_credentials_in_public_api(self):
        self.assertEqual(list(inspect.signature(SyntheticMax).parameters), [])
        self.assertEqual(list(inspect.signature(get_transport).parameters), [])
        for transport in (None, httpx.HTTPTransport()):
            try:
                with self.assertRaisesRegex(ValueError, '^synthetic_transport_required$'):
                    MaxClient('SYNTHETIC-TEST', transport=transport)
            finally:
                if transport is not None:
                    transport.close()

    def test_failure_does_not_expose_or_retry_synthetic_credential(self):
        transport = get_transport()
        self.addCleanup(transport.close)
        with patch.object(httpx.MockTransport, 'handle_request', side_effect=RuntimeError('PRIVATE-SYNTHETIC-ERROR')) as handler:
            with self.assertRaisesRegex(RuntimeError, '^synthetic_delivery_failed$') as error:
                transport.deliver(1, 'Идея', 'failure-event')
        self.assertEqual(handler.call_count, 1)
        self.assertNotIn('PRIVATE', str(error.exception))
        self.assertNotIn('CREDENTIAL', str(error.exception))


if __name__ == '__main__':
    unittest.main()
