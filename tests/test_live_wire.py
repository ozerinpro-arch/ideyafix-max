"""Current official Recipient shape plus strict legacy compatibility; synthetic only."""
from copy import deepcopy
import json
import unittest

from app.vendor.max_wire import MaxWireError, normalize_update


class LiveWireTests(unittest.TestCase):
    def native(self, recipient=None):
        return {'update_type': 'message_created', 'timestamp': 1794009600000,
                'message': {'sender': {'user_id': 990001, 'is_bot': False},
                            'recipient': recipient if recipient is not None else {
                                'chat_type': 'dialog', 'chat_id': 990002, 'user_id': 990003},
                            'timestamp': 1794009600000,
                            'body': {'mid': 'synthetic-native-mid', 'text': '/start'}}}

    def parse(self, native):
        return normalize_update(json.dumps(native).encode('utf-8')).as_dict()

    def test_official_recipient_normalizes_identically_to_legacy_except_raw_hash(self):
        official = self.parse(self.native())
        legacy = self.parse(self.native({'type': 'dialog', 'chat_id': 990002, 'user_id': 990003}))
        self.assertEqual({k: v for k, v in official.items() if k != 'payloadHash'},
                         {k: v for k, v in legacy.items() if k != 'payloadHash'})
        self.assertNotEqual(official['payloadHash'], legacy['payloadHash'])
        self.assertEqual((official['eventType'], official['senderId'], official['chatId'], official['text']),
                         ('message_created', '990001', '990002', '/start'))
        both = self.parse(self.native({'chat_type': 'dialog', 'type': 'dialog', 'chat_id': 990002}))
        self.assertEqual(both['eventId'], official['eventId'])

    def test_nonpersonal_missing_and_conflicting_types_are_rejected(self):
        for recipient in [
                {'chat_type': 'chat', 'chat_id': 990002},
                {'chat_type': 'channel', 'chat_id': 990002},
                {'chat_type': 'dialog', 'type': 'chat', 'chat_id': 990002},
                {'chat_type': 'chat', 'type': 'dialog', 'chat_id': 990002},
                {'chat_type': None, 'type': 'dialog', 'chat_id': 990002},
                {'chat_type': 'dialog', 'type': None, 'chat_id': 990002},
                {'chat_id': 990002},
        ]:
            with self.subTest(recipient=recipient), self.assertRaises(MaxWireError) as error:
                self.parse(self.native(recipient))
            self.assertEqual(error.exception.code, 'personal_dialog_required')

    def test_nullable_or_invalid_inbound_chat_cannot_infer_a_dialog_identity(self):
        for chat in (None, True, '990002', 2**63):
            with self.subTest(chat=chat), self.assertRaises(MaxWireError):
                self.parse(self.native({'chat_type': 'dialog', 'chat_id': chat, 'user_id': 990001}))

    def test_official_message_callback_personal_recipient_is_supported(self):
        native = self.native()
        native['update_type'] = 'message_callback'
        native['message'].pop('sender')
        native['message'].pop('body')
        native['callback'] = {'callback_id': 'synthetic-callback-id', 'user': {'user_id': 990001},
                              'payload': 'synthetic_opaque_payload_123'}
        value = self.parse(native)
        self.assertEqual(value['eventType'], 'message_callback')
        self.assertEqual(value['chatId'], '990002')
        self.assertEqual(value['callbackPayload'], 'synthetic_opaque_payload_123')

    def test_existing_sender_address_attachment_and_link_guards_are_retained(self):
        original = self.native()
        cases = []
        value = deepcopy(original); value['message']['sender']['is_bot'] = True; cases.append(value)
        value = deepcopy(original); value['user'] = {'user_id': 990099}; cases.append(value)
        value = deepcopy(original); value['chat_id'] = 990099; cases.append(value)
        value = deepcopy(original); value['message']['body']['attachments'] = [{'type': 'file'}]; cases.append(value)
        value = deepcopy(original); value['message']['link'] = {'type': 'reply'}; cases.append(value)
        for index, value in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(MaxWireError):
                self.parse(value)


if __name__ == '__main__':
    unittest.main()
