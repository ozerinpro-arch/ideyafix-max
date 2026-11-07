"""Verified HTTPS boundary for the school notification worker protocol."""
import json
import re
import time

import httpx

from .live_transport import _read_token
from .vendor.max_transport import _identity, _text, _tls_context


WORKER_URL = 'https://taskfloww.site/school-staff/api/demo-max/worker'
_LINK_TOKEN = re.compile(r'sdn_[A-Za-z0-9_-]{32}')
_CLAIM_ID = re.compile(r'[A-Za-z0-9_-]{32}')
DELIVERY_STATES = frozenset({'accepted', 'rejected', 'unknown'})


def school_link_token(event):
    """Recognize only a private native start or an explicit typed start."""
    if event.get('isPrivate') is not True or event.get('file') is not None:
        return None
    if event['eventType'] == 'bot_started':
        token = event['startPayload']
    elif event['eventType'] == 'message_created':
        command = event['text'].split()
        token = command[1] if len(command) == 2 and command[0] == '/start' else None
    else:
        return None
    return token if isinstance(token, str) and _LINK_TOKEN.fullmatch(token) else None


class SchoolNotificationsUnavailable(Exception):
    def __init__(self):
        super().__init__('school_notifications_unavailable')


class SchoolNotificationClient:
    def __init__(self, token_file, *, transport=None):
        if transport is not None and type(transport) is not httpx.MockTransport:
            raise ValueError('invalid_test_transport')
        self._token = _read_token(token_file)
        context = _tls_context(None)
        boundary = transport if transport is not None else httpx.HTTPTransport(
            verify=context, trust_env=False, retries=0,
        )
        self._client = httpx.Client(
            verify=context, transport=boundary, trust_env=False, follow_redirects=False,
            timeout=httpx.Timeout(2, connect=1),
            headers={'X-Taskflow-Max-Worker': self._token, 'Accept': 'application/json',
                     'Accept-Encoding': 'identity'},
        )

    def _post(self, action, **fields):
        deadline = time.monotonic() + 3
        try:
            with self._client.stream('POST', WORKER_URL, json={'action': action, **fields}) as response:
                if response.status_code != 200 or response.headers.get('Content-Encoding', 'identity').casefold() != 'identity':
                    raise SchoolNotificationsUnavailable()
                data = bytearray()
                for chunk in response.iter_bytes():
                    if len(data) + len(chunk) > 65536 or time.monotonic() > deadline:
                        raise SchoolNotificationsUnavailable()
                    data.extend(chunk)
                payload = json.loads(data)
        except (httpx.HTTPError, ValueError, RecursionError):
            raise SchoolNotificationsUnavailable() from None
        if type(payload) is not dict:
            raise SchoolNotificationsUnavailable()
        return payload

    def link(self, token, chat_id, sender_id):
        if not isinstance(token, str) or not _LINK_TOKEN.fullmatch(token):
            raise ValueError('invalid_school_link_token')
        payload = self._post('link', token=token, chatId=str(_identity(chat_id)),
                             senderId=str(_identity(sender_id)))
        if type(payload.get('linked')) is not bool:
            raise SchoolNotificationsUnavailable()
        return payload['linked']

    def claim(self):
        payload = self._post('claim')
        if 'item' not in payload:
            raise SchoolNotificationsUnavailable()
        item = payload['item']
        if item is None:
            return None
        if (type(item) is not dict or not isinstance(item.get('claimId'), str)
                or not _CLAIM_ID.fullmatch(item['claimId']) or not _text(item.get('text'), 4000)
                or self._token in item['text'] or not isinstance(item.get('chatId'), str)
                or len(item['chatId']) > 20):
            raise SchoolNotificationsUnavailable()
        try:
            chat_id = str(_identity(item['chatId']))
        except ValueError:
            raise SchoolNotificationsUnavailable() from None
        return {'claimId': item['claimId'], 'chatId': chat_id, 'text': item['text']}

    def finish(self, claim_id, status):
        if not isinstance(claim_id, str) or not _CLAIM_ID.fullmatch(claim_id) or status not in DELIVERY_STATES:
            raise ValueError('invalid_school_delivery_receipt')
        payload = self._post('finish', claimId=claim_id, status=status)
        if type(payload.get('ok')) is not bool:
            raise SchoolNotificationsUnavailable()
        return payload['ok']

    def close(self):
        self._client.close()
