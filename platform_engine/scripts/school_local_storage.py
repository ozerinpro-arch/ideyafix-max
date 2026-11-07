"""Private persistent local-school storage, stopped-state backup and new-path restore.

Backups contain school/authentication databases. Keep them in the private local
storage root; they are never report artifacts or Obsidian exports.
"""
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
from uuid import uuid4

STORAGE_ROOT = Path.home() / 'Library/Application Support/ShkolaDeystvie'
DEFAULT_RUNTIME = STORAGE_ROOT / 'local-school'
DATABASES = ('domain.sqlite3', 'staff.sqlite3', 'agent.sqlite3', 'request-ai.sqlite3',
             'teacher-preparation.sqlite3', 'student-learning.sqlite3', 'replacement-materials.sqlite3')
ASSETS = ('derived-short-week-basis.json', 'reference-computational-seed.json')
FILES = DATABASES + ASSETS
LOCK = '.school-runtime.lock'
GENERATION_LOCK_DIRECTORIES = ('teacher-preparation.sqlite3.locks', 'student-learning.sqlite3.locks',
                               'request-ai.sqlite3.locks')


def _path(raw, *, allow_temporary=False):
    path = Path(raw)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('An explicit absolute local storage path is required')
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError('Symlinks are forbidden in local storage paths')
    roots = [STORAGE_ROOT]
    if allow_temporary:
        roots.extend([Path('/tmp').resolve(), Path(tempfile.gettempdir()).resolve()])
    if not any(path != root and path.is_relative_to(root) for root in roots):
        raise ValueError('Use the private ShkolaDeystvie storage root; temporary fixtures require explicit opt-in')
    return path


def _regular(path, *, private=False):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1
            or (private and stat.S_IMODE(info.st_mode) != 0o600)):
        raise ValueError('An owned regular local storage file is required')


def _directory(path, *, create=False):
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=False)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError('An owned private 0700 local storage directory is required')
    return path


def validate_runtime(raw=None, *, allow_temporary=False, create=False):
    path = _path(raw or DEFAULT_RUNTIME, allow_temporary=allow_temporary)
    return _directory(path, create=create)


def _present(path):
    allowed = set(FILES) | {LOCK} | set(GENERATION_LOCK_DIRECTORIES)
    allowed.update(name + suffix for name in DATABASES for suffix in ('-wal', '-shm', '-journal'))
    for child in path.iterdir():
        if child.name not in allowed:
            raise ValueError('Unknown files in local runtime; refuse an incomplete or secret-bearing backup')
        if child.name not in GENERATION_LOCK_DIRECTORIES:
            _regular(child)
    _generation_lock_files(path)
    names = []
    for name in FILES:
        if (path / name).exists():
            _regular(path / name)
            names.append(name)
    return names


def _generation_lock_files(path):
    files = []
    for name in GENERATION_LOCK_DIRECTORIES:
        directory = path / name
        if not directory.exists() and not directory.is_symlink():
            continue
        if directory.is_symlink():
            raise ValueError('Native generation lock directory symlinks are forbidden')
        _directory(directory)
        for child in directory.iterdir():
            if not re.fullmatch(r'[a-f0-9]{64}', child.name):
                raise ValueError('Unknown file in native generation lock directory')
            _regular(child, private=True)
            files.append(child)
    return files


def assert_empty_runtime(path):
    if any(child.name != LOCK for child in path.iterdir()):
        raise ValueError('Initialization requires a new empty runtime; existing school data are never re-imported')


def assert_resumable(path):
    names = _present(path)
    if not {'domain.sqlite3', 'staff.sqlite3'} <= set(names):
        raise ValueError('Existing domain and staff databases are required; restore a backup or explicitly initialize an empty runtime')
    return names


def assert_unprepared_domain(path):
    """Preparation may only import into the native empty school container."""
    database = path / 'domain.sqlite3'
    if not database.exists():
        raise ValueError('The initialized local backend is required before preparation')
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables != {'demo_state'}:
            raise ValueError('School preparation refuses an existing domain schema')
        row = connection.execute('SELECT payload FROM demo_state WHERE id=1').fetchone()
        from server.workspace import empty_school_state
        if not row or json.loads(row[0]) != empty_school_state():
            raise ValueError('School preparation refuses existing school records')


@contextmanager
def runtime_lock(path):
    lock = path / LOCK
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        _regular(lock, private=True)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Local school runtime is already running or being backed up') from None
        yield
    finally:
        os.close(descriptor)


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_private(path, payload):
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _check_sqlite(path):
    # Backup copies are closed and self-contained. Immutable reads must not
    # create WAL/SHM sidecars or change an already hashed private backup.
    with sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True) as connection:
        result = connection.execute('PRAGMA integrity_check').fetchall()
        if result != [('ok',)]:
            raise ValueError('SQLite integrity check failed')


