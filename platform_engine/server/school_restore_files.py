"""Explicit private SQLite sidecars for a stopped school PostgreSQL restore.

The caller holds the exclusive RuntimeGate and PostgreSQL table locks. This
module does not configure the application, read credentials or copy other files.
"""
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass, field
import fcntl
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import stat

from scripts.school_local_storage import _generation_lock_files
from . import staff_access
from .integration_storage import _check_schema
from .student_learning import StudentLearningService, TABLES as LEARNING_TABLES
from .teacher_preparation import TeacherPreparationService, TABLES as PREPARATION_TABLES


NAMES = ('staff.sqlite3', 'teacher-preparation.sqlite3', 'student-learning.sqlite3')
OPTIONAL_NAMES = ('max.sqlite3',)
ALL_NAMES = NAMES + OPTIONAL_NAMES
_LOCK_DIRS = {name + '.locks' for name in NAMES[1:]}


def _canonical(raw):
    path = Path(raw)
    if not path.is_absolute() or '..' in path.parts or path.resolve(strict=True) != path:
        raise ValueError('An existing canonical absolute sidecar path is required')
    return path


def _directory(raw, *, empty=False):
    path = _canonical(raw)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700 or empty and any(path.iterdir())):
        raise ValueError('An owned private 0700 sidecar directory is required')
    return path


def _regular(raw):
    path = _canonical(raw)
    _directory(path.parent)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise ValueError('An owned private single-link 0600 sidecar file is required')
    return info.st_dev, info.st_ino


def _metadata(path):
    identity = _regular(path)
    for suffix in ('-wal', '-shm', '-journal'):
        companion = path.with_name(path.name + suffix)
        if companion.exists() or companion.is_symlink():
            _regular(companion)
    return identity


def _school(school):
    if not isinstance(school, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', school):
        raise ValueError('An explicit school ID is required')


def _validate(connection, name, school):
    connection.execute('PRAGMA trusted_schema=OFF')
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type IN ('view','trigger') "
                          "OR upper(sql) LIKE 'CREATE VIRTUAL TABLE%' LIMIT 1").fetchone():
        raise ValueError('Executable sidecar schema objects are forbidden')
    if connection.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
        raise ValueError('Sidecar integrity check failed')
    if name == NAMES[0]:
        staff_access._validate_existing(connection, school)
    elif name == NAMES[1]:
        _check_schema(connection, PREPARATION_TABLES)
        service = object.__new__(TeacherPreparationService)
        service.school_id = school
        service._validate_db(connection)
    elif name == NAMES[2]:
        _check_schema(connection, LEARNING_TABLES)
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables != LEARNING_TABLES:
            raise ValueError('An initialized learning sidecar is required')
        service = object.__new__(StudentLearningService)
        service.school = school
        service._load(connection)
    elif name == 'max.sqlite3':
        from .school_max_store import validate_snapshot
        validate_snapshot(connection, school=school)
    else:
        raise ValueError('Unknown school sidecar name')


def validate_sidecar(path, name, school):
    """Validate one explicit native store without configuration or schema writes."""
    _school(school)
    if name not in ALL_NAMES:
        raise ValueError('Unknown school sidecar name')
    path = Path(path)
    identity = _metadata(path)
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=0)) as connection:
        _validate(connection, name, school)
    if _metadata(path) != identity:
        raise ValueError('Sidecar identity changed while validating')


@dataclass(frozen=True)
class SidecarFile:
    name: str
    path: Path
    identity: tuple[int, int]


@dataclass
class SidecarInventory:
    school: str
    staff_path: Path
    files: tuple[SidecarFile, ...]
    active: bool = False
    _lock_paths: tuple[Path, ...] = field(default=(), repr=False)


def _generation_files(parent):
    return tuple(sorted(path for path in _generation_lock_files(parent) if path.parent.name in _LOCK_DIRS))


@contextmanager
def locked_sidecars(staff_path, school):
    """Hold all native generation locks and SQLite writer reservations together."""
    _school(school)
    staff_path = Path(staff_path)
    if staff_path.name != NAMES[0]:
        raise ValueError('The explicit staff.sqlite3 sidecar is required')
    _metadata(staff_path)
    parent = staff_path.parent
    files = tuple(SidecarFile(name, parent / name, _metadata(parent / name)) for name in ALL_NAMES
                  if name == NAMES[0] or (parent / name).exists() or (parent / name).is_symlink())
    locks = _generation_files(parent)
    inventory = SidecarInventory(school, staff_path, files, _lock_paths=locks)
    with ExitStack() as stack:
        for path in locks:
            identity = _regular(path)
            descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
            stack.callback(os.close, descriptor)
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != identity:
                raise ValueError('Native generation lock identity changed')
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('A native sidecar generation is still active') from None
        connections = []
        for file in files:
            connection = sqlite3.connect(file.path.as_uri() + '?mode=rw', uri=True, timeout=0)
            stack.callback(connection.close)
            try:
                connection.execute('BEGIN IMMEDIATE')
            except sqlite3.OperationalError:
                raise ValueError('A sidecar writer is still active') from None
            connections.append((file, connection))
        for file, connection in connections:
            if _metadata(file.path) != file.identity:
                raise ValueError('Sidecar identity changed while locking')
            _validate(connection, file.name, school)
        inventory.active = True
        try:
            yield inventory
        finally:
            inventory.active = False


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _entry(path):
    return dict(name=path.name, kind='sqlite', bytes=path.stat().st_size, sha256=_hash(path))


