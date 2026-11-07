"""Explicit private checkpoint and new-target restore for the PostgreSQL host.

Raw checkpoints contain authentication databases. They are not report artifacts.
Only independently retained manifest hashes may authorize an input checkpoint.
"""
from contextlib import closing
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from uuid import NAMESPACE_OID, UUID, uuid4, uuid5
from fastapi import HTTPException

from .integration_settings import _secret
from .notice_jobs import authority
from .request_planning import digest
from .school_notice_jobs import SchoolNoticeSettings, staff_identity, _scope
from .school_postgres import SchoolPostgres, SchoolPostgresSettings, _SchoolConnection, domain_identity, _private_directory
from .school_runtime_gate import RuntimeGate
from . import staff_access
from scripts.school_local_storage import _write_private, _regular, _check_sqlite

CORE = frozenset({'demo_state', 'timetable_draft', 'timetable_history', 'timetable_sources',
 'timetable_command_receipts', 'planning_config', 'planning_runs', 'planning_questions',
 'planning_run_requests', 'planning_run_receipts', 'planning_source_settings',
 'planning_source_receipts', 'school_publications', 'school_publication_audit',
 'school_publication_outbox', 'school_publication_receipts', 'replacement_material_state',
 'school_domain_metadata', 'shkola_notice_jobs'})
OPTIONAL = frozenset({'rehearsal_sqlite_metadata', 'shkola_document_jobs'})
SIDECARS = frozenset({'staff.sqlite3', 'teacher-preparation.sqlite3', 'student-learning.sqlite3', 'max.sqlite3'})
ACTIVATION = 'school-restore-state.json'
FENCE = 'school-runtime.fenced.json'
_RESTORE_LOCK = 0x5343484F4F4C5253


