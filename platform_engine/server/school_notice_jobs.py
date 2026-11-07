"""Queue binding for an explicitly configured, single-school PostgreSQL host."""
from contextlib import closing
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import sqlite3
import stat
from urllib.parse import urlsplit

from fastapi import HTTPException

from . import staff_access
from .integration_settings import _flag, _secret
from .notice_jobs import _endpoint, UNAVAILABLE
from .request_planning import digest
from .school_postgres import SchoolPostgres, SchoolPostgresSettings, _private_directory


# Celery 5.5 adds these after a clean worker startup. They do not select a
# broker, loader or backend. All inherited CELERY_* remain forbidden below.
_CELERY_LOGGING = frozenset({'CELERY_LOG_LEVEL', 'CELERY_LOG_FILE',
                           'CELERY_LOG_REDIRECT', 'CELERY_LOG_REDIRECT_LEVEL'})
_CELERY_CLI = frozenset({'APP', 'BROKER_URL', 'RESULT_BACKEND', 'LOADER',
                       'CONFIG_MODULE', 'SKIP_CHECKS'})


def staff_identity(school, path, origin):
    """Pin the existing authority file independently of pending job contents."""
    path = Path(path)
    parsed = urlsplit(origin)
    if (not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', school)
            or not path.is_absolute() or path.resolve(strict=True) != path
            or not parsed.hostname or parsed.username or parsed.password
            or parsed.path or parsed.query or parsed.fragment
            or not (parsed.scheme == 'https' or parsed.scheme == 'http' and parsed.hostname == '127.0.0.1')):
        raise ValueError('Expected an explicit school authority and protected origin')
    _private_directory(path.parent)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise ValueError('Expected the private school authority file')
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as connection:
            staff_access._validate_existing(connection, school)
    except (sqlite3.Error, RuntimeError):
        raise ValueError('Expected an existing authority for this school') from None
    after = path.lstat()
    if (info.st_dev, info.st_ino) != (after.st_dev, after.st_ino):
        raise ValueError('School authority changed while checking its identity')
    return digest(['school-notice-staff-v1', school, str(path), origin, info.st_dev, info.st_ino])


def _broker(value):
    parsed = _endpoint(value, {'redis', 'rediss'})
    if (parsed.scheme == 'redis' and parsed.hostname != '127.0.0.1'
            or not re.fullmatch(r'/[0-9]+', parsed.path)):
        raise ValueError('Expected an authenticated protected Redis endpoint')
    return parsed


def _scope(school, storage, expected_staff, origin):
    return digest(['school-notices-v1', school, storage.settings.expected_domain, expected_staff, origin])


@dataclass(frozen=True)
class SchoolNoticeSettings:
    school: str
    scope: str
    redis: str = field(repr=False)
    storage: SchoolPostgres = field(repr=False)
    staff_path: Path
    staff_expected: str
    origin: str

    @classmethod
    def from_environment(cls, school, *, storage=None):
        if not _flag(os.environ, 'SHKOLA_NOTICE_QUEUE_ENABLED'):
            return None
        try:
            if any(key.startswith('CELERY_') or key in _CELERY_CLI for key in os.environ):
                raise ValueError('Inherited Celery configuration is forbidden')
            if os.environ.get('SHKOLA_NOTICE_REDIS_URL') or not os.environ.get('SHKOLA_NOTICE_REDIS_URL_FILE'):
                raise ValueError('Expected a private Redis secret file')
            if any(os.environ.get(key) for key in ('SHKOLA_NOTICE_PG_URL', 'SHKOLA_NOTICE_PG_URL_FILE')):
                raise ValueError('School jobs use the selected school database')
            if storage is not None and type(storage) is not SchoolPostgres:
                raise ValueError('Expected explicit school PostgreSQL storage')
            configured = SchoolPostgresSettings.from_environment(school)
            if configured is None or storage is not None and storage.settings != configured:
                raise ValueError('Expected the selected PostgreSQL domain')
            storage = storage or SchoolPostgres(configured)
            path, configured_school = staff_access.configuration()
            if configured_school != school or type(storage) is not SchoolPostgres:
                raise ValueError('Expected the configured school storage')
            origin = os.environ.get('SHKOLA_STAFF_ORIGIN', '')
            expected = os.environ.get('SHKOLA_NOTICE_STAFF_IDENTITY', '')
            value = cls(school, _scope(school, storage, expected, origin),
                        _secret(os.environ, 'SHKOLA_NOTICE_REDIS_URL', needed=True),
                        storage, path, expected, origin)
            value.validate()
            return value
        except (OSError, ValueError, RuntimeError, HTTPException):
            raise RuntimeError('Неверная защищённая настройка школьной очереди PostgreSQL') from None

    def validate(self):
        try:
            if (type(self.storage) is not SchoolPostgres or self.storage.settings.school != self.school
                    or not re.fullmatch(r'[a-f0-9]{64}', self.staff_expected)
                    or staff_access.configuration() != (self.staff_path, self.school)
                    or os.environ.get('SHKOLA_STAFF_ORIGIN', '') != self.origin
                    or any(key.startswith('PG') or key in _CELERY_CLI
                           or key.startswith('CELERY_') and key not in _CELERY_LOGGING
                           for key in os.environ)
                    or self.scope != _scope(self.school, self.storage, self.staff_expected, self.origin)
                    or staff_identity(self.school, self.staff_path, self.origin) != self.staff_expected):
                raise ValueError('School queue authority changed')
            _broker(self.redis)
            self.storage.settings.connection_parameters()
            from .school_restore import require_activation
            require_activation(self.staff_path, self.school, self.storage.settings.expected_domain,
                             self.origin, self.storage.settings.activation_id)
        except (OSError, ValueError, TypeError, AttributeError, RuntimeError, sqlite3.Error):
            raise HTTPException(503, UNAVAILABLE) from None

    def connect(self):
        self.validate()
        return self.storage.notice_connection()
