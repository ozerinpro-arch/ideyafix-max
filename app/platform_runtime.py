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

PROJECT = Path(__file__).resolve().parent.parent
ENGINE = PROJECT / 'platform_engine'
MAX_FRAME = 64 * 1024 * 1024


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
        self._lock_fd = os.open(self.root / '.rpc.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        lock_info = os.fstat(self._lock_fd)
        if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_nlink != 1 or lock_info.st_uid != os.getuid():
            os.close(self._lock_fd); raise ValueError('invalid_platform_runtime_lock')
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self._lock_fd); raise ValueError('platform_runtime_already_running') from None
        env = {'PATH': os.defpath, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHON_DOTENV_DISABLED': '1'}
        config_path=Path(nanogpt_config).absolute() if nanogpt_config is not None else None
        if config_path is not None and not (config_path.is_relative_to(PROJECT/'runtime') or any(
                config_path.is_relative_to(Path(p)) for p in ('/private/tmp','/var/tmp','/private/var/folders'))):
            os.close(self._lock_fd)
            raise ValueError('provider_config_must_be_independent')
        try:
            self._process = subprocess.Popen([sys.executable, '-I', '-B', str(ENGINE/'runtime_child.py'), str(self.root),str(config_path) if config_path else ''],
                cwd=ENGINE, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding='utf-8', bufsize=1)
            self._receive()
        except BaseException:
            try:
                if hasattr(self,'_process'):
                    self._process.terminate()
                    try:self._process.wait(timeout=10)
                    except subprocess.TimeoutExpired:self._process.kill();self._process.wait(timeout=10)
            finally:
                if hasattr(self,'_process'):
                    self._process.stdin.close();self._process.stdout.close()
                os.close(self._lock_fd)
                self._closed = True
            raise

    def _receive(self):
        if not select.select([self._process.stdout], [], [], 180)[0]:
            raise RuntimeError('platform_runtime_timeout')
        line = self._process.stdout.readline(MAX_FRAME+1)
        if not line or len(line.encode('utf-8')) > MAX_FRAME:
            raise RuntimeError('platform_runtime_invalid_frame')
        value = json.loads(line)
        if value.get('ok') is not True:
            raise RuntimeError('platform_runtime_operation_failed:' + str(value.get('error', 'unknown')))
        return value.get('value')

    def _call(self, operation, arguments=None):
        with self._mutex:
            if self._closed:
                raise RuntimeError('platform_runtime_closed')
            line = json.dumps({'operation': operation, 'arguments': arguments or {}}, ensure_ascii=True, allow_nan=False)+'\n'
            if len(line.encode()) > 16*1024*1024:
                raise ValueError('platform_request_too_large')
            self._process.stdin.write(line); self._process.stdin.flush()
            return self._receive()

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
            if self._closed: return
            try:
                self._call('close')
                self._process.wait(timeout=10)
            except Exception:
                self._process.terminate()
                try: self._process.wait(timeout=10)
                except subprocess.TimeoutExpired: self._process.kill(); self._process.wait(timeout=10)
            finally:
                self._closed = True
                self._process.stdin.close(); self._process.stdout.close()
                os.close(self._lock_fd)
