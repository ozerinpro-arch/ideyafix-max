"""Explicit, fixed-origin MAX HTTP calls. No environment reads or automatic sends.

Provider acceptance is not delivery or a school authorization. The durable host
must commit an attempt before calling send_message, and never auto-retry unknown.
"""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import ssl
import stat
import time
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

import certifi
import httpx

from .max_wire import UPDATE_TYPES, MaxWireError, opaque_payload

API_ORIGIN = 'https://platform-api2.max.ru'
MAX_RESPONSE_BYTES = 65536


@dataclass(frozen=True, slots=True)
class TransportOutcome:
    status: Literal['accepted', 'rejected', 'unknown']
    code: str
    http_status: int | None = None
    provider_message_id: str | None = None
    retry_after_seconds: int | None = None
    data: dict | None = None

    def as_dict(self):
        return {'status': self.status, 'code': self.code, 'httpStatus': self.http_status,
                'providerMessageId': self.provider_message_id, 'retryAfterSeconds': self.retry_after_seconds,
                'data': None if self.data is None else json.loads(json.dumps(self.data))}


def _text(value, limit):
    return (isinstance(value, str) and 1 <= len(value) <= limit and bool(value.strip())
            and not any(0xD800 <= ord(c) <= 0xDFFF or ord(c) < 32 and c not in '\n\r\t'
                        or 0x7F <= ord(c) <= 0x9F for c in value))


def _identity(value):
    if type(value) is int and -(2**63) <= value < 2**63:
        return value
    if isinstance(value, str) and re.fullmatch(r'-?(0|[1-9][0-9]*)', value) and -(2**63) <= int(value) < 2**63:
        return int(value)
    raise ValueError('invalid_recipient')


def validate_webhook_url(value: str) -> str:
    """Validate/canonicalize the locally supported public HTTPS webhook URL.

    This performs no DNS/network request. TLS reachability and bot ownership are
    separately verified during explicit activation, never inferred from syntax.
    """
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 or ord(c) > 126 for c in value) or '\\' in value:
        raise ValueError('invalid_webhook_url')
    try:
        parts = urlsplit(value)
        host = parts.hostname
        if (parts.scheme != 'https' or not host or parts.username is not None or parts.password is not None
                or parts.port is not None or parts.query or parts.fragment or host.endswith('.')
                or '.' not in host or host.endswith(('.localhost', '.local', '.internal', '.onion'))
                or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', host)
                or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in host.split('.'))):
            raise ValueError()
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError()
        return urlunsplit(('https', host, parts.path or '/', '', ''))
    except (ValueError, UnicodeError):
        raise ValueError('invalid_webhook_url') from None


_webhook_url = validate_webhook_url  # Compatibility for internal callers/tests.


def _json_pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError('duplicate_response_key')
        value[key] = item
    return value


def _tls_context(additional_ca_file):
    # Explicit certifi avoids global SSL_CERT_FILE/proxy surprises. Extra trust
    # is local to THIS client; no verify=False or global trust-store mutation.
    context = ssl.create_default_context(cafile=certifi.where())
    if additional_ca_file is not None:
        try:
            path = Path(additional_ca_file)
            if not path.is_absolute() or any(part.is_symlink() for part in [path, *path.parents]):
                raise ValueError()
            info = path.stat()
            if (not stat.S_ISREG(info.st_mode) or info.st_size > 262144 or info.st_mode & 0o022
                    or info.st_uid not in {0, os.getuid()}):
                raise ValueError()
            context.load_verify_locations(cafile=str(path))
        except (OSError, ValueError, ssl.SSLError, TypeError):
            raise ValueError('invalid_additional_ca_file') from None
    return context


