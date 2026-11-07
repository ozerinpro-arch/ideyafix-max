"""Private PostgreSQL rehearsal of the staff replacement transactions."""
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from stat import S_ISDIR, S_ISSOCK

from fastapi import HTTPException

from .test_isolation import isolated_database_paths
from .workspace import verify_school_state


SCHOOL = 'synthetic-school'
PORT = 5432
USER = 'publication_lab'
UNAVAILABLE = 'Хранилище замены недоступно. Повторите тот же запрос позже.'


def _private_directory(path):
    if not path.is_absolute() or path.resolve(strict=True) != path or not path.is_relative_to(Path('/tmp').resolve()):
        raise ValueError('Expected a canonical temporary directory')
    info = path.stat()
    if not S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError('Expected a private temporary directory')


class _Row:
    def __init__(self, values, positions):
        self.values = values
        self.positions = positions

    def __getitem__(self, key):
        return self.values[self.positions[key] if isinstance(key, str) else key]

    def __iter__(self):
        return iter(self.values)

    def keys(self):
        return self.positions.keys()


def _row_factory(cursor):
    positions = {column.name: index for index, column in enumerate(cursor.description or ())}
    return lambda values: _Row(values, positions)


class _Connection:
    def __init__(self, connection, driver_error, expected_queue_identity=None):
        self.connection = connection
        self.driver_error = driver_error
        self.expected_queue_identity = expected_queue_identity

    def execute(self, sql, params=()):
        try:
            if sql == 'BEGIN':
                transaction = self.connection.execute('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
                self.validate_domain()
                return transaction
            if params:
                # The shared replacement SQL uses qmark parameters. Values
                # remain bound by psycopg and never enter the SQL text.
                return self.connection.execute(sql.replace('?', '%s'), params)
            return self.connection.execute(sql)
        except self.driver_error:
            raise HTTPException(503, UNAVAILABLE) from None

    def table_exists(self, name):
        return self.execute(
            "SELECT 1 FROM pg_catalog.pg_tables WHERE schemaname = 'public' AND tablename = ?", (name,)
        ).fetchone() is not None

    def validate_domain(self):
        # Source settings would resolve the real school's bundled source package.
        if self.expected_queue_identity is None:
            matches = self.execute('SELECT 1 FROM rehearsal_sqlite_metadata WHERE id=1').fetchone() is not None
        else:
            matches = self.queue_identity() == self.expected_queue_identity
        if not matches or self.execute('SELECT 1 FROM planning_source_settings LIMIT 1').fetchone() is not None:
            raise HTTPException(503, UNAVAILABLE)

    def queue_identity(self):
        row = self.execute('''SELECT control.system_identifier::text, pg_catalog.current_database(), marker.payload
            FROM public.rehearsal_sqlite_metadata AS marker CROSS JOIN pg_catalog.pg_control_system() AS control
            WHERE marker.id=1''').fetchone()
        if row is None or not isinstance(row[2], str) or not row[2]:
            raise HTTPException(503, UNAVAILABLE)
        return sha256(json.dumps([SCHOOL, *row], ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()

    def close(self):
        self.connection.close()


@dataclass(frozen=True)
class ReplacementPostgres:
    """Select the rehearsal only through a private socket and an isolated app factory."""

    socket_dir: Path
    _expected_queue_identity: str | None = field(default=None, repr=False, kw_only=True)

    def queue_identity(self):
        """Derive the queue scope on the originating host, or verify a bound scope."""
        with closing(self.read_connection()) as connection:
            connection.execute('BEGIN')
            return self._expected_queue_identity or connection.queue_identity()

    def bind_queue_identity(self, expected):
        """Pin a scope supplied by the originating host without consulting the current socket."""
        if (not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected)
                or self._expected_queue_identity not in (None, expected)):
            raise ValueError('Invalid rehearsal queue identity')
        return ReplacementPostgres(self.socket_dir, _expected_queue_identity=expected)

    def validate_staff(self, school, scheme, host, staff_path):
        if (school != SCHOOL or scheme != 'http' or host not in {'localhost', '127.0.0.1'}
                or os.environ.get('SHKOLA_LOCAL_RUNTIME_MODE') == 'persistent'):
            raise ValueError('PostgreSQL rehearsal requires isolated synthetic staff')
        isolated_database_paths()
        _private_directory(staff_path.parent)
        _private_directory(self.socket_dir)
        try:
            from psycopg import Error
        except ImportError:
            raise RuntimeError('Install the pinned PostgreSQL driver for the rehearsal') from None

    def _connect(self):
        import psycopg

        try:
            _private_directory(self.socket_dir)
            info = (self.socket_dir / f'.s.PGSQL.{PORT}').lstat()
            if (not S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077
                    or any(key.startswith('PG') for key in os.environ)):
                raise ValueError('Expected a private PostgreSQL socket and explicit connection parameters')
            connection = psycopg.connect(
                host=str(self.socket_dir), port=PORT, user=USER, dbname='postgres',
                sslmode='disable', gssencmode='disable', connect_timeout=3, autocommit=True,
                options='-c search_path=public -c lock_timeout=4000 -c statement_timeout=8000',
                row_factory=_row_factory,
            )
            if self._expected_queue_identity is not None:
                try:
                    _Connection(connection, psycopg.Error, self._expected_queue_identity).validate_domain()
                except BaseException:
                    connection.close()
                    raise
            return connection
        except (OSError, ValueError, psycopg.Error):
            raise HTTPException(503, UNAVAILABLE) from None

    def read_connection(self):
        from psycopg import Error

        return _Connection(self._connect(), Error, self._expected_queue_identity)

    @contextmanager
    def locked_domain(self):
        from psycopg import Error

        try:
            with self._connect() as connection, connection.transaction():
                domain = _Connection(connection, Error, self._expected_queue_identity)
                row = domain.execute('SELECT payload FROM demo_state WHERE id=1 FOR UPDATE').fetchone()
                if row is None:
                    raise HTTPException(503, UNAVAILABLE)
                domain.validate_domain()
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
