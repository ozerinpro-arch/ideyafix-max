"""Bounded native MAX updates, without school identity or authorization.

The host authenticates the webhook BEFORE calling normalize_update. This module
accepts the school's personal-text/opaque-button subset, not arbitrary MAX chats.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re

MAX_BODY_BYTES = 65536
UPDATE_TYPES = ('message_created', 'message_callback', 'bot_started', 'bot_stopped')


class MaxWireError(ValueError):
    def __init__(self, code='invalid_update'):
        self.code = code
        super().__init__(code)  # Never echo native text, names, payloads or secrets.


def _object(value):
    if type(value) is not dict:
        raise MaxWireError()
    return value


def _number(value, *, timestamp=False):
    if type(value) is not int or not -(2**63) <= value < 2**63 or timestamp and value < 0:
        raise MaxWireError('invalid_timestamp' if timestamp else 'invalid_identity')
    return value


def _string(value, maximum, *, allow_lines=False):
    if (not isinstance(value, str) or not 1 <= len(value) <= maximum
            or any(0xD800 <= ord(c) <= 0xDFFF or ord(c) < 32 and not (allow_lines and c in '\r\n\t')
                   or 0x7F <= ord(c) <= 0x9F for c in value)):
        raise MaxWireError('invalid_text')
    return value


def opaque_payload(value):
    """School button token subset; never a JSON command or identity claim."""
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', value):
        raise MaxWireError('invalid_callback_payload')
    return value


def _pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            raise MaxWireError('duplicate_json_key')
        value[key] = item
    return value


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


@dataclass(frozen=True, slots=True, repr=False)
class NormalizedUpdate:
    event_type: str
    event_key: str
    sender_id: str
    chat_id: str
    timestamp_ms: int
    native_id: str | None = None
    text: str | None = None
    start_payload: str | None = None
    callback_payload: str | None = None
    payload_hash: str = ''
    message_timestamp_ms: int | None = None
    callback_timestamp_ms: int | None = None

    def __repr__(self):
        return f'<NormalizedUpdate type={self.event_type} payload=redacted>'

    @property
    def is_private(self):
        return True

    def as_dict(self):
        return {'eventType': self.event_type, 'eventId': self.event_key,
                'senderId': self.sender_id, 'chatId': self.chat_id, 'timestamp': self.timestamp_ms,
                'nativeId': self.native_id, 'text': self.text, 'startPayload': self.start_payload,
                'callbackPayload': self.callback_payload, 'payloadHash': self.payload_hash,
                'isPrivate': True, 'messageTimestamp': self.message_timestamp_ms,
                'callbackTimestamp': self.callback_timestamp_ms}

    def as_native(self):
        """Minimal native shape; opaque callback remains opaque (not legacy JSON)."""
        value = {'update_type': self.event_type, 'timestamp': self.timestamp_ms}
        user = {'user_id': int(self.sender_id)}
        if self.event_type in {'bot_started', 'bot_stopped'}:
            value.update(chat_id=int(self.chat_id), user=user)
            if self.start_payload is not None:
                value['payload'] = self.start_payload
        else:
            message = {'recipient': {'type': 'dialog', 'chat_id': int(self.chat_id)}}
            if self.message_timestamp_ms is not None:
                message['timestamp'] = self.message_timestamp_ms
            if self.event_type == 'message_created':
                message.update(sender=user, body={'mid': self.native_id, 'text': self.text})
            else:
                value['callback'] = {'callback_id': self.native_id, 'user': user, 'payload': self.callback_payload}
                if self.callback_timestamp_ms is not None:
                    value['callback']['timestamp'] = self.callback_timestamp_ms
            value['message'] = message
        return value


def normalize_update(body: bytes) -> NormalizedUpdate:
    if type(body) is not bytes or len(body) > MAX_BODY_BYTES:
        raise MaxWireError('body_too_large' if isinstance(body, bytes) else 'invalid_body')
    try:
        value = json.loads(body.decode('utf-8'), object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(MaxWireError()))
        value = _object(value)
        # Validate all Unicode, including ignored extension fields, without exposing it.
        encoded = _canonical(value).encode('utf-8')
    except MaxWireError:
        raise
    except (ValueError, UnicodeError, TypeError, RecursionError):
        raise MaxWireError() from None
    kind = value.get('update_type')
    if kind not in UPDATE_TYPES:
        raise MaxWireError('unsupported_update')
    timestamp = _number(value.get('timestamp'), timestamp=True)
    native = text = start_payload = callback_payload = message_timestamp = callback_timestamp = None
    if kind in {'message_created', 'message_callback'}:
        if ('is_channel' in value and value['is_channel'] is not False
                or value.get('chat_type') not in (None, 'dialog')):
            raise MaxWireError('personal_dialog_required')
        message = _object(value.get('message'))
        recipient = _object(message.get('recipient'))
        if recipient.get('type') != 'dialog':
            raise MaxWireError('personal_dialog_required')
        chat = _number(recipient.get('chat_id'))
        if 'chat_id' in value and _number(value['chat_id']) != chat:
            raise MaxWireError('ambiguous_address')
        if message.get('link') is not None:
            raise MaxWireError('linked_message_not_supported')
        if 'timestamp' in message:
            message_timestamp = _number(message['timestamp'], timestamp=True)
        if kind == 'message_created':
            sender = _object(message.get('sender'))
            contents = _object(message.get('body'))
            if contents.get('attachments') not in (None, []):
                raise MaxWireError('attachments_not_supported')
            native = _string(contents.get('mid'), 200)
            text = _string(contents.get('text'), 4000, allow_lines=True)
            if not text.strip():
                raise MaxWireError('empty_message')
        else:
            callback = _object(value.get('callback'))
            sender = _object(callback.get('user'))
            native = _string(callback.get('callback_id'), 200)
            callback_payload = opaque_payload(callback.get('payload'))
            if 'timestamp' in callback:
                callback_timestamp = _number(callback['timestamp'], timestamp=True)
                if callback_timestamp > timestamp:
                    raise MaxWireError('invalid_timestamp')
    else:
        # MAX defines start/stop as user-to-bot lifecycle events. They have no
        # recipient object; never infer a personal chat for other event types.
        if ('is_channel' in value and value['is_channel'] is not False
                or value.get('chat_type') not in (None, 'dialog')):
            raise MaxWireError('personal_dialog_required')
        if 'message' in value or 'callback' in value:
            raise MaxWireError('ambiguous_address')
        chat = _number(value.get('chat_id'))
        sender = _object(value.get('user'))
        if kind == 'bot_started' and value.get('payload') is not None:
            start_payload = _string(value['payload'], 128)
    sender_id = _number(sender.get('user_id'))
    if 'is_bot' in sender and type(sender['is_bot']) is not bool:
        raise MaxWireError('invalid_sender')
    if sender.get('is_bot') is True:
        raise MaxWireError('bot_sender_not_supported')
    if 'user' in value and kind.startswith('message_') and _number(_object(value['user']).get('user_id')) != sender_id:
        raise MaxWireError('ambiguous_sender')
    event_identity = [kind, str(chat), str(sender_id), native if native is not None else [timestamp, start_payload]]
    return NormalizedUpdate(kind, hashlib.sha256(_canonical(event_identity).encode()).hexdigest(),
                            str(sender_id), str(chat), timestamp, native, text, start_payload,
                            callback_payload, hashlib.sha256(encoded).hexdigest(), message_timestamp, callback_timestamp)
