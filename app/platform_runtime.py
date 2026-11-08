"""Bounded private subprocess bridge to the independently copied native core."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import select
import stat
import subprocess
import sys
import threading
import time

PROJECT = Path(__file__).resolve().parent.parent
ENGINE = PROJECT / 'platform_engine'
MAX_FRAME = 64 * 1024 * 1024
MAX_REQUEST = 16 * 1024 * 1024
RPC_TIMEOUT = 180
SHUTDOWN_TIMEOUT = 10


class _TransportError(RuntimeError):
    """The pipe no longer has a trustworthy request/response boundary."""


class PlatformRuntime:
    def __init__(self, root: Path | None = None, *, nanogpt_config: Path | None = None):
        self.root = Path(root or PROJECT / 'runtime/platform').absolute()
        if '..' in self.root.parts or any(p.is_symlink() for p in [self.root, *self.root.parents]):
            raise ValueError('invalid_platform_runtime_directory')
        allowed = self.root.is_relative_to(PROJECT / 'runtime') or any(
            self.root.is_relative_to(Path(p)) for p in ('/private/tmp', '/var/tmp', '/private/var/folders'))
        if not allowed:
            raise ValueError('platform_runtime_must_be_owned_local_or_temporary')
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        info = self.root.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError('private_platform_runtime_directory_required')
        marker = self.root / '.platform-root.json'
        if marker.exists():
            if marker.is_symlink() or marker.stat().st_nlink != 1 or json.loads(marker.read_text()) != {'kind':'ideyafix-independent-platform-v1'}:
                raise ValueError('invalid_platform_runtime_marker')
        else:
            if any(self.root.iterdir()):
                raise ValueError('foreign_platform_runtime_directory')
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as stream: json.dump({'kind':'ideyafix-independent-platform-v1'}, stream)
        for path in self.root.rglob('*'):
            info = path.lstat()
            if (path.is_symlink() or info.st_uid != os.getuid()
                    or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
                    or stat.S_ISREG(info.st_mode) and (info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600)
                    or stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) != 0o700):
                raise ValueError('unsafe_platform_runtime_entry')
        self._mutex = threading.RLock()
        self._closed = False
        self._process = None
        self._broken = False
        self._rpc_id = 0
        self._lock_fd = os.open(self.root / '.rpc.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        lock_info = os.fstat(self._lock_fd)
        if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_nlink != 1 or lock_info.st_uid != os.getuid():
            os.close(self._lock_fd); raise ValueError('invalid_platform_runtime_lock')
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self._lock_fd); raise ValueError('platform_runtime_already_running') from None
        config_path=Path(nanogpt_config).absolute() if nanogpt_config is not None else None
        if config_path is not None and not (config_path.is_relative_to(PROJECT/'runtime') or any(
                config_path.is_relative_to(Path(p)) for p in ('/private/tmp','/var/tmp','/private/var/folders'))):
            os.close(self._lock_fd)
            raise ValueError('provider_config_must_be_independent')
        self._argv = [sys.executable, '-I', '-B', str(ENGINE / 'runtime_child.py'),
                      str(self.root), str(config_path) if config_path else '']
        try:
            self._start_process()
        except BaseException:
            os.close(self._lock_fd)
            self._closed = True
            raise

    def _start_process(self):
        env = {'PATH': os.defpath, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHON_DOTENV_DISABLED': '1'}
        self._process = subprocess.Popen(self._argv, cwd=ENGINE, env=env, stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        try:
            os.set_blocking(self._process.stdin.fileno(), False)
            os.set_blocking(self._process.stdout.fileno(), False)
            self._receive(time.monotonic() + RPC_TIMEOUT, None)
        except BaseException:
            self._stop_process()
            raise

    def _stop_process(self):
        process = self._process
        if process is None:
            return
        # Do not reuse a pipe after a timeout. Killing the child also makes
        # sure a restart cannot race an earlier in-flight mutation.
        self._broken = True
        if process.poll() is None:
            process.kill()
        process.wait(timeout=SHUTDOWN_TIMEOUT)
        process.stdin.close()
        process.stdout.close()
        self._process = None
        self._broken = False

    @staticmethod
    def _wait(fd, deadline, *, writing=False):
        remaining = deadline - time.monotonic()
        if remaining > 0:
            ready_read, ready_write, _ = select.select(
                [] if writing else [fd], [fd] if writing else [], [], remaining)
            if ready_read or ready_write:
                return
        raise _TransportError('platform_runtime_timeout')

    def _receive(self, deadline, rpc_id):
        fd = self._process.stdout.fileno()
        line = bytearray()
        while True:
            self._wait(fd, deadline)
            try:
                chunk = os.read(fd, 65536)
            except BlockingIOError:
                continue
            if not chunk or len(line) + len(chunk) > MAX_FRAME:
                raise _TransportError('platform_runtime_invalid_frame')
            line.extend(chunk)
            newline = chunk.find(b'\n')
            if newline >= 0:
                if newline != len(chunk) - 1:
                    raise _TransportError('platform_runtime_invalid_frame')
                break
        try:
            value = json.loads(line)
        except (ValueError, RecursionError) as error:
            raise _TransportError('platform_runtime_invalid_frame') from error
        if not isinstance(value, dict) or 'rpcId' not in value:
            raise _TransportError('platform_runtime_invalid_frame')
        actual_id = value['rpcId']
        if type(actual_id) is not type(rpc_id) or actual_id != rpc_id:
            raise _TransportError('platform_runtime_mismatched_response')
        if type(value.get('ok')) is not bool:
            raise _TransportError('platform_runtime_invalid_frame')
        if value.get('ok') is not True:
            raise RuntimeError('platform_runtime_operation_failed:' + str(value.get('error', 'unknown')))
        return value.get('value')

    def _exchange(self, operation, arguments, timeout):
        self._rpc_id += 1
        line = (json.dumps({'rpcId': self._rpc_id, 'operation': operation, 'arguments': arguments},
                           ensure_ascii=True, allow_nan=False) + '\n').encode('utf-8')
        if len(line) > MAX_REQUEST:
            raise ValueError('platform_request_too_large')
        deadline = time.monotonic() + timeout
        fd = self._process.stdin.fileno()
        data = memoryview(line)
        while data:
            self._wait(fd, deadline, writing=True)
            try:
                written = os.write(fd, data)
            except BlockingIOError:
                continue
            if not written:
                raise _TransportError('platform_runtime_invalid_frame')
            data = data[written:]
        return self._receive(deadline, self._rpc_id)

    def _call(self, operation, arguments=None):
        with self._mutex:
            if self._closed:
                raise RuntimeError('platform_runtime_closed')
            if self._broken:
                self._stop_process()
            if self._process is None:
                self._start_process()
            try:
                return self._exchange(operation, arguments or {}, RPC_TIMEOUT)
            except (_TransportError, OSError):
                self._stop_process()
                raise

    def request(self, actor_id: int, method: str, path: str, payload: dict | None = None,
                event_id: str | None = None) -> dict:
        return self._call('request', {'actor_id': actor_id, 'method': method, 'path': path,
                                      'payload': payload, 'event_id': event_id})

    def profiles(self) -> list:
        return self._call('profiles')

    def schema(self) -> dict:
        return self._call('schema')

    def upload(self, actor_id: int, method: str, path: str, document: dict,
               event_id: str) -> dict:
        return self._call('upload', {'actor_id': actor_id, 'method': method, 'path': path,
                                     'document': document, 'event_id': event_id})

    def diagnostics(self) -> dict:
        return self._call('diagnostics')

    def claim_native_invite(self, capability: str, event_id: str) -> dict:
        """Private IPC only; the MAX gateway owns the claimant/capability lock."""
        return self._call('claim_native_invite', {'capability':capability,'event_id':event_id})

    def authorize(self, actor_id: int) -> dict:
        """Never cached: verify native membership before serving outer state."""
        return self._call('authorize',{'actor_id':actor_id})

    def close(self):
        with self._mutex:
            if self._closed:
                return
            if self._process is not None and not self._broken:
                try:
                    self._exchange('close', {}, SHUTDOWN_TIMEOUT)
                    self._process.wait(timeout=SHUTDOWN_TIMEOUT)
                except Exception:
                    pass
            self._stop_process()
            os.close(self._lock_fd)
            self._closed = True