def _copy(source, target):
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(descriptor)
    try:
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=0)) as reader, \
                closing(sqlite3.connect(target)) as writer:
            reader.backup(writer)
            writer.execute('PRAGMA journal_mode=DELETE').fetchone()
    except BaseException:
        _remove_created([target])
        raise


def _remove_created(paths):
    for path in reversed(paths):
        for suffix in ('-wal', '-shm', '-journal', ''):
            candidate = path.with_name(path.name + suffix)
            if candidate.exists():
                candidate.unlink()


def copy_sidecars(inventory, destination_existing_emptyprivate):
    if not isinstance(inventory, SidecarInventory) or not inventory.active:
        raise ValueError('A live locked sidecar inventory is required')
    destination = _directory(destination_existing_emptyprivate, empty=True)
    parent = inventory.staff_path.parent
    if destination == parent or destination.is_relative_to(parent) or parent.is_relative_to(destination):
        raise ValueError('Source and backup sidecars must be separate directories')
    present = tuple(name for name in ALL_NAMES if (parent / name).exists() or (parent / name).is_symlink())
    if present != tuple(file.name for file in inventory.files) or _generation_files(parent) != inventory._lock_paths:
        raise ValueError('Sidecar inventory changed after locking')
    for file in inventory.files:
        if _metadata(file.path) != file.identity:
            raise ValueError('Sidecar identity changed after locking')
    created = []
    try:
        for file in inventory.files:
            target = destination / file.name
            _copy(file.path, target)
            created.append(target)
            validate_sidecar(target, file.name, inventory.school)
        return [_entry(path) for path in created]
    except BaseException:
        _remove_created(created)
        raise


def restore_sidecars(bundle, destination_existingprivate, entries, school):
    """Copy validated fixed stores, then invalidate authentication before activation."""
    _school(school)
    bundle, destination = _directory(bundle), _directory(destination_existingprivate)
    if bundle == destination or bundle.is_relative_to(destination) or destination.is_relative_to(bundle):
        raise ValueError('Bundle and restored sidecars must be separate directories')
    if not isinstance(entries, list) or not entries:
        raise ValueError('An explicit sidecar manifest is required')
    names = [entry.get('name') if isinstance(entry, dict) else None for entry in entries]
    if (any(name not in ALL_NAMES for name in names) or len(set(names)) != len(names)
            or NAMES[0] not in names):
        raise ValueError('Unknown, duplicate or missing sidecar manifest names')
    for entry in entries:
        name = entry['name']
        source, target = bundle / name, destination / name
        if any((destination / (name + suffix)).exists() or (destination / (name + suffix)).is_symlink()
               for suffix in ('', '-wal', '-shm', '-journal')):
            raise ValueError('Restored sidecar targets must be absent')
        validate_sidecar(source, name, school)
        if (entry.get('kind') != 'sqlite' or type(entry.get('bytes')) is not int
                or entry['bytes'] != source.stat().st_size or entry.get('sha256') != _hash(source)):
            raise ValueError('Sidecar manifest integrity changed')
    created, restored = [], []
    counts = dict(sessionsInvalidated=0, preauthInvalidated=0, invitationsInvalidated=0)
    try:
        for entry in entries:
            name = entry['name']
            target = destination / name
            _copy(bundle / name, target)
            created.append(target)
            if name == NAMES[0]:
                with closing(sqlite3.connect(target, timeout=0)) as connection:
                    connection.execute('BEGIN IMMEDIATE')
                    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if 'staff_sessions' in tables:
                        counts['sessionsInvalidated'] = connection.execute('SELECT count(*) FROM staff_sessions').fetchone()[0]
                        counts['preauthInvalidated'] = connection.execute('SELECT count(*) FROM staff_sessions WHERE member_id IS NULL').fetchone()[0]
                        connection.execute('DELETE FROM staff_sessions')
                    if 'staff_invites' in tables:
                        counts['invitationsInvalidated'] = connection.execute('SELECT count(*) FROM staff_invites').fetchone()[0]
                        connection.execute('DELETE FROM staff_invites')
                    connection.commit()
            validate_sidecar(target, name, school)
            restored.append(dict(name=name, sourceSha256=entry['sha256'], restoredSha256=_hash(target),
                                 bytes=target.stat().st_size))
        return {**counts, 'files': restored}
    except BaseException:
        _remove_created(created)
        raise
