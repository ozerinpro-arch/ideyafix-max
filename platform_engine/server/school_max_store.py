"""Strict optional MAX sidecar for the selected native school PostgreSQL profile.

No credentials, provider calls, configuration reads or activation are performed.
The caller owns RuntimeGate.shared for the complete operation, including I/O.
Snapshot helpers operate only on an already owned SQLite connection.
"""
from contextlib import closing, contextmanager
from copy import deepcopy
import json
from pathlib import Path
import re

from fastapi import HTTPException

from . import launch_runtime as authority
from .integration_storage import APPLICATION_ID, _check_schema
from .max_identity import MaxStore
from .schedule_notifications import SCHEDULE_NOTICE_TEXT

PROFILE_KIND = 'school-postgres-max-v1'
NAME = 'max.sqlite3'
TABLES = frozenset({'max_scopes'})
COLLECTIONS = ('challenges', 'bindings', 'inbox', 'outbox', 'callbacks')
UNAVAILABLE = 'Личное хранилище MAX недоступно.'


def _require(value, message='Invalid school MAX snapshot'):
    if not value:
        raise ValueError(message)


def _hash(value):
    return isinstance(value, str) and re.fullmatch(r'[a-f0-9]{64}', value) is not None


def _text(value, maximum=160):
    return isinstance(value, str) and 0 < len(value) <= maximum and not any(ord(c) < 32 for c in value)


def _int(value):
    return type(value) is int and 0 <= value < 2 ** 63


def _json(raw):
    def unique(items):
        result = {}
        for key, value in items:
            _require(key not in result, 'Duplicate school MAX JSON field')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid JSON constant')))


def scope_for(school, integration_id, domain):
    return authority.digest([school, integration_id, PROFILE_KIND, domain])


def _actor(actor, school):
    _require(isinstance(actor, dict) and actor.get('schoolId') == school
             and actor.get('staffRole') == 'teacher' and actor.get('membershipVerified') is True
             and _text(actor.get('membershipId')) and _text(actor.get('userId'))
             and _int(actor.get('membershipRevision')))