def _require(ok, message):
    if not ok:
        raise ValueError(message)


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sync_directory(path):
    _private_directory(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_file(path):
    _regular(path, private=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _new_path(raw):
    path = Path(raw)
    _require(path.is_absolute() and path.resolve(strict=False) == path, 'Expected a canonical new private path')
    _private_directory(path.parent)
    _require(not path.exists() and not path.is_symlink(), 'Destination already exists')
    return path


def _raw(settings, *, autocommit=False):
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(**settings.connection_parameters(), autocommit=autocommit,
        connect_timeout=3, row_factory=dict_row,
        options='-c search_path=public -c timezone=UTC -c synchronous_commit=on -c lock_timeout=2000 -c statement_timeout=15000')


def _tools(settings, name, args, *, server_major=None):
    parameters = settings.connection_parameters()
    directory = os.environ.get('SHKOLA_POSTGRES_BIN_DIRECTORY')
    executable = Path(directory) / name if directory else Path(shutil.which(name) or '')
    _require(executable.is_file() and os.access(executable, os.X_OK), 'Required PostgreSQL tool unavailable')
    executable = executable.resolve(strict=True)
    info = executable.stat()
    _require(info.st_uid in {0, os.geteuid()} and not info.st_mode & 0o022, 'Unsafe PostgreSQL executable')
    if server_major is not None:
        version = subprocess.run([str(executable), '--version'], stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=10, env={'PATH': os.environ.get('PATH', '')})
        match = re.search(r'PostgreSQL\)\s+(\d+)', version.stdout)
        _require(version.returncode == 0 and match and int(match[1]) == server_major, 'PostgreSQL tool major version differs')
    # Credentials never enter argv, output or the checkpoint manifest.
    environment = {'PATH': os.environ.get('PATH', ''), 'PGHOST': parameters['host'],
        'PGPORT': parameters['port'], 'PGUSER': parameters['user'], 'PGDATABASE': parameters['dbname'],
        'PGPASSWORD': parameters['password'], 'PGSSLMODE': parameters['sslmode'], 'PGGSSENCMODE': 'disable'}
    if 'sslrootcert' in parameters:
        environment['PGSSLROOTCERT'] = parameters['sslrootcert']
    result = subprocess.run([str(executable), *args], env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
    _require(result.returncode == 0, 'PostgreSQL checkpoint tool failed')


def _catalog(connection, *, empty=False):
    _require(not connection.execute("""SELECT 1 FROM pg_namespace WHERE nspname NOT IN
        ('public','pg_catalog','information_schema') AND nspname NOT LIKE 'pg_toast%'
        AND nspname NOT LIKE 'pg_temp_%' LIMIT 1""").fetchone(), 'Unexpected user schemas')
    rows = connection.execute("""SELECT c.relname,c.relkind,c.relpersistence FROM pg_class c JOIN pg_namespace n
        ON n.oid=c.relnamespace WHERE n.nspname='public' ORDER BY c.relname""").fetchall()
    if empty:
        _require(not rows, 'Restore target public schema is not empty')
    else:
        tables = {r['relname'] for r in rows if r['relkind'] == 'r'}
        _require(CORE <= tables and tables <= CORE | OPTIONAL, 'Unexpected school domain tables')
        _require(all(r['relkind'] in {'r', 'i', 'S'} for r in rows), 'Unsupported executable school schema')
        _require(all(r['relpersistence'] == 'p' for r in rows), 'Permanent school relations required')
        if 'shkola_document_jobs' in tables:
            from .notice_jobs import DOCUMENT_TABLE, verify_job_schema
            verify_job_schema(connection, DOCUMENT_TABLE, 'shkola_document_jobs_dispatch')
    _require(not connection.execute("SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' LIMIT 1").fetchone(), 'Unexpected school functions')
    _require(not connection.execute("SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND NOT t.tgisinternal LIMIT 1").fetchone(), 'Unexpected school triggers')
    _require(not connection.execute("SELECT 1 FROM pg_policies WHERE schemaname='public' LIMIT 1").fetchone(), 'Unsupported school row policies')
    columns = connection.execute("""SELECT c.relname,a.attnum,a.attname,format_type(a.atttypid,a.atttypmod) AS type,
        a.attnotnull,a.attidentity,a.attgenerated,pg_get_expr(d.adbin,d.adrelid) AS default_value
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
        LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
        WHERE n.nspname='public' AND c.relkind='r' ORDER BY c.relname,a.attnum""").fetchall()
    constraints = connection.execute("""SELECT c.relname,k.conname,pg_get_constraintdef(k.oid) AS definition
        FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' ORDER BY c.relname,k.conname""").fetchall()
    indices = connection.execute("SELECT tablename,indexname,indexdef FROM pg_indexes WHERE schemaname='public' ORDER BY tablename,indexname").fetchall()
    return {'relations': rows, 'columns': columns, 'constraints': constraints, 'indices': indices}


def _snapshot(connection):
    from psycopg import sql
    catalog = _catalog(connection)
    tables = [r['relname'] for r in catalog['relations'] if r['relkind'] == 'r']
    data, sequences = {}, {}
    for name in tables:
        rows = connection.execute(sql.SQL('SELECT to_jsonb(t)::text AS row FROM {} t').format(sql.Identifier('public', name))).fetchall()
        data[name] = {'rows': len(rows), 'sha256': digest(sorted(r['row'] for r in rows))}
    for row in connection.execute("""SELECT sequencename,start_value,min_value,max_value,increment_by,cycle,cache_size
            FROM pg_sequences WHERE schemaname='public' ORDER BY sequencename""").fetchall():
        state = connection.execute(sql.SQL('SELECT last_value,is_called FROM {}').format(sql.Identifier('public', row['sequencename']))).fetchone()
        sequences[row['sequencename']] = {**row, **state}
    return {'catalog': catalog, 'tables': data, 'sequences': sequences}


def _actors(path, school):
    import sqlite3
    with closing(sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        staff_access._validate_existing(connection, school)
        result = {}
        for row in connection.execute('SELECT * FROM staff_members'):
            if row['enabled']:
                result[row['id']] = authority(staff_access.profile(row))
        return result


def _queue_jobs(connection, school, actors, *, documents=False):
    table = 'public.shkola_document_jobs' if documents else 'public.shkola_notice_jobs'
    rows = connection.execute('SELECT * FROM ' + table + ' ORDER BY id').fetchall()
    seen, invalid, scopes = set(), [], set()
    for row in rows:
        actor, command = row['actor'], row['command']
        _require(row['school'] == school and isinstance(actor, dict) and actor.get('schoolId') == school,
                 'Foreign school queue record')
        _require(isinstance(command, dict) and row['member'] == actor.get('membershipId')
                 and row['request_id'] == command.get('requestId')
                 and row['fingerprint'] == digest([actor, command]), 'Invalid stored queue command')
        _require(re.fullmatch('[a-f0-9]{64}', row['host_scope']) is not None, 'Invalid queue scope')
        if documents:
            from .club_documents import DocumentInput, ClubDocuments, checked_receipt
            try:
                request = DocumentInput.model_validate({key: command[key] for key in ('documentId', 'revision')})
                _require(actor == authority(actor), 'Invalid stored document authority')
                _require(command == ClubDocuments._command(request, actor), 'Invalid stored document command')
                _require(str(UUID(str(row['id']))) == str(row['id']), 'Invalid document job identity')
                _require(row['state'] != 'succeeded' or row['receipt'] is not None, 'Succeeded document receipt is missing')
                if row['receipt'] is not None:
                    checked_receipt(row['receipt'], command)
            except (ValueError, TypeError, KeyError, AttributeError, HTTPException):
                raise ValueError('Invalid stored document job or receipt') from None
        key = (row['member'], row['request_id'])
        _require(key not in seen, 'Queue scopes have colliding request identities')
        seen.add(key); scopes.add(row['host_scope'])
        if actors.get(row['member']) != actor or actor.get('staffRole') not in ({'student'} if documents else {'director', 'planner'}):
            invalid.append(str(row['id']))
    immutable = [{k: row[k] for k in ('id', 'school', 'member', 'request_id', 'fingerprint', 'actor', 'command', 'receipt')} for row in rows]
    # UUID and timestamp-free content is canonicalized explicitly for the receipt.
    for row in immutable:
        row['id'] = str(row['id'])
    return {'rows': len(rows), 'scopes': sorted(scopes), 'invalidActors': invalid,
            'immutableSha256': digest(immutable)}


def _jobs(connection, school, actors):
    return _queue_jobs(connection, school, actors)


def _document_jobs(connection, school, actors):
    from .notice_jobs import DOCUMENT_TABLE, verify_job_schema
    verify_job_schema(connection, DOCUMENT_TABLE, 'shkola_document_jobs_dispatch')
    return _queue_jobs(connection, school, actors, documents=True)


def _document_manifest(manifest):
    snapshot = manifest.get('snapshot', {})
    tables = snapshot.get('tables', {})
    relations = snapshot.get('catalog', {}).get('relations', [])
    present = 'shkola_document_jobs' in tables
    catalog = any(isinstance(row, dict) and row.get('relname') == 'shkola_document_jobs' for row in relations)
    _require(present == catalog, 'Document checkpoint catalog binding differs')
    _require(('documentJobs' in manifest) == present, 'Document checkpoint audit is missing or unexpected')
    if not present:
        return None
    audit = manifest['documentJobs']
    _require(isinstance(audit, dict) and set(audit) == {'rows','scopes','invalidActors','immutableSha256'},
             'Invalid document checkpoint audit')
    _require(type(audit['rows']) is int and audit['rows'] >= 0
        and isinstance(audit['scopes'], list) and all(isinstance(value, str) and re.fullmatch('[a-f0-9]{64}', value) for value in audit['scopes'])
        and audit['scopes'] == sorted(set(audit['scopes']))
        and isinstance(audit['invalidActors'], list) and len(audit['invalidActors']) <= audit['rows']
        and all(isinstance(value, str) for value in audit['invalidActors'])
        and len(set(audit['invalidActors'])) == len(audit['invalidActors'])
        and isinstance(audit['immutableSha256'], str) and re.fullmatch('[a-f0-9]{64}', audit['immutableSha256']),
        'Invalid document checkpoint audit values')
    try:
        _require(all(isinstance(value, str) and str(UUID(value)) == value for value in audit['invalidActors']),
                 'Invalid document checkpoint actor identities')
    except (ValueError, TypeError, AttributeError):
        raise ValueError('Invalid document checkpoint actor identities') from None
    return audit


def _rebind_queue(connection, school, actors, audit, scope, *, documents=False):
    table = 'public.shkola_document_jobs' if documents else 'public.shkola_notice_jobs'
    if documents:
        _require(_document_jobs(connection, school, actors) == audit, 'Document queue changed before rebind')
    invalid = set(audit['invalidActors'])
    for row in connection.execute('SELECT id,state FROM ' + table + ' ORDER BY id').fetchall():
        state = 'rejected' if str(row['id']) in invalid else ('queued' if row['state'] == 'running' else row['state'])
        connection.execute('UPDATE ' + table + """ SET host_scope=%s,state=%s,
            rejection=CASE WHEN %s THEN 403 ELSE rejection END,lease_token=NULL,lease_until=NULL,
            next_dispatch_at=clock_timestamp() WHERE id=%s""", (scope, state, str(row['id']) in invalid, row['id']))
    after = _document_jobs(connection, school, actors) if documents else _jobs(connection, school, actors)
    _require(after['immutableSha256'] == audit['immutableSha256'] and after['rows'] == audit['rows'],
             'Queue command identities changed')
    _require(not after['rows'] or after['scopes'] == [scope], 'Queue scope rebind differs')
    return after


def _private_json(path, value):
    _write_private(path, (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode())
    _sync_directory(path.parent)


def _database_properties(connection):
    return connection.execute("""SELECT pg_encoding_to_char(d.encoding) AS encoding,d.datcollate,d.datctype,
        d.datlocprovider,to_jsonb(d)->>'datlocale' AS locale,d.datcollversion
        FROM pg_database d WHERE d.datname=current_database()""").fetchone()


def _max_audit(path, school, domain, staff):
    import sqlite3
    from .school_max_store import validate_snapshot
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=0)) as connection:
        return validate_snapshot(connection, school=school, domain=domain, staff_identity=staff)


def _max_manifest(path, manifest):
    present = any(entry['name'] == 'max.sqlite3' for entry in manifest['files'])
    _require(('max' in manifest) == present, 'MAX checkpoint audit is missing or unexpected')
    if not present:
        return None
    audit = _max_audit(path / 'max.sqlite3', manifest['school'], manifest['sourceDomain'], manifest['sourceStaffIdentity'])
    _require(manifest['max'] == audit, 'MAX checkpoint audit differs')
    return audit


def _restore_max(destination, manifest, new_domain, staff_pin, sidecars):
    import sqlite3
    from .school_max_store import rebind_snapshot
    audit = manifest.get('max')
    if audit is None:
        return None
    path = destination / 'max.sqlite3'
    with closing(sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=0)) as connection:
        connection.execute('BEGIN IMMEDIATE')
        result = rebind_snapshot(connection, school=manifest['school'], source_audit=audit,
                                 domain=new_domain, staff_identity=staff_pin)
        connection.commit()
    _sync_file(path)
    for entry in sidecars['files']:
        if entry['name'] == 'max.sqlite3':
            entry.update(restoredSha256=_hash(path), bytes=path.stat().st_size)
    return result


def _max_restore_status(manifest, receipt, target):
    """Bind the ready receipt to MAX provenance, allowing later lawful commands."""
    audit = manifest.get('max')
    if audit is None:
        _require('maxRestore' not in receipt, 'Unexpected MAX restore receipt')
        return None
    from .school_max_store import scope_for, validate_snapshot
    import sqlite3
    states = audit['states']
    expected = dict(sourceScope=audit['scope'],
        targetScope=scope_for(manifest['school'], audit['integrationId'], receipt['expectedDomain']),
        challengesCancelled=states['challenges'].get('pending', 0),
        bindingsRevoked=states['bindings'].get('verified', 0),
        inboxCancelled=states['inbox'].get('queued', 0) + states['inbox'].get('processing', 0),
        pendingCancelled=states['outbox'].get('pending', 0),
        attemptsUnknown=states['outbox'].get('attempt_started', 0))
    _require(receipt.get('maxRestore') == expected, 'MAX restore receipt differs')
    path = target.staff_path.parent / 'max.sqlite3'
    _regular(path, private=True)
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=0)) as connection:
        current = validate_snapshot(connection, school=manifest['school'], integration_id=audit['integrationId'],
            domain=receipt['expectedDomain'], staff_identity=receipt['staffIdentity'])
        data = json.loads(connection.execute('SELECT payload FROM max_scopes').fetchone()[0])
    _require(current['scope'] == expected['targetScope'] and data.get('restoredFrom') ==
        dict(scope=audit['scope'], domain=audit['sourceDomain'], staffIdentity=audit['sourceStaffIdentity'],
             payloadSha256=audit['payloadSha256']), 'MAX restored provenance differs')
    return expected


