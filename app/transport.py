"""Local MAX simulator. There is no live mode, credential input or network path."""
from __future__ import annotations

import hashlib
import json
import threading

import httpx

from .vendor.max_transport import MaxClient
from .vendor.max_wire import normalize_update

_SYNTHETIC_TOKEN = 'IDEYAFIX-SYNTHETIC-NOT-A-CREDENTIAL'
_DEMO_TIMESTAMP_MS = 1_794_009_600_000


def normalize_demo_event(actor_id: int, text: str, event_id: str) -> dict:
    """Parse an actual MAX message_created shape using the native wire parser.

    Actor IDs, text and message IDs are synthetic demo input. The fixed timestamp
    makes normalization reproducible; no current account or provider is queried.
    """
    native = {
        'update_type': 'message_created',
        'timestamp': _DEMO_TIMESTAMP_MS,
        'message': {
            'sender': {'user_id': actor_id, 'is_bot': False},
            'recipient': {'type': 'dialog', 'chat_id': actor_id},
            'timestamp': _DEMO_TIMESTAMP_MS,
            'body': {'mid': event_id, 'text': text},
        },
    }
    body = json.dumps(native, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    return normalize_update(body).as_dict()


class SyntheticMax:
    """Serialize real MaxClient sends through an in-process MAX simulator.

    Repeated calls still perform repeated synthetic sends. Command idempotency is
    the caller's responsibility; call_count exposes the actual boundary count.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._call_count = 0
        self._closed = False
        self._delivery: tuple[int, str, str] | None = None
        self._client = MaxClient(_SYNTHETIC_TOKEN, transport=httpx.MockTransport(self._respond))

    def __repr__(self):
        return f'<SyntheticMax mode=simulator calls={self.call_count}>'

    @property
    def call_count(self) -> int:
        with self._lock:
            return self._call_count

    def _respond(self, request: httpx.Request) -> httpx.Response:
        """The sole external boundary is a local, fixed successful response."""
        self._call_count += 1
        if self._delivery is None:
            raise RuntimeError('synthetic_request_without_delivery')
        actor_id, text, provider_id = self._delivery
        if (request.method != 'POST' or request.url.scheme != 'https'
                or request.url.host != 'platform-api2.max.ru' or request.url.path != '/messages'
                or dict(request.url.params) != {'user_id': str(actor_id)}
                or request.headers.get('Authorization') != _SYNTHETIC_TOKEN
                or json.loads(request.content) != {'text': text}):
            raise RuntimeError('unexpected_synthetic_request')
        return httpx.Response(200, json={
            'message': {
                'recipient': {'type': 'dialog', 'user_id': actor_id},
                'body': {'mid': provider_id},
            },
        })

    def deliver(self, actor_id: int, text: str, event_id: str) -> dict:
        """Return simulated acceptance, never a claim of real MAX delivery."""
        with self._lock:
            if self._closed:
                raise RuntimeError('synthetic_transport_closed')
            normalized = normalize_demo_event(actor_id, text, event_id)
            identity = json.dumps([int(normalized['senderId']), normalized['nativeId']],
                                  ensure_ascii=True, separators=(',', ':')).encode('utf-8')
            provider_id = 'demo-' + hashlib.sha256(identity).hexdigest()
            self._delivery = (actor_id, text, provider_id)
            try:
                outcome = self._client.send_text(actor_id, text)
            finally:
                self._delivery = None
            if outcome.status != 'accepted' or outcome.provider_message_id != provider_id:
                raise RuntimeError('synthetic_delivery_failed')
            return {'accepted': True, 'providerMessageId': provider_id}

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._client.close()
                self._closed = True


def get_transport() -> SyntheticMax:
    """Create isolated simulator state for each application instance."""
    return SyntheticMax()
