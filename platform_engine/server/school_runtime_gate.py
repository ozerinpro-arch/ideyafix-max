"""Cooperative single-host maintenance gate; only OS locks establish ownership."""
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
import stat

from fastapi import HTTPException


UNAVAILABLE = 'Школьный контур временно недоступен.'
FENCE_NAME = 'school-runtime.fenced.json'
_ERRORS = (OSError, ValueError, RuntimeError, TypeError)


def _identity(info):
    return info.st_dev, info.st_ino


def _parent(path, expected=None):
    parent = path.parent
    if not path.is_absolute() or parent.resolve(strict=True) != parent:
        raise ValueError('Noncanonical runtime directory')
    info = parent.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700
            or expected is not None and _identity(info) != expected):
        raise ValueError('Unsafe runtime directory')
    return _identity(info)


def _checked(descriptor, path, parent_identity, lock_identity=None):
    _parent(path, parent_identity)
    opened, current = os.fstat(descriptor), path.lstat()
    for info in (opened, current):
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise ValueError('Unsafe runtime lock')
    identity = _identity(opened)
    if identity != _identity(current) or lock_identity is not None and identity != lock_identity:
        raise ValueError('Runtime lock was replaced')
    _parent(path, parent_identity)
    return identity


def _open(path, *, create=False):
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    if create:
        try:
            descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            try:
                os.fchmod(descriptor, 0o600)
            except BaseException:
                os.close(descriptor)
                raise
            return descriptor
    return os.open(path, flags)


def _failure(shared):
    if shared:
        raise HTTPException(503, UNAVAILABLE) from None
    raise RuntimeError(UNAVAILABLE) from None


def _unfenced(path):
    try:
        (path.parent / FENCE_NAME).lstat()
    except FileNotFoundError:
        return
    raise ValueError('Runtime is fenced')


@dataclass(frozen=True)
class RuntimeGate:
    path: Path
    _parent_identity: tuple[int, int]
    _lock_identity: tuple[int, int]

    @classmethod
    def for_staff(cls, staff_path):
        descriptor = None
        try:
            staff_path = Path(staff_path)
            if not staff_path.is_absolute() or staff_path.resolve(strict=False) != staff_path:
                raise ValueError('Noncanonical staff path')
            path = staff_path.parent / 'school-runtime.lock'
            parent_identity = _parent(path)
            descriptor = _open(path, create=True)
            lock_identity = _checked(descriptor, path, parent_identity)
            return cls(path, parent_identity, lock_identity)
        except _ERRORS:
            _failure(False)
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    _failure(False)

    def shared(self):
        # Independent shared descriptors remain compatible, including nested calls.
        return self._hold(True)

    def exclusive(self):
        return self._hold(False)

    @contextmanager
    def _hold(self, shared):
        descriptor, held = None, False
        try:
            try:
                _parent(self.path, self._parent_identity)
                if shared:
                    _unfenced(self.path)
                descriptor = _open(self.path)
                _checked(descriptor, self.path, self._parent_identity, self._lock_identity)
                fcntl.flock(descriptor, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
                held = True
                _checked(descriptor, self.path, self._parent_identity, self._lock_identity)
                if shared:
                    _unfenced(self.path)
            except _ERRORS:
                _failure(shared)
            try:
                yield self
            finally:
                try:
                    _checked(descriptor, self.path, self._parent_identity, self._lock_identity)
                except _ERRORS:
                    _failure(shared)
        finally:
            if descriptor is not None:
                try:
                    try:
                        if held:
                            fcntl.flock(descriptor, fcntl.LOCK_UN)
                    finally:
                        os.close(descriptor)
                except OSError:
                    _failure(shared)