def backup_bundle(destination, *, storage=None, notices=None, operator=None, fence_source=False):
    from psycopg import Error, sql
    from .school_restore_files import locked_sidecars, copy_sidecars
    storage = storage or SchoolPostgres(SchoolPostgresSettings.from_environment(staff_access.configuration()[1]))
    notices = notices or SchoolNoticeSettings.from_environment(storage.settings.school, storage=storage)
    _require(type(notices) is SchoolNoticeSettings and notices.storage == storage, 'Expected the working school queue profile')
    notices.validate()
    if operator is None:
        _require(os.environ.get('SHKOLA_BACKUP_PG_URL_FILE') and not os.environ.get('SHKOLA_BACKUP_PG_URL'), 'Expected an operator credential file')
        operator = replace(storage.settings, url=_secret(os.environ, 'SHKOLA_BACKUP_PG_URL', needed=True))
    left, right = storage.settings.connection_parameters(), operator.connection_parameters()
    _require(all(left[k] == right[k] for k in ('host', 'port', 'dbname'))
             and operator.school == storage.settings.school and operator.expected_domain == storage.settings.expected_domain,
             'Backup operator must use the selected school database')
    destination = _new_path(destination)
    source = notices.staff_path.parent
    _require(not destination.is_relative_to(source) and not source.is_relative_to(destination), 'Separate checkpoint destination required')
    with RuntimeGate.for_staff(notices.staff_path).exclusive(), _raw(operator) as connection:
        _require(not (source / FENCE).exists() and not (source / FENCE).is_symlink(), 'Source is already fenced')
        connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
        _SchoolConnection(connection, Error, operator).validate_domain(lock=True)
        catalog = _catalog(connection)
        tables = [r['relname'] for r in catalog['relations'] if r['relkind'] == 'r']
        connection.execute(sql.SQL('LOCK TABLE {} IN SHARE MODE NOWAIT').format(sql.SQL(',').join(sql.Identifier('public', n) for n in tables)))
        with locked_sidecars(notices.staff_path, storage.settings.school) as inventory:
            snapshot = _snapshot(connection)
            jobs = _jobs(connection, storage.settings.school, _actors(notices.staff_path, storage.settings.school))
            document_jobs = _document_jobs(connection, storage.settings.school, _actors(notices.staff_path, storage.settings.school)) if 'shkola_document_jobs' in snapshot['tables'] else None
            marker = connection.execute('SELECT school_id,domain_id::text,restore_epoch::text,schema_version FROM public.school_domain_metadata WHERE id=1').fetchone()
            snapshot_id = connection.execute('SELECT pg_export_snapshot() AS id').fetchone()['id']
            destination.mkdir(mode=0o700)
            _sync_directory(destination.parent)
            entries = copy_sidecars(inventory, destination)
            dump = destination / 'domain.dump'
            _write_private(dump, b'')
            _tools(operator, 'pg_dump', ['--format=custom', '--schema=public', '--no-owner', '--no-privileges',
                   '--no-password', '--snapshot=' + snapshot_id, '--file=' + str(dump)],
                   server_major=connection.info.server_version // 10000)
            for entry in entries:
                _sync_file(destination / entry['name'])
            _sync_file(dump)
            _require(snapshot == _snapshot(connection), 'Source changed during checkpoint')
            entries.append({'name': 'domain.dump', 'kind': 'postgres-custom', 'bytes': dump.stat().st_size, 'sha256': _hash(dump)})
            manifest = {'version': 1, 'kind': 'school-postgres-private-checkpoint',
                'createdAt': datetime.now(timezone.utc).isoformat(), 'school': storage.settings.school,
                'marker': marker, 'sourceDomain': storage.settings.expected_domain, 'sourceDatabase': left['dbname'],
                'sourceScope': notices.scope, 'sourceStaffIdentity': notices.staff_expected, 'sourceOrigin': notices.origin,
                'sourceRuntime': str(source), 'fenceNonce': str(uuid4()) if fence_source else None,
                'serverMajor': connection.info.server_version // 10000,
                'databaseProperties': _database_properties(connection),
                'files': entries, 'snapshot': snapshot, 'jobs': jobs}
            if document_jobs is not None:
                manifest['documentJobs'] = document_jobs
            if any(entry['name'] == 'max.sqlite3' for entry in entries):
                manifest['max'] = _max_audit(destination / 'max.sqlite3', storage.settings.school,
                                             storage.settings.expected_domain, notices.staff_expected)
            _private_json(destination / 'manifest.json', manifest)
            if fence_source:
                _private_json(source / FENCE, {'kind': 'school-checkpoint-fence', 'school': storage.settings.school,
                    'sourceDomain': storage.settings.expected_domain, 'sourceStaffIdentity': notices.staff_expected,
                    'fenceNonce': manifest['fenceNonce'], 'manifestSha256': _hash(destination / 'manifest.json')})
    return {'kind': 'school-postgres-private-checkpoint', 'manifestSha256': _hash(destination / 'manifest.json'),
            'files': len(entries), 'jobs': jobs['rows'], 'privateCheckpoint': str(destination)}


def _bundle(raw, expected_hash):
    from .school_restore_files import validate_sidecar
    path = Path(raw)
    _private_directory(path)
    _regular(path / 'manifest.json', private=True)
    _require(re.fullmatch('[a-f0-9]{64}', expected_hash) is not None
             and _hash(path / 'manifest.json') == expected_hash, 'Independent checkpoint hash does not match')
    value = json.loads((path / 'manifest.json').read_bytes())
    _require(value.get('version') == 1 and value.get('kind') == 'school-postgres-private-checkpoint', 'Unknown checkpoint format')
    school, entries = value.get('school'), value.get('files')
    _require(isinstance(school, str) and re.fullmatch('[A-Za-z0-9_-]{1,80}', school)
             and isinstance(entries, list) and all(isinstance(e, dict) for e in entries), 'Invalid checkpoint inventory')
    names = [e.get('name') for e in entries]
    _require(len(names) == len(set(names)) and {'staff.sqlite3', 'domain.dump'} <= set(names)
             and set(names) <= SIDECARS | {'domain.dump'}
             and {p.name for p in path.iterdir()} == set(names) | {'manifest.json'}, 'Unknown checkpoint files')
    for entry in entries:
        file = path / entry['name']
        _regular(file, private=True)
        _require(entry.get('bytes') == file.stat().st_size and entry.get('sha256') == _hash(file), 'Checkpoint content changed')
        _require(entry.get('kind') == ('postgres-custom' if file.name == 'domain.dump' else 'sqlite'), 'Unknown checkpoint content type')
        if file.name != 'domain.dump':
            validate_sidecar(file, file.name, school)
    marker = value['marker']
    _require(marker['school_id'] == school and marker['schema_version'] == 1
             and domain_identity(school, marker['domain_id'], marker['restore_epoch'], value['sourceDatabase']) == value['sourceDomain'],
             'Checkpoint school binding changed')
    _document_manifest(value)
    _max_manifest(path, value)
    return path, value


def _fence(manifest, manifest_hash):
    source = Path(manifest['sourceRuntime'])
    _private_directory(source)
    file = source / FENCE
    _regular(file, private=True)
    value = json.loads(file.read_bytes())
    expected = {'kind': 'school-checkpoint-fence', 'school': manifest['school'],
        'sourceDomain': manifest['sourceDomain'], 'sourceStaffIdentity': manifest['sourceStaffIdentity'],
        'fenceNonce': manifest['fenceNonce'], 'manifestSha256': manifest_hash}
    _require(manifest['fenceNonce'] and {k:v for k,v in value.items() if k != 'claim'} == expected
             and ('claim' not in value or isinstance(value['claim'], dict)), 'Source fence is not confirmed')
    return source


def _replace_json(path, value):
    _regular(path, private=True)
    temporary = path.with_name(path.name + '.' + uuid4().hex)
    _private_json(temporary, value)
    temporary.replace(path)
    _sync_directory(path.parent)


@dataclass(frozen=True)
class RestoreTarget:
    settings: SchoolPostgresSettings = field(repr=False)
    staff_path: Path
    origin: str
    runtime_role: str

    def validate(self):
        from urllib.parse import urlsplit
        path = Path(self.staff_path)
        _require(path.is_absolute() and path.resolve(strict=False) == path and path.name == 'staff.sqlite3',
                 'Expected a new canonical staff.sqlite3 target')
        _private_directory(path.parent)
        _require(all(p.name == 'school-runtime.lock' for p in path.parent.iterdir()), 'Restore file target is not empty')
        parsed = urlsplit(self.origin)
        _require(parsed.hostname and not parsed.username and not parsed.password and not parsed.path
            and not parsed.query and not parsed.fragment and (parsed.scheme == 'https'
                or parsed.scheme == 'http' and parsed.hostname == '127.0.0.1'), 'Protected target origin required')
        _require(re.fullmatch('[A-Za-z0-9_-]{1,63}', self.runtime_role) is not None, 'Explicit runtime SQL role required')
        self.settings.connection_parameters()


def _target_info(connection, target):
    _catalog(connection, empty=True)
    _require(connection.execute('SHOW fsync').fetchone()['fsync'] == 'on'
             and connection.execute('SHOW full_page_writes').fetchone()['full_page_writes'] == 'on',
             'Durable target PostgreSQL configuration required')
    _require(not connection.execute("SELECT 1 FROM pg_default_acl LIMIT 1").fetchone(), 'Unexpected target default privileges')
    role = connection.execute('SELECT oid,rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls,rolcanlogin FROM pg_roles WHERE rolname=%s', (target.runtime_role,)).fetchone()
    _require(role and role['rolcanlogin'] and not any(role[k] for k in ('rolsuper', 'rolcreatedb', 'rolcreaterole', 'rolreplication', 'rolbypassrls')),
             'Runtime role must be an existing restricted login')
    _require(not connection.execute('SELECT 1 FROM pg_auth_members WHERE member=%s LIMIT 1', (role['oid'],)).fetchone(), 'Runtime role has inherited privileges')
    _require(not connection.execute("""SELECT 1 FROM pg_database d CROSS JOIN pg_namespace n
        WHERE d.datname=current_database() AND n.nspname='public'
        AND (d.datdba=%s OR n.nspowner=%s) LIMIT 1""", (role['oid'], role['oid'])).fetchone(),
             'Runtime role must not own the target database or public schema')
    _require(not connection.execute("SELECT has_schema_privilege(%s,'public','CREATE') AS allowed", (target.runtime_role,)).fetchone()['allowed'],
             'Runtime role must not create schema objects')
    _require(not connection.execute("SELECT has_database_privilege(%s,current_database(),'CREATE') AS allowed", (target.runtime_role,)).fetchone()['allowed'],
             'Runtime role must not own or create database schemas')
    _require(connection.execute('SELECT current_user AS name').fetchone()['name'] != target.runtime_role,
             'Separate provisioning and runtime roles required')
    operator = connection.execute("""SELECT
        r.rolsuper OR pg_has_role(current_user,d.datdba,'USAGE') AS database_owner,
        r.rolsuper OR pg_has_role(current_user,n.nspowner,'USAGE') AS schema_owner,
        has_database_privilege(current_user,current_database(),'CREATE') AS can_create
        FROM pg_database d CROSS JOIN pg_namespace n CROSS JOIN pg_roles r
        WHERE d.datname=current_database() AND n.nspname='public' AND r.rolname=current_user""").fetchone()
    _require(operator and all(operator[key] for key in ('database_owner', 'schema_owner', 'can_create')),
             'Restore operator must own the target database and public schema')
    return connection.execute("""SELECT current_database() AS database,d.oid::text AS database_oid,
        pg_postmaster_start_time()::text AS started,c.system_identifier::text AS cluster,
        current_user AS provisioner FROM pg_database d CROSS JOIN pg_control_system() c
        WHERE d.datname=current_database()""").fetchone()


def plan_restore(bundle, manifest_sha256, target):
    _require(type(target) is RestoreTarget, 'Explicit restore target required')
    path, manifest = _bundle(bundle, manifest_sha256)
    source = _fence(manifest, manifest_sha256)
    target.validate()
    destination = target.staff_path.parent
    _require(target.settings.school == manifest['school'] and destination != source
        and not destination.is_relative_to(path) and not path.is_relative_to(destination)
        and not destination.is_relative_to(source) and not source.is_relative_to(destination), 'Separate school restore target required')
    with _raw(target.settings, autocommit=True) as connection:
        identity = _target_info(connection, target)
        _require(connection.info.server_version // 10000 == manifest['serverMajor']
                 and _database_properties(connection) == manifest['databaseProperties'],
                 'Restore database version or locale differs')
    intent = {'version': 1, 'manifestSha256': manifest_sha256, 'school': manifest['school'],
        'targetIdentity': identity, 'staffPath': str(target.staff_path), 'origin': target.origin,
        'runtimeRole': target.runtime_role, 'sourceScopes': manifest['jobs']['scopes'],
        'jobs': manifest['jobs']['rows'], 'sourceInvalidActors': len(manifest['jobs']['invalidActors'])}
    plan_hash = digest(intent)
    epoch = str(uuid5(NAMESPACE_OID, 'school-restore-v1:' + plan_hash))
    _require(epoch != manifest['marker']['restore_epoch'], 'New restore generation required')
    plan = {**intent, 'planHash': plan_hash, 'newRestoreEpoch': epoch,
        'activationId': str(uuid5(NAMESPACE_OID, 'school-restore-activation-v1:' + plan_hash)),
        'newExpectedDomain': domain_identity(manifest['school'], manifest['marker']['domain_id'], epoch, identity['database'])}
    fence = json.loads((source / FENCE).read_bytes())
    _require('claim' not in fence or fence['claim'].get('planHash') == plan_hash,
             'Checkpoint is already claimed by a different target')
    return plan


def check_activation(staff_path, school, expected_domain, origin, expected_activation=''):
    file = Path(staff_path).parent / ACTIVATION
    if not file.exists() and not file.is_symlink():
        _require(not expected_activation, 'Restored runtime activation is missing')
        return
    _regular(file, private=True)
    value = json.loads(file.read_bytes())
    _require(value.get('state') == 'ready' and value.get('school') == school
        and value.get('expectedDomain') == expected_domain and value.get('origin') == origin
        and value.get('activationId') == expected_activation and bool(expected_activation)
        and value.get('staffIdentity') == staff_identity(school, staff_path, origin), 'Restored runtime is not activated')


def require_activation(*args):
    from fastapi import HTTPException
    try:
        check_activation(*args)
    except (OSError, ValueError, TypeError, RuntimeError):
        raise HTTPException(503, 'Восстановленный школьный контур ещё не активирован') from None


def _grant(connection, target, tables):
    from psycopg import sql
    role = sql.Identifier(target.runtime_role)
    connection.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(sql.Identifier(target.settings.connection_parameters()['dbname']), role))
    connection.execute(sql.SQL('GRANT USAGE ON SCHEMA public TO {}').format(role))
    for name in tables:
        permissions = 'SELECT,UPDATE(id)' if name == 'school_domain_metadata' else (
            'SELECT,INSERT,UPDATE' if name in {'shkola_notice_jobs','shkola_document_jobs'} else 'SELECT,INSERT,UPDATE,DELETE')
        connection.execute(sql.SQL('GRANT ' + permissions + ' ON TABLE {} TO {}').format(sql.Identifier('public', name), role))
    connection.execute(sql.SQL('GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO {}').format(role))
    for privilege in ('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE'):
        _require(not connection.execute("SELECT has_table_privilege(%s,'public.school_domain_metadata',%s) AS allowed",
            (target.runtime_role, privilege)).fetchone()['allowed'], 'Runtime marker privileges are too broad')
    _require(not connection.execute("SELECT has_column_privilege(%s,'public.school_domain_metadata','restore_epoch','UPDATE') AS allowed",
        (target.runtime_role,)).fetchone()['allowed'], 'Runtime may not rotate the domain')
    for queue in ('shkola_notice_jobs','shkola_document_jobs'):
        if queue not in tables:
            continue
        for permission in ('DELETE','TRUNCATE'):
            _require(not connection.execute('SELECT has_table_privilege(%s,%s,%s) AS allowed',
                (target.runtime_role, 'public.' + queue, permission)).fetchone()['allowed'], 'Runtime queue privileges are too broad')