class MaxClient:
    """Synchronous worker client. Construction never performs a network call."""
    def __init__(self, token: str, *, additional_ca_file=None, transport: httpx.BaseTransport | None = None,
                 timeout_seconds: float = 10):
        if (not isinstance(token, str) or not 1 <= len(token) <= 4096 or not token.isascii()
                or any(ord(c) < 33 or ord(c) > 126 for c in token)):
            raise ValueError('invalid_max_credential')
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 15):
            raise ValueError('invalid_transport_timeout')
        self._token, self._timeout = token, float(timeout_seconds)
        self._client = httpx.Client(verify=_tls_context(additional_ca_file), trust_env=False,
            follow_redirects=False, transport=transport, timeout=httpx.Timeout(self._timeout, connect=min(5, self._timeout)),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
            headers={'Authorization': token, 'Accept': 'application/json'})

    def __repr__(self):
        return '<MaxClient credentials=redacted>'

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _outcome(self, status, code, http_status=None, *, provider_message_id=None, data=None, retry_after_seconds=None):
        # An upstream error/receipt must not reflect credentials into local UI/logs.
        if self._token in json.dumps([provider_message_id, data], ensure_ascii=False):
            return TransportOutcome('unknown', 'unsafe_provider_response', http_status)
        return TransportOutcome(status, code, http_status, provider_message_id, retry_after_seconds, data)

    def _call(self, method, path, *, params=None, body=None):
        if (method, path) not in {('GET', '/me'), ('GET', '/subscriptions'), ('GET', '/updates'), ('POST', '/subscriptions'), ('POST', '/messages')}:
            return TransportOutcome('rejected', 'invalid_operation')
        if body is not None and self._token in json.dumps(body, ensure_ascii=False):
            return TransportOutcome('rejected', 'credential_in_payload')
        if params is not None and self._token in json.dumps(params, ensure_ascii=False):
            return TransportOutcome('rejected', 'credential_in_query')
        started = time.monotonic()
        try:
            with self._client.stream(method, API_ORIGIN + path, params=params, json=body) as response:
                code = response.status_code
                if 300 <= code < 400:
                    return TransportOutcome('unknown', 'redirect_refused', code)
                if code == 408 or code >= 500:
                    return TransportOutcome('unknown', 'provider_outcome_unknown', code)
                if not 200 <= code < 300:
                    retry = response.headers.get('Retry-After', '')
                    retry = int(retry) if re.fullmatch(r'[0-9]{1,5}', retry) and int(retry) <= 86400 else None
                    return TransportOutcome('rejected', 'rate_limited' if code == 429 else 'provider_rejected', code, retry_after_seconds=retry)
                if code != 200:
                    return TransportOutcome('unknown', 'unexpected_success_status', code)
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES or time.monotonic() - started > self._timeout:
                        return TransportOutcome('unknown', 'response_limit', code)
                    chunks.append(chunk)
                raw = json.loads(b''.join(chunks).decode('utf-8'), object_pairs_hook=_json_pairs,
                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                if type(raw) is not dict:
                    raise ValueError()
                return code, raw
        except Exception:
            # No exception text/request/response repr may reach a durable outcome.
            return TransportOutcome('unknown', 'transport_or_response_error')

    def get_me(self) -> TransportOutcome:
        value = self._call('GET', '/me')
        if isinstance(value, TransportOutcome):
            return value
        code, raw = value
        try:
            bot_id = _identity(raw['user_id'])
            if raw.get('is_bot') is not True:
                raise ValueError()
            username = raw.get('username')
            if username is not None and (not _text(username, 80) or any(c.isspace() for c in username)):
                raise ValueError()
            return self._outcome('accepted', 'bot_identity', code, data={'botUserId': str(bot_id), 'isBot': True, 'username': username})
        except (KeyError, TypeError, ValueError):
            return TransportOutcome('unknown', 'malformed_bot_identity', code)

    def list_subscriptions(self) -> TransportOutcome:
        value = self._call('GET', '/subscriptions')
        if isinstance(value, TransportOutcome):
            return value
        code, raw = value
        try:
            rows = raw['subscriptions']
            if not isinstance(rows, list) or len(rows) > 100:
                raise ValueError()
            result = []
            for item in rows:
                if type(item) is not dict:
                    raise ValueError()
                types = item.get('update_types', [])
                if not isinstance(types, list) or len(types) > 100 or any(not _text(t, 80) for t in types):
                    raise ValueError()
                result.append({'url': _webhook_url(item['url']), 'updateTypes': types})
            return self._outcome('accepted', 'subscriptions', code, data={'subscriptions': result})
        except (KeyError, TypeError, ValueError):
            return TransportOutcome('unknown', 'malformed_subscriptions', code)

    def get_updates(self, *, marker=None, limit=20, timeout=5,
                    update_types=('bot_started', 'bot_stopped')) -> TransportOutcome:
        """Read a bounded batch; the host commits it before using its next marker.

        A missing marker asks MAX for the latest update only. Sending a marker
        acknowledges previous updates, so cursor ownership belongs to the host.
        The returned updates are private runtime input, never a status/log DTO.
        """
        try:
            if (type(limit) is not int or not 1 <= limit <= 100
                    or type(timeout) is not int or not 0 <= timeout <= 5
                    or marker is not None and (type(marker) is not int or not 0 <= marker < 2**63)
                    or not isinstance(update_types, (tuple, list))
                    or not 1 <= len(update_types) <= len(UPDATE_TYPES)
                    or any(t not in UPDATE_TYPES for t in update_types)
                    or len(set(update_types)) != len(update_types)):
                raise ValueError()
            params = {'limit': limit, 'timeout': timeout, 'types': ','.join(update_types)}
            if marker is not None:
                params['marker'] = marker
        except (TypeError, ValueError):
            return TransportOutcome('rejected', 'invalid_poll_request')
        value = self._call('GET', '/updates', params=params)
        if isinstance(value, TransportOutcome):
            return value
        code, raw = value
        updates, next_marker = raw.get('updates'), raw.get('marker')
        if (set(raw) != {'updates', 'marker'} or type(updates) is not list
                or len(updates) > limit or any(type(item) is not dict for item in updates)
                or next_marker is not None and (type(next_marker) is not int or not 0 <= next_marker < 2**63)):
            return TransportOutcome('unknown', 'malformed_updates', code)
        return self._outcome('accepted', 'updates', code, data={'updates': updates, 'marker': next_marker})

    def register_subscription(self, url: str, secret: str, update_types=UPDATE_TYPES) -> TransportOutcome:
        try:
            url = _webhook_url(url)
            if (not isinstance(secret, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', secret)
                    or not isinstance(update_types, (list, tuple)) or not 1 <= len(update_types) <= len(UPDATE_TYPES)
                    or any(t not in UPDATE_TYPES for t in update_types) or len(set(update_types)) != len(update_types)):
                raise ValueError()
        except (ValueError, TypeError):
            return TransportOutcome('rejected', 'invalid_subscription')
        value = self._call('POST', '/subscriptions', body={'url': url, 'secret': secret, 'update_types': list(update_types)})
        if isinstance(value, TransportOutcome):
            return value
        code, raw = value
        if type(raw.get('success')) is not bool:
            return TransportOutcome('unknown', 'malformed_subscription_receipt', code)
        return TransportOutcome('accepted' if raw['success'] else 'rejected', 'subscription_registered' if raw['success'] else 'subscription_rejected', code)

    def send_message(self, chat_id, text: str, *, callback_payload=None, request_key=None) -> TransportOutcome:
        """chat_id must come from the host's verified personal-dialog binding."""
        return self._send(text, chat_id=chat_id, callback_payload=callback_payload, request_key=request_key)

    def send_text(self, user_id, text: str, *, callback_payload=None, request_key=None) -> TransportOutcome:
        """Direct personal send by user_id; never combined with chat_id."""
        return self._send(text, user_id=user_id, callback_payload=callback_payload, request_key=request_key)

    def _send(self, text, *, user_id=None, chat_id=None, callback_payload=None, request_key=None):
        try:
            if (user_id is None) == (chat_id is None) or not _text(text, 4000):
                raise ValueError()
            if request_key is not None and (not _text(request_key, 160) or any(c.isspace() for c in request_key)):
                raise ValueError()
            address = {'user_id': _identity(user_id)} if user_id is not None else {'chat_id': _identity(chat_id)}
            body = {'text': text}
            if callback_payload is not None:
                body['attachments'] = [{'type': 'inline_keyboard', 'payload': {'buttons': [[
                    {'type': 'callback', 'text': 'Ответить', 'payload': opaque_payload(callback_payload)}]]}}]
        except (ValueError, TypeError, MaxWireError):
            return TransportOutcome('rejected', 'invalid_message')
        value = self._call('POST', '/messages', params=address, body=body)
        if isinstance(value, TransportOutcome):
            return value
        code, raw = value
        try:
            message = raw['message']
            mid = message['body']['mid']
            if not _text(mid, 200) or any(c.isspace() for c in mid):
                raise ValueError()
            recipient = message.get('recipient')
            if recipient is not None:
                if type(recipient) is not dict or recipient.get('type') != 'dialog':
                    raise ValueError()
                for key, expected in address.items():
                    if key in recipient and _identity(recipient[key]) != expected:
                        raise ValueError()
            return self._outcome('accepted', 'provider_accepted', code, provider_message_id=mid)
        except (KeyError, TypeError, ValueError):
            return TransportOutcome('unknown', 'malformed_message_receipt', code)
