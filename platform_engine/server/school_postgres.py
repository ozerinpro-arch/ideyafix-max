"""Explicit single-school PostgreSQL domain. Schema provisioning is operator-owned."""
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import HTTPException

from .integration_settings import _secret
from .replacement_postgres import _Connection, _row_factory, UNAVAILABLE
from .request_planning import digest
from .workspace import verify_school_state


def domain_identity(school, domain_id, restore_epoch, database):
    return digest(['school-pg-v1', school, str(UUID(str(domain_id))), str(UUID(str(restore_epoch))), database])


def _private_directory(path):
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError('Expected a canonical private school directory')
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError('Expected an owned private school directory')


@dataclass(frozen=True)
class SchoolPostgresSettings:
    school: str
    url: str = field(repr=False)
    expected_domain: str
    root_ca: str = ''
    activation_id: str = ''

    @classmethod
    def from_environment(cls, school):
        backend = os.environ.get('SHKOLA_DOMAIN_BACKEND', '').strip()
        if backend in {'', 'sqlite'}:
            return None
        if backend != 'postgresql':
            raise ValueError('Unknown school domain backend')
        value = cls(school, _secret(os.environ, 'SHKOLA_SCHOOL_PG_URL', needed=True),
                    os.environ.get('SHKOLA_SCHOOL_PG_EXPECTED_DOMAIN', ''),
                    os.environ.get('SHKOLA_SCHOOL_PG_CA_FILE', ''),
                    os.environ.get('SHKOLA_RESTORE_ACTIVATION_ID', ''))
        value.connection_parameters()
        return value

    def connection_parameters(self):
        from psycopg.conninfo import conninfo_to_dict
        from psycopg import ProgrammingError

        if (not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', self.school)
                or not re.fullmatch(r'[a-f0-9]{64}', self.expected_domain)
                or any(key.startswith('PG') for key in os.environ)):
            raise ValueError('Expected explicit school and PostgreSQL identity')
        if self.activation_id and str(UUID(self.activation_id)) != self.activation_id:
            raise ValueError('Expected an explicit restore activation identifier')
        uri = urlsplit(self.url)
        if uri.scheme != 'postgresql' or uri.query or uri.fragment:
            raise ValueError('Expected a single explicit PostgreSQL URI')
        try:
            values = conninfo_to_dict(self.url)
            if set(values) != {'host', 'port', 'user', 'password', 'dbname'}:
                raise ValueError()
            if (not all(isinstance(value, str) and value for value in values.values())
                    or not 1 <= int(values['port']) <= 65535 or ',' in values['host']
                    or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', values['dbname'])):
                raise ValueError()
        except (ProgrammingError, ValueError, TypeError):
            raise ValueError('Expected explicit PostgreSQL host, port, database and credentials') from None
        host = values['host']
        local = host in {'127.0.0.1', '::1'} or host.startswith('/')
        if host.startswith('/'):
            _private_directory(Path(host))
        elif not re.fullmatch(r'[A-Za-z0-9.:-]+', host):
            raise ValueError('Invalid PostgreSQL host')
        parameters = {**values, 'sslmode': 'disable' if local else 'verify-full', 'gssencmode': 'disable'}
        if not local:
            ca = Path(self.root_ca)
            if not ca.is_absolute() or ca.resolve(strict=True) != ca:
                raise ValueError('Expected an explicit trusted PostgreSQL CA file')
            info = ca.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
                raise ValueError('Unsafe PostgreSQL CA file')
            parameters['sslrootcert'] = str(ca)
        return parameters


class _SchoolConnection(_Connection):
    requires_provisioned_schema = True

    def __init__(self, connection, driver_error, settings):
        super().__init__(connection, driver_error, settings.expected_domain)
        self.settings = settings

    def queue_identity(self, *, lock=False):
        from psycopg.rows import tuple_row
        try:
            with self.connection.cursor(row_factory=tuple_row) as cursor:
                row = cursor.execute('''SELECT school_id, domain_id::text, restore_epoch::text,
                    schema_version, current_database(), to_jsonb(marker)->>'activation_id'
                    FROM public.school_domain_metadata marker WHERE id=1'''
                    + (' FOR SHARE' if lock else '')).fetchone()
        except self.driver_error:
            raise HTTPException(503, UNAVAILABLE) from None
        if (row is None or row[0] != self.settings.school or row[3] != 1
                or row[5] != (self.settings.activation_id or None)):
            raise HTTPException(503, UNAVAILABLE)
        try:
            return domain_identity(row[0], row[1], row[2], row[4])
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(503, UNAVAILABLE) from None

    def validate_domain(self, *, lock=False):
        if self.queue_identity(lock=lock) != self.settings.expected_domain:
            raise HTTPException(503, UNAVAILABLE)


@dataclass(frozen=True)
class SchoolPostgres:
    settings: SchoolPostgresSettings

    def queue_identity(self):
        with closing(self.read_connection()) as connection:
            connection.execute('BEGIN')
            return connection.queue_identity()

    def bind_queue_identity(self, expected):
        if expected != self.settings.expected_domain:
            raise ValueError('School queue refers to a different PostgreSQL domain')
        return self

    def validate_staff(self, school, scheme, host, staff_path):
        if school != self.settings.school or not (scheme == 'https' or scheme == 'http' and host == '127.0.0.1'):
            raise ValueError('Expected the configured school and a protected staff origin')
        _private_directory(staff_path.parent)
        from .school_restore import require_activation
        require_activation(staff_path, school, self.settings.expected_domain,
                         os.environ.get('SHKOLA_STAFF_ORIGIN', ''), self.settings.activation_id)
        self.settings.connection_parameters()
        self.queue_identity()

    def _connect(self, *, autocommit=True, row_factory=_row_factory, lock_domain=False):
        import psycopg

        connection = None
        try:
            parameters = self.settings.connection_parameters()
            if parameters['host'].startswith('/'):
                path = Path(parameters['host']) / ('.s.PGSQL.' + parameters['port'])
                info = path.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                    raise ValueError('Expected a private PostgreSQL socket')
            connection = psycopg.connect(**parameters, connect_timeout=3, autocommit=autocommit,
                options='-c search_path=public -c lock_timeout=4000 -c statement_timeout=8000', row_factory=row_factory)
            _SchoolConnection(connection, psycopg.Error, self.settings).validate_domain(lock=lock_domain)
            return connection
        except (OSError, RuntimeError, ValueError, psycopg.Error):
            if connection is not None:
                connection.close()
            raise HTTPException(503, UNAVAILABLE) from None
        except BaseException:
            if connection is not None:
                connection.close()
            raise

    def read_connection(self):
        from psycopg import Error
        return _SchoolConnection(self._connect(), Error, self.settings)

    def notice_connection(self):
        """Raw write transaction for jobs, locked to the same school marker."""
        from psycopg.rows import dict_row
        return self._connect(autocommit=False, row_factory=dict_row, lock_domain=True)

    @contextmanager
    def locked_domain(self):
        from psycopg import Error
        try:
            with self._connect() as connection, connection.transaction():
                domain = _SchoolConnection(connection, Error, self.settings)
                if domain.queue_identity(lock=True) != self.settings.expected_domain:
                    raise HTTPException(503, UNAVAILABLE)
                row = domain.execute('SELECT payload FROM demo_state WHERE id=1 FOR UPDATE').fetchone()
                if row is None:
                    raise HTTPException(503, UNAVAILABLE)
                state = json.loads(row[0])
                verify_school_state(state)
                yield domain, state
        except Error:
            raise HTTPException(503, UNAVAILABLE) from None

    @contextmanager
    def atomic_domain(self):
        with self.locked_domain() as (domain, state):
            before = json.dumps(state, ensure_ascii=False)
            yield domain, state
            after = json.dumps(state, ensure_ascii=False)
            if after != before:
                domain.execute('UPDATE demo_state SET payload=? WHERE id=1', (after,))