def restore_bundle(bundle, manifest_sha256, target, *, expected_plan_hash):
    from psycopg import sql
    from .school_restore_files import restore_sidecars
    path, manifest = _bundle(bundle, manifest_sha256)
    source = _fence(manifest, manifest_sha256)
    target.validate()
    destination = target.staff_path.parent
    with RuntimeGate.for_staff(source / 'staff.sqlite3').exclusive(), RuntimeGate.for_staff(target.staff_path).exclusive(), \
            _raw(target.settings, autocommit=True) as controller:
        _require(controller.execute('SELECT pg_try_advisory_lock(%s) AS locked', (_RESTORE_LOCK,)).fetchone()['locked'], 'Restore database is busy')
        plan = plan_restore(bundle, manifest_sha256, target)
        _require(plan['planHash'] == expected_plan_hash, 'Restore plan changed; inspect a new dry-run')
        fence = json.loads((source / FENCE).read_bytes())
        _require('claim' not in fence, 'Checkpoint restore has already been claimed')
        fence['claim'] = {'planHash': plan['planHash'], 'activationId': plan['activationId'],
            'targetIdentity': plan['targetIdentity'], 'staffPath': str(target.staff_path), 'state': 'claimed'}
        _replace_json(source / FENCE, fence)
        activation = {'state': 'staging', 'school': manifest['school'], 'origin': target.origin,
            'expectedDomain': plan['newExpectedDomain'], 'planHash': plan['planHash'], 'manifestSha256': manifest_sha256,
            'activationId': plan['activationId']}
        _private_json(destination / ACTIVATION, activation)
        sidecars = restore_sidecars(path, destination, [e for e in manifest['files'] if e['name'] in SIDECARS], manifest['school'])
        for entry in sidecars['files']:
            _sync_file(destination / entry['name'])
        _sync_directory(destination)
        _tools(target.settings, 'pg_restore', ['--exit-on-error', '--single-transaction', '--clean', '--if-exists', '--no-owner',
            '--no-privileges', '--no-password', '--dbname=' + target.settings.connection_parameters()['dbname'], str(path / 'domain.dump')],
            server_major=manifest['serverMajor'])
        with _raw(target.settings) as connection:
            tables = [r['relname'] for r in _catalog(connection)['relations'] if r['relkind'] == 'r']
            connection.execute(sql.SQL('LOCK TABLE {} IN ACCESS EXCLUSIVE MODE NOWAIT').format(sql.SQL(',').join(sql.Identifier('public', n) for n in tables)))
            _require(_snapshot(connection) == manifest['snapshot'], 'Restored rows, schema or sequences changed')
            jobs = _jobs(connection, manifest['school'], _actors(target.staff_path, manifest['school']))
            _require(jobs == manifest['jobs'], 'Restored queue or authority changed')
            document_audit = _document_manifest(manifest)
            if document_audit is not None:
                _require(_document_jobs(connection, manifest['school'], _actors(target.staff_path, manifest['school'])) == document_audit, 'Restored document queue or authority changed')
            pin = staff_identity(manifest['school'], target.staff_path, target.origin)
            new_settings = replace(target.settings, expected_domain=plan['newExpectedDomain'], activation_id=plan['activationId'])
            scope = _scope(manifest['school'], SchoolPostgres(new_settings), pin, target.origin)
            connection.execute('ALTER TABLE public.school_domain_metadata ADD COLUMN IF NOT EXISTS activation_id uuid')
            connection.execute('UPDATE public.school_domain_metadata SET restore_epoch=%s,activation_id=%s WHERE id=1',
                               (plan['newRestoreEpoch'], plan['activationId']))
            actors = _actors(target.staff_path, manifest['school'])
            invalid = set(jobs['invalidActors'])
            _rebind_queue(connection, manifest['school'], actors, jobs, scope)
            restored_documents = _rebind_queue(connection, manifest['school'], actors, document_audit, scope, documents=True) if document_audit is not None else None
            _grant(connection, target, tables)
            restored_max = _restore_max(destination, manifest, plan['newExpectedDomain'], pin, sidecars)
        receipt = {**activation, 'state': 'ready', 'staffIdentity': pin, 'noticeScope': scope,
            'sourceDomain': manifest['sourceDomain'], 'sourceScopes': jobs['scopes'],
            'restoredJobs': jobs['rows'], 'rejectedActors': len(invalid), 'sidecars': sidecars,
            'configuration': {'SHKOLA_DOMAIN_BACKEND': 'postgresql', 'SHKOLA_SCHOOL_ID': manifest['school'],
                'SHKOLA_STAFF_DB_PATH': str(target.staff_path), 'SHKOLA_STAFF_ORIGIN': target.origin,
                'SHKOLA_SCHOOL_PG_EXPECTED_DOMAIN': plan['newExpectedDomain'], 'SHKOLA_NOTICE_STAFF_IDENTITY': pin,
                'SHKOLA_RESTORE_ACTIVATION_ID': plan['activationId']}}
        if restored_documents is not None:
            receipt['documentJobs'] = restored_documents
        if restored_max is not None:
            receipt['maxRestore'] = restored_max
            receipt['configuration']['SHKOLA_MAX_STAFF_IDENTITY'] = pin
        _replace_json(destination / ACTIVATION, receipt)
        fence['claim']['state'] = 'ready'
        _replace_json(source / FENCE, fence)
        return receipt