def validate_payload(data, *, school, integration_id=None, domain=None, staff_identity=None):
    """Pure validation; never consult live membership or rewrite historical records."""
    _require(isinstance(data, dict))
    required = {'formatVersion', 'schoolId', 'integrationId', 'profileKind', 'replacementDomain', 'replacementStaff', *COLLECTIONS}
    _require(required <= set(data) and set(data) <= required | {'restoredFrom', 'polling'})
    _require(type(data['formatVersion']) is int and data['formatVersion'] == 1
             and data['schoolId'] == school and data['profileKind'] == PROFILE_KIND
             and isinstance(data['integrationId'], str)
             and re.fullmatch(r'[A-Za-z0-9_-]{1,80}', data['integrationId'])
             and _hash(data['replacementDomain']) and _hash(data['replacementStaff']))
    for key, value in (('integrationId', integration_id), ('replacementDomain', domain), ('replacementStaff', staff_identity)):
        _require(value is None or data[key] == value, 'School MAX identity differs')
    for name in COLLECTIONS:
        _require(isinstance(data[name], dict) and len(data[name]) <= 5000)
    # This native profile accepts identity events and fixed schedule notices only.
    _require(data['callbacks'] == {}, 'School MAX callbacks are unsupported')
    if 'polling' in data:
        polling = data['polling']
        _require(isinstance(polling, dict) and set(polling) == {'marker'}
                 and (polling['marker'] is None or _int(polling['marker'])), 'Invalid MAX polling marker')
    if 'restoredFrom' in data:
        old = data['restoredFrom']
        _require(isinstance(old, dict) and set(old) == {'scope', 'domain', 'staffIdentity', 'payloadSha256'}
                 and all(_hash(value) for value in old.values()))
    for key, row in data['challenges'].items():
        _require(_hash(key) and isinstance(row, dict)
                 and set(row) == {'actor', 'state', 'createdAt', 'expiresAt'}
                 and row['state'] in {'pending', 'used', 'cancelled'}
                 and _int(row['createdAt']) and _int(row['expiresAt']) and row['expiresAt'] > row['createdAt'])
        _actor(row['actor'], school)
    for key, row in data['bindings'].items():
        _require(isinstance(row, dict) and set(row) == {'id', 'actor', 'senderId', 'chatId', 'state', 'revision', 'generation', 'verifiedAt'}
                 and key == row['id'] and isinstance(key, str) and re.fullmatch(r'mb_[A-Za-z0-9_-]{16,80}', key)
                 and row['state'] in {'verified', 'revoked'} and _int(row['revision']) and _int(row['verifiedAt'])
                 and _text(row['senderId'], 80) and _text(row['chatId'], 80)
                 and isinstance(row['generation'], str) and re.fullmatch(r'[A-Za-z0-9_-]{16,100}', row['generation']))
        _actor(row['actor'], school)
    current_members, current_senders = set(), set()
    for row in data['bindings'].values():
        if row['state'] == 'verified':
            member = row['actor']['membershipId']
            _require(member not in current_members and row['senderId'] not in current_senders)
            current_members.add(member); current_senders.add(row['senderId'])
    inbox_keys = {'id', 'event', 'state', 'receivedAt', 'leaseUntil', 'claim', 'bindingId', 'generation', 'command', 'result'}
    event_keys = {'eventType', 'eventId', 'senderId', 'chatId', 'timestamp', 'nativeId', 'payloadHash', 'isPrivate', 'messageTimestamp', 'callbackTimestamp'}
    for key, row in data['inbox'].items():
        _require(_hash(key) and isinstance(row, dict) and inbox_keys <= set(row)
                 and set(row) <= inbox_keys | {'failureCode'} and row['id'] == key
                 and row['state'] in {'queued', 'processing', 'completed', 'failed', 'cancelled'}
                 and _int(row['receivedAt']) and (row['leaseUntil'] is None or _int(row['leaseUntil']))
                 and (row['claim'] is None or _text(row['claim'])) and row['command'] is None and row['result'] is None)
        event = row['event']
        _require(isinstance(event, dict) and event_keys <= set(event) and set(event) <= event_keys | {'startHash'}
                 and event['eventType'] in {'bot_started', 'bot_stopped'} and _text(event['eventId'], 256)
                 and key == authority.digest([school, data['integrationId'], event['eventId']])
                 and _text(event['senderId'], 80) and _text(event['chatId'], 80)
                 and _int(event['timestamp']) and _hash(event['payloadHash']) and event['isPrivate'] is True
                 and event['nativeId'] is None and event['messageTimestamp'] is None and event['callbackTimestamp'] is None
                 and ('startHash' not in event or _hash(event['startHash']) or event['startHash'] == 'invalid'))
        if row['bindingId'] is not None:
            bound = data['bindings'].get(row['bindingId'])
            _require(bound is not None and row['generation'] == bound['generation']
                     and event['senderId'] == bound['senderId'] and event['chatId'] == bound['chatId'])
        else:
            _require(row['generation'] is None)
        if 'failureCode' in row:
            _require(type(row['failureCode']) is int and 100 <= row['failureCode'] <= 599)
    outbox_keys = {'id', 'bindingId', 'generation', 'chatId', 'state', 'text', 'schoolRequestId', 'questionId',
                   'requestRevision', 'providerMessageId', 'attemptId', 'createdAt', 'scheduleNoticeId',
                   'schedulePublicationVersion', 'replacementDomain'}
    unique_notices = set()
    for key, row in data['outbox'].items():
        _require(_hash(key) and isinstance(row, dict) and outbox_keys <= set(row)
                 and set(row) <= outbox_keys | {'attemptedAt'} and row['id'] == key
                 and row['state'] in {'pending', 'attempt_started', 'accepted', 'failed', 'outcome_unknown', 'cancelled'}
                 and row['text'] == SCHEDULE_NOTICE_TEXT and row['schoolRequestId'] is None
                 and row['questionId'] is None and row['requestRevision'] is None
                 and row['replacementDomain'] == data['replacementDomain'] and _int(row['createdAt'])
                 and _text(row['scheduleNoticeId']) and type(row['schedulePublicationVersion']) is int
                 and 1 <= row['schedulePublicationVersion'] <= 2 ** 31 - 1
                 and (row['providerMessageId'] is None or _text(row['providerMessageId'], 256))
                 and (row['attemptId'] is None or _text(row['attemptId'])))
        bound = data['bindings'].get(row['bindingId'])
        _require(bound is not None and row['generation'] == bound['generation'] and row['chatId'] == bound['chatId'])
        notice = row['scheduleNoticeId']
        _require(notice not in unique_notices, 'Duplicate school MAX notice')
        unique_notices.add(notice)
        _require(key == authority.digest([school, data['integrationId'], 'schedule_notice_' + notice, row['bindingId'], row['generation']]))
        if 'attemptedAt' in row:
            _require(row['attemptedAt'] is None or _int(row['attemptedAt']))
        if row['state'] == 'attempt_started':
            _require(row['attemptId'] is not None and _int(row.get('attemptedAt')))
    packed = json.dumps(data, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    _require(len(packed) <= 16 * 1024 ** 2)
    states = {name: {} for name in COLLECTIONS}
    for name in COLLECTIONS:
        for row in data[name].values():
            state = row['state']; states[name][state] = states[name].get(state, 0) + 1
    return dict(profileKind=PROFILE_KIND, formatVersion=1, integrationId=data['integrationId'],
                sourceDomain=data['replacementDomain'], sourceStaffIdentity=data['replacementStaff'],
                scope=scope_for(school, data['integrationId'], data['replacementDomain']),
                payloadSha256=authority.digest(data), states=states)


def validate_snapshot(connection, *, school, integration_id=None, domain=None, staff_identity=None):
    """Validate exactly one initialized native scope; return non-sensitive audit."""
    connection.execute('PRAGMA trusted_schema=OFF')
    _check_schema(connection, TABLES)
    _require(connection.execute('PRAGMA application_id').fetchone()[0] == APPLICATION_ID)
    _require(not connection.execute("SELECT 1 FROM sqlite_master WHERE type IN ('view','trigger') OR upper(sql) LIKE 'CREATE VIRTUAL TABLE%' LIMIT 1").fetchone())
    _require({r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")} == TABLES)
    columns = connection.execute('PRAGMA table_info(max_scopes)').fetchall()
    _require([tuple(r)[1:3] for r in columns] == [('scope', 'TEXT'), ('payload', 'TEXT')]
             and columns[0][5] == 1 and columns[1][3] == 1 and columns[1][5] == 0)
    _require(connection.execute('PRAGMA integrity_check').fetchall() == [('ok',)])
    rows = connection.execute('SELECT scope,payload FROM max_scopes').fetchall()
    _require(len(rows) == 1 and isinstance(rows[0][1], str) and len(rows[0][1].encode()) <= 16 * 1024 ** 2)
    data = _json(rows[0][1])
    audit = validate_payload(data, school=school, integration_id=integration_id, domain=domain, staff_identity=staff_identity)
    _require(rows[0][0] == audit['scope'], 'School MAX scope differs')
    return audit


def rebind_snapshot(connection, *, school, source_audit, domain, staff_identity):
    """Caller-owned transaction; invalidate all capabilities before target activation."""
    old = validate_snapshot(connection, school=school)
    _require(old == source_audit, 'School MAX checkpoint audit changed')
    _require(_hash(domain) and _hash(staff_identity) and domain != old['sourceDomain'], 'New MAX restore generation required')
    data = json.loads(connection.execute('SELECT payload FROM max_scopes').fetchone()[0])
    data = deepcopy(data)
    counts = dict(challengesCancelled=0, bindingsRevoked=0, inboxCancelled=0, pendingCancelled=0, attemptsUnknown=0)
    for row in data['challenges'].values():
        if row['state'] == 'pending': row['state'] = 'cancelled'; counts['challengesCancelled'] += 1
    for row in data['bindings'].values():
        if row['state'] == 'verified': row.update(state='revoked', revision=row['revision'] + 1); counts['bindingsRevoked'] += 1
    for row in data['inbox'].values():
        if row['state'] in {'queued', 'processing'}: row.update(state='cancelled', claim=None, leaseUntil=None); counts['inboxCancelled'] += 1
    for row in data['outbox'].values():
        if row['state'] == 'pending': row['state'] = 'cancelled'; counts['pendingCancelled'] += 1
        elif row['state'] == 'attempt_started': row['state'] = 'outcome_unknown'; counts['attemptsUnknown'] += 1
        row['replacementDomain'] = domain
    if 'polling' in data:
        data['polling']['marker'] = None
    data.update(replacementDomain=domain, replacementStaff=staff_identity,
                restoredFrom=dict(scope=old['scope'], domain=old['sourceDomain'], staffIdentity=old['sourceStaffIdentity'], payloadSha256=old['payloadSha256']))
    audit = validate_payload(data, school=school, domain=domain, staff_identity=staff_identity)
    connection.execute('UPDATE max_scopes SET scope=?,payload=? WHERE scope=?',
                       (audit['scope'], json.dumps(data, ensure_ascii=False, allow_nan=False), old['scope']))
    _require(validate_snapshot(connection, school=school, domain=domain, staff_identity=staff_identity) == audit)
    return dict(sourceScope=old['scope'], targetScope=audit['scope'], **counts)


class SchoolMaxStore(MaxStore):
    def __init__(self, *, db_path, school_id, integration_id, enabled, domain, staff_identity, validate_profile):
        _require(_hash(domain) and _hash(staff_identity) and callable(validate_profile))
        super().__init__(db_path, school_id, integration_id, enabled=enabled,
                         replacement_domain=domain, replacement_staff=staff_identity)
        _require(self.path is not None and self.path.name == NAME)
        self.scope = scope_for(school_id, integration_id, domain)
        self.validate_profile = validate_profile

    def _empty(self):
        return {**super()._empty(), 'profileKind': PROFILE_KIND}

    def _validate_scope(self, data):
        validate_payload(data, school=self.school_id, integration_id=self.integration_id,
                         domain=self.replacement_domain, staff_identity=self.replacement_staff)

    def require(self):
        super().require()
        self.validate_profile()

    def read(self):
        self.require()
        _require(not self.path.is_symlink(), 'MAX sidecar symlink is forbidden')
        if not self.path.exists():
            self.validate_profile()
            return self._empty()
        from .integration_storage import open_private_sqlite
        with closing(open_private_sqlite(self.path, TABLES, readonly=True)) as connection:
            validate_snapshot(connection, school=self.school_id, integration_id=self.integration_id,
                              domain=self.replacement_domain, staff_identity=self.replacement_staff)
            data = json.loads(connection.execute('SELECT payload FROM max_scopes').fetchone()[0])
        self.validate_profile()
        return data

    @contextmanager
    def edit(self):
        self.require()
        existed = self.path.exists()
        if existed or self.path.is_symlink():
            self.read()
        connection = self._connect()
        try:
            connection.execute('BEGIN IMMEDIATE')
            rows = connection.execute('SELECT scope,payload FROM max_scopes').fetchall()
            if not rows and not existed:
                # Persist only the empty scoped container, so a later rejected first
                # command cannot leave an initialized-but-unreadable zero-row file.
                empty = self._empty()
                self.validate_profile()
                self._validate_scope(empty)
                connection.execute('INSERT INTO max_scopes(scope,payload) VALUES(?,?)',
                                   (self.scope, json.dumps(empty, ensure_ascii=False, allow_nan=False)))
                validate_snapshot(connection, school=self.school_id, integration_id=self.integration_id,
                                  domain=self.replacement_domain, staff_identity=self.replacement_staff)
                connection.commit()
                connection.execute('BEGIN IMMEDIATE')
                rows = connection.execute('SELECT scope,payload FROM max_scopes').fetchall()
            if rows or existed:
                validate_snapshot(connection, school=self.school_id, integration_id=self.integration_id,
                                  domain=self.replacement_domain, staff_identity=self.replacement_staff)
                data = _json(rows[0][1])
            else:
                raise ValueError('Uninitialized MAX scope')
            yield data
            self.validate_profile()
            self._validate_scope(data)
            connection.execute('INSERT INTO max_scopes(scope,payload) VALUES(?,?) ON CONFLICT(scope) DO UPDATE SET payload=excluded.payload',
                               (self.scope, json.dumps(data, ensure_ascii=False, allow_nan=False)))
            validate_snapshot(connection, school=self.school_id, integration_id=self.integration_id,
                              domain=self.replacement_domain, staff_identity=self.replacement_staff)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
