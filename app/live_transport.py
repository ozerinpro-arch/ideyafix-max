"""Explicit live MAX client for the separately authorized local polling worker.

The synthetic UI keeps its own MockTransport-only client. This module never reads
environment credentials, changes system trust, registers webhooks or retries sends.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat

import httpx

from .vendor.max_transport import MaxClient, TransportOutcome, _tls_context


def _read_token(path: Path) -> str:
    """Read a private single-token file without returning paths or file contents."""
    descriptor = None
    try:
        path = Path(path)
        if not path.is_absolute() or any(p.is_symlink() for p in [path, *path.parents]):
            raise ValueError()
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                or not 0 < info.st_size < 8192):
            raise ValueError()
        with os.fdopen(descriptor, 'rb') as stream:
            descriptor = None
            data = stream.read(8192)
            after = os.fstat(stream.fileno())
        current = path.lstat()
        if (len(data) != info.st_size or len(data) >= 8192
                or (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)
                or any(p.is_symlink() for p in [path, *path.parents])):
            raise ValueError()
        token = data.decode('ascii').strip()
        if not 1 <= len(token) <= 4096 or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise ValueError()
        return token
    except (OSError, ValueError, TypeError, UnicodeError):
        raise ValueError('invalid_max_token_file') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


class LiveMaxClient(MaxClient):
    """Fixed-origin verified HTTPS; the durable worker owns send authorization."""

    def __init__(self, token_file: Path, additional_ca_file: Path | None = None, *, transport=None):
        if transport is not None and type(transport) is not httpx.MockTransport:
            raise ValueError('invalid_test_transport')
        token = _read_token(token_file)
        context = _tls_context(additional_ca_file)
        limits = httpx.Limits(max_connections=4, max_keepalive_connections=2)
        boundary = transport if transport is not None else httpx.HTTPTransport(
            verify=context, trust_env=False, retries=0, limits=limits)
        self._token, self._timeout = token, 10.0
        self._client = httpx.Client(
            verify=context, trust_env=False, follow_redirects=False, transport=boundary,
            timeout=httpx.Timeout(self._timeout, connect=5), limits=limits,
            headers={'Authorization': token, 'Accept': 'application/json'},
        )

    def __repr__(self):
        return '<LiveMaxClient credentials=redacted>'

    def _call(self, method, path, *, params=None, body=None):
        if (method, path) not in {
                ('GET', '/me'), ('GET', '/subscriptions'), ('GET', '/updates'), ('POST', '/messages')}:
            return TransportOutcome('rejected', 'invalid_operation')
        # Replies must use the personal dialog authorized by the worker, never a
        # direct user lookup, arbitrary URL or webhook mutation.
        if method == 'POST' and (type(params) is not dict or set(params) != {'chat_id'}):
            return TransportOutcome('rejected', 'invalid_operation')
        return super()._call(method, path, params=params, body=body)

    def get_updates(self, *, marker=None, limit=20, timeout=5,
                    update_types=('bot_started', 'bot_stopped', 'message_created')) -> TransportOutcome:
        return super().get_updates(marker=marker, limit=limit, timeout=timeout, update_types=update_types)

    def send_text(self, *args, **kwargs) -> TransportOutcome:
        """Direct user sends are outside this worker's authorized dialog flow."""
        return TransportOutcome('rejected', 'invalid_operation')

    def register_subscription(self, *args, **kwargs) -> TransportOutcome:
        """The local polling client cannot modify provider webhook state."""
        return TransportOutcome('rejected', 'invalid_operation')