def restore_status(bundle, manifest_sha256, target):
    """Read the commit receipt after a lost final response; never apply again."""
    _, manifest = _bundle(bundle, manifest_sha256)
    source = _fence(manifest, manifest_sha256)
    _private_directory(target.staff_path.parent)
    file = target.staff_path.parent / ACTIVATION
    _regular(file, private=True)
    receipt = json.loads(file.read_bytes())
    fence = json.loads((source / FENCE).read_bytes())
    claim = fence.get('claim', {})
    _require(receipt.get('manifestSha256') == manifest_sha256 and receipt.get('school') == manifest['school']
        and receipt.get('planHash') == claim.get('planHash') and receipt.get('activationId') == claim.get('activationId')
        and str(target.staff_path) == claim.get('staffPath') and target.origin == receipt.get('origin'), 'Restore receipt binding differs')
    if receipt.get('state') != 'ready':
        return {'state': 'staging', 'ready': False, 'planHash': receipt['planHash'], 'retryApplyAllowed': False}
    check_activation(target.staff_path, manifest['school'], receipt['expectedDomain'], target.origin, receipt['activationId'])
    with _raw(target.settings, autocommit=True) as connection:
        identity = connection.execute("""SELECT current_database() AS database,d.oid::text AS database_oid,
            c.system_identifier::text AS cluster FROM pg_database d CROSS JOIN pg_control_system() c
            WHERE d.datname=current_database()""").fetchone()
        _require(all(identity[k] == claim['targetIdentity'][k] for k in identity), 'Restored database identity differs')
        row = connection.execute('SELECT school_id,domain_id::text,restore_epoch::text,activation_id::text FROM public.school_domain_metadata WHERE id=1').fetchone()
        _require(row and row['school_id'] == manifest['school'] and row['activation_id'] == receipt['activationId']
            and domain_identity(row['school_id'], row['domain_id'], row['restore_epoch'], identity['database']) == receipt['expectedDomain'],
            'Restored domain identity differs')
        document_audit = _document_manifest(manifest)
        if document_audit is not None:
            committed = receipt.get('documentJobs', {})
            _require(committed.get('rows') == document_audit['rows']
                and committed.get('immutableSha256') == document_audit['immutableSha256']
                and committed.get('invalidActors') == document_audit['invalidActors']
                and committed.get('scopes') == ([receipt['noticeScope']] if committed.get('rows') else []),
                'Document restore receipt differs')
            from .notice_jobs import DOCUMENT_TABLE, verify_job_schema
            verify_job_schema(connection, DOCUMENT_TABLE, 'shkola_document_jobs_dispatch')
        else:
            _require('documentJobs' not in receipt, 'Unexpected document restore receipt')
    _max_restore_status(manifest, receipt, target)
    return {**receipt, 'ready': True, 'sourceReceiptPending': claim.get('state') != 'ready'}


