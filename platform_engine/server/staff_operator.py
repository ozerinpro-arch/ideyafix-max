"""Explicit local provisioning of the approved planner role; no HTTP/startup hook.

Run as the owner of the separately configured private staff DB. A root operator
uses the service account for a service-owned DB; existing ownership guards are
not weakened. Credentials are generated internally and written only to a NEW
private file. Neither arguments, environment nor stdout accept/contain passwords.

After a planner is provisioned into an empty DB, the existing director bootstrap
correctly refuses the nonempty DB. Assigning a first director then requires a
separately approved operator procedure; this command never grants director.
"""
from contextlib import closing
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
from uuid import uuid4

from . import staff_access as store


class ProvisionError(ValueError):
    """Sanitized code only; never credentials or DB/provider exception text."""


def _output_path(raw, config):
    try:
        path = Path(raw)
        if not path.is_absolute() or '..' in path.parts:
            raise ValueError()
        if any(part.is_symlink() for part in (path, *path.parents)):
            raise ValueError()
        if path.exists():
            raise ProvisionError('credential_output_exists')
        parent = path.parent.stat()
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid() or parent.st_mode & 0o077:
            raise ValueError()
        protected = [config[0], *(Path(value) for value in (
            os.environ.get('SHKOLA_DB_PATH'), os.environ.get('AGENT_DB_PATH')) if value)]
        if any(path.resolve() == other.resolve() for other in protected):
            raise ValueError()
        return path
    except ProvisionError:
        raise
    except (TypeError, ValueError, OSError):
        raise ProvisionError('invalid_private_credential_output') from None


def _write_credentials(descriptor, credentials):
    raw = (json.dumps(credentials, ensure_ascii=False) + '\n').encode('utf-8')
    remaining = memoryview(raw)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError('Credential file write failed')
        remaining = remaining[written:]
    os.fsync(descriptor)


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def provision_planner(username: str, name: str, output_file) -> dict:
    """Create one planner, never replace/reset an existing identity.

    The private credential file is durable before the SQLite commit. A process
    kill between those operations can leave an orphan private file without an
    account; rerun refuses to overwrite it. The operator must inspect that case.
    There is no interval with a committed account and unwritten credentials.
    """
    if not isinstance(username, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_.-]{2,79}', username):
        raise ProvisionError('invalid_planner_username')
    if (not isinstance(name, str) or not name.strip() or len(name) > 120
            or any(ord(char) < 32 or 0x7f <= ord(char) <= 0x9f for char in name)):
        raise ProvisionError('invalid_planner_name')
    config = store.configuration()
    target = _output_path(output_file, config)
    # Reject a duplicate before any schema initialization of an existing file.
    if config[0].exists():
        try:
            with closing(sqlite3.connect(config[0].as_uri()+'?mode=ro', uri=True)) as connection:
                store._validate_existing(connection, config[1])
                if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='staff_members'").fetchone():
                    if connection.execute('SELECT 1 FROM staff_members WHERE username=?', (username,)).fetchone():
                        raise ProvisionError('planner_username_exists')
        except ProvisionError:
            raise
        except (RuntimeError, OSError, sqlite3.Error):
            raise ProvisionError('invalid_staff_configuration') from None
    descriptor = None
    created_identity = None
    committed = False
    commit_started = False
    with store.bound_configuration(config):
        store.initialize()
        try:
            with closing(store.connect()) as connection:
                try:
                    connection.execute('BEGIN IMMEDIATE')
                    store._validate_existing(connection, config[1])
                    if connection.execute('SELECT 1 FROM staff_members WHERE username=?', (username,)).fetchone():
                        raise ProvisionError('planner_username_exists')
                    _output_path(target, config)
                    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    info = os.fstat(descriptor)
                    created_identity = (info.st_dev, info.st_ino)
                    # fchmod makes the promised mode independent of a restrictive umask.
                    os.fchmod(descriptor, 0o600)
                    password = secrets.token_urlsafe(32)
                    encoded = store.password_hash(password)
                    identifier = str(uuid4())
                    connection.execute('INSERT INTO staff_members VALUES(?,?,?,?,?,?,?,?,1,1)',
                        (identifier, config[1], username, name.strip(), 'planner', '[]', '[]', encoded))
                    store.audit(connection, 'operator:'+str(os.geteuid()), 'operator_provision_planner', identifier)
                    _write_credentials(descriptor, {'username':username, 'password':password,
                        'name':name.strip(), 'role':'planner'})
                    _sync_directory(target.parent)
                    # A commit failure/death may be ambiguous. Preserve the durable
                    # private credentials then; never erase a possibly live login.
                    commit_started = True
                    connection.commit()
                    committed = True
                except BaseException:
                    connection.rollback()
                    raise
            return {'created':1, 'role':'planner'}
        except ProvisionError:
            raise
        except Exception:
            raise ProvisionError('planner_provision_failed') from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if created_identity is not None and not committed and not commit_started:
                try:
                    info = target.lstat()
                    if (info.st_dev, info.st_ino) == created_identity and not stat.S_ISLNK(info.st_mode):
                        target.unlink()
                        _sync_directory(target.parent)
                except OSError:
                    # Never remove a replacement path or mask the original error.
                    pass


def main(argv=None):
    class SafeParser(argparse.ArgumentParser):
        def error(self, message):
            # Never echo an accidental unsupported --password/value argument.
            self.exit(2, json.dumps({'created':0, 'role':'planner', 'result':'FAIL'})+'\n')
    parser = SafeParser(description='Явно создать один одобренный аккаунт завуча (только planner)')
    parser.add_argument('--username', required=True)
    parser.add_argument('--name', default='Завуч')
    parser.add_argument('--output', required=True, help='Новый абсолютный файл credentials в собственном каталоге 0700')
    args = parser.parse_args(argv)
    try:
        result = provision_planner(args.username, args.name, args.output)
    except (ProvisionError, RuntimeError, OSError, sqlite3.Error):
        print(json.dumps({'created':0, 'role':'planner', 'result':'FAIL'}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