def backup_runtime(source, destination=None, *, source_stopped=False, allow_temporary=False):
    if source_stopped is not True:
        raise ValueError('Stop the local backend and explicitly acknowledge --source-stopped first')
    source = validate_runtime(source, allow_temporary=allow_temporary)
    assert_resumable(source)
    destination = _path(destination or STORAGE_ROOT / 'backups' /
        (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:8]),
        allow_temporary=allow_temporary)
    if destination == source or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError('Backup and runtime must be separate directories')
    if destination.exists():
        raise ValueError('Backup destination already exists')
    with runtime_lock(source), ExitStack() as stack:
        names = _present(source)
        # Native commands retain hash-named lock files after completion. Their
        # receipts live in SQLite; only idle synchronization locks may be omitted.
        for file in _generation_lock_files(source):
            descriptor = os.open(file, os.O_RDWR | os.O_NOFOLLOW)
            stack.callback(os.close, descriptor)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('A native command is still active') from None
        # Hold all writer reservations together. Separate readers perform backup
        # of committed SQLite state, including WAL, without a raw file copy.
        for name in names:
            if name in DATABASES:
                connection = sqlite3.connect((source / name).as_uri() + '?mode=rw', uri=True, timeout=2)
                stack.callback(connection.close)
                connection.execute('BEGIN IMMEDIATE')
        _directory(destination, create=True)
        entries = []
        for name in names:
            old, new = source / name, destination / name
            if name in DATABASES:
                _write_private(new, b'')
                with sqlite3.connect(old.as_uri() + '?mode=ro', uri=True) as reader:
                    with sqlite3.connect(new) as writer:
                        reader.backup(writer)
                        writer.execute('PRAGMA journal_mode=DELETE').fetchone()
                _check_sqlite(new)
            else:
                payload = old.read_bytes()
                json.loads(payload)
                _write_private(new, payload)
            entries.append({'name': name, 'bytes': new.stat().st_size, 'sha256': _hash(new),
                            'kind': 'sqlite' if name in DATABASES else 'json'})
        manifest = {'version': 1, 'kind': 'school-local-private-backup',
            'createdAt': datetime.now(timezone.utc).isoformat(), 'sourceRuntime': str(source),
            'sourceStoppedAcknowledged': True, 'files': entries}
        _write_private(destination / 'manifest.json', (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode())
    return destination


def validate_backup(raw, *, allow_temporary=False):
    path = _path(raw, allow_temporary=allow_temporary)
    _directory(path)
    _regular(path / 'manifest.json', private=True)
    manifest = json.loads((path / 'manifest.json').read_bytes())
    if (not isinstance(manifest, dict) or manifest.get('version') != 1
            or manifest.get('kind') != 'school-local-private-backup'
            or not isinstance(manifest.get('files'), list)):
        raise ValueError('Unsupported local backup manifest')
    entries = manifest['files']
    names = [entry.get('name') for entry in entries if isinstance(entry, dict)]
    if (len(names) != len(entries) or not all(isinstance(name, str) for name in names)
            or len(set(names)) != len(names)
            or not {'domain.sqlite3', 'staff.sqlite3'} <= set(names) or not set(names) <= set(FILES)
            or {item.name for item in path.iterdir()} != set(names) | {'manifest.json'}):
        raise ValueError('Unknown, duplicate or missing backup files')
    for entry in entries:
        name = entry['name']
        file = path / name
        _regular(file, private=True)
        if (entry.get('bytes') != file.stat().st_size or entry.get('sha256') != _hash(file)
                or entry.get('kind') != ('sqlite' if name in DATABASES else 'json')):
            raise ValueError('Backup integrity hash or file metadata changed')
        if name in DATABASES:
            _check_sqlite(file)
        else:
            json.loads(file.read_bytes())
    return path, manifest


def restore_backup(backup, destination, *, allow_temporary=False):
    source, manifest = validate_backup(backup, allow_temporary=allow_temporary)
    destination = _path(destination, allow_temporary=allow_temporary)
    if destination.exists():
        raise ValueError('Restore requires a new destination; existing school data are never overwritten')
    if destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError('Restore and backup must be separate directories')
    _directory(destination, create=True)
    for entry in manifest['files']:
        file = destination / entry['name']
        _write_private(file, (source / entry['name']).read_bytes())
        if _hash(file) != entry['sha256']:
            raise ValueError('Restored file failed verification')
        if entry['name'] in DATABASES:
            _check_sqlite(file)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    backup = commands.add_parser('backup')
    backup.add_argument('--runtime-dir', default=str(DEFAULT_RUNTIME))
    backup.add_argument('--destination')
    backup.add_argument('--source-stopped', action='store_true')
    restore = commands.add_parser('restore')
    restore.add_argument('--backup', required=True)
    restore.add_argument('--destination', required=True)
    for command in (backup, restore):
        command.add_argument('--allow-temporary-runtime', action='store_true')
    args = parser.parse_args()
    if args.command == 'backup':
        path = backup_runtime(args.runtime_dir, args.destination, source_stopped=args.source_stopped,
                              allow_temporary=args.allow_temporary_runtime)
    else:
        path = restore_backup(args.backup, args.destination, allow_temporary=args.allow_temporary_runtime)
    print(json.dumps({'ready': True, 'command': args.command, 'path': str(path),
                      'privateLocalOnly': True, 'secretsInOutput': False}))


if __name__ == '__main__':
    main()