def _environment_target():
    _require(os.environ.get('SHKOLA_RESTORE_PG_URL_FILE') and not os.environ.get('SHKOLA_RESTORE_PG_URL'),
             'Expected a private restore credential file')
    settings = SchoolPostgresSettings(os.environ.get('SHKOLA_SCHOOL_ID', ''),
        _secret(os.environ, 'SHKOLA_RESTORE_PG_URL', needed=True), '0' * 64,
        os.environ.get('SHKOLA_RESTORE_PG_CA_FILE', ''))
    return RestoreTarget(settings, Path(os.environ.get('SHKOLA_RESTORE_STAFF_DB_PATH', '')),
        os.environ.get('SHKOLA_RESTORE_ORIGIN', ''), os.environ.get('SHKOLA_RESTORE_RUNTIME_ROLE', ''))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    backup = commands.add_parser('backup')
    backup.add_argument('--destination', required=True)
    backup.add_argument('--fence-source', action='store_true')
    for name in ('plan', 'restore', 'status'):
        command = commands.add_parser(name)
        command.add_argument('--bundle', required=True)
        command.add_argument('--manifest-sha256', required=True)
        if name == 'restore':
            command.add_argument('--plan-sha256', required=True)
    args = parser.parse_args()
    try:
        os.environ['PYTHON_DOTENV_DISABLED'] = '1'
        if args.action == 'backup':
            result = backup_bundle(args.destination, fence_source=args.fence_source)
        else:
            target = _environment_target()
            if args.action == 'plan':
                result = plan_restore(args.bundle, args.manifest_sha256, target)
            elif args.action == 'restore':
                result = restore_bundle(args.bundle, args.manifest_sha256, target, expected_plan_hash=args.plan_sha256)
            else:
                result = restore_status(args.bundle, args.manifest_sha256, target)
        print(json.dumps(result, ensure_ascii=False))
    except Exception:
        raise SystemExit('Операция восстановления не завершена. Проверьте приватную конфигурацию, состояние и утверждённый план.') from None


if __name__ == '__main__':
    main()
