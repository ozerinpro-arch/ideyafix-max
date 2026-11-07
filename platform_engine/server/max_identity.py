"""Host-scoped MAX identity proof and lazy private durable storage.

No environment reads, public routes, account creation or network calls. A challenge
proves only the already authenticated teacher and the observed private MAX user;
it never acts as a school login code. Only token hashes are stored.
"""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import secrets

from fastapi import HTTPException
from . import launch_runtime as authority
from .school_requests import require_bound_school
from .integration_storage import open_private_sqlite


def token_hash(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,100}', value):
        raise HTTPException(403, 'Подтверждение MAX не принято')
    return hashlib.sha256(value.encode('ascii')).hexdigest()


class MaxStore:
    """One private sidecar, separate from every domain/auth database; lazy I/O."""
    def __init__(self, db_path, school_id, integration_id, *, enabled=False,
                 replacement_domain=None, replacement_staff=None):
        self.path = Path(db_path) if db_path else None
        if (enabled is True and (self.path is None or not self.path.is_absolute())) or not isinstance(school_id, str) or not school_id or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', integration_id):
            raise ValueError('Explicit absolute MAX sidecar path and scope required')
        self.school_id, self.integration_id, self.enabled = school_id, integration_id, enabled is True
        if ((replacement_domain is None) != (replacement_staff is None)
                or any(not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value)
                       for value in (replacement_domain, replacement_staff) if value is not None)):
            raise ValueError('Expected pinned PostgreSQL and staff authorities')
        self.replacement_domain, self.replacement_staff = replacement_domain, replacement_staff
        scope = [school_id, integration_id]
        if replacement_domain is not None:
            scope.extend(['replacement-schedule', replacement_domain])
        self.scope = authority.digest(scope)

    def _validate_scope(self, data):
        if (data.get('schoolId') != self.school_id or data.get('integrationId') != self.integration_id
                or data.get('formatVersion') != 1 or data.get('replacementDomain') != self.replacement_domain
                or data.get('replacementStaff') != self.replacement_staff):
            raise RuntimeError('Invalid MAX scope')

    def require(self):
        if not self.enabled:
            raise HTTPException(503, 'MAX отключён')
        require_bound_school(self.school_id)

    def _empty(self):
        data = {'formatVersion':1, 'schoolId':self.school_id,
            'integrationId':self.integration_id, 'challenges':{}, 'bindings':{}, 'inbox':{}, 'outbox':{}, 'callbacks':{}}
        if self.replacement_domain is not None:
            data['replacementDomain'] = self.replacement_domain
            data['replacementStaff'] = self.replacement_staff
        return data

    def _connect(self):
        self.require()
        connection = open_private_sqlite(self.path,{'max_scopes'})
        connection.execute('PRAGMA busy_timeout=15000')
        connection.execute('CREATE TABLE IF NOT EXISTS max_scopes (scope TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        connection.commit()
        return connection

    @contextmanager
    def edit(self):
        con = self._connect()
        try:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT payload FROM max_scopes WHERE scope=?', (self.scope,)).fetchone()
            data = json.loads(row[0]) if row else self._empty()
            self._validate_scope(data)
            yield data
            if any(len(data[key]) > 5000 for key in ('challenges','bindings','inbox','outbox','callbacks')):
                raise HTTPException(429, 'Очередь MAX заполнена')
            payload = json.dumps(data, ensure_ascii=False, allow_nan=False)
            if len(payload.encode()) > 16 * 1024 * 1024:
                raise HTTPException(429, 'Хранилище MAX заполнено')
            con.execute('INSERT INTO max_scopes(scope,payload) VALUES(?,?) ON CONFLICT(scope) DO UPDATE SET payload=excluded.payload', (self.scope,payload))
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def read(self):
        # Disabled and absent-file status never creates/opens any database.
        self.require()
        con = open_private_sqlite(self.path,{'max_scopes'},readonly=True)
        if con is None:
            return {'challenges':{}, 'bindings':{}, 'inbox':{}, 'outbox':{}, 'callbacks':{}}
        try:
            row = con.execute('SELECT payload FROM max_scopes WHERE scope=?', (self.scope,)).fetchone()
            if not row: return {'challenges':{}, 'bindings':{}, 'inbox':{}, 'outbox':{}, 'callbacks':{}}
            data=json.loads(row[0])
            self._validate_scope(data)
            return data
        finally:
            con.close()


def teacher(profile, school_id):
    require_bound_school(school_id)
    actor = authority.actor(profile, 'launch.read')
    if actor['schoolId'] != school_id or actor.get('staffRole', actor.get('role')) != 'teacher':
        raise HTTPException(403, 'Личная привязка доступна только действующему учителю')
    return actor


def binding_current(data, ident, school_id, *, generation=None):
    binding = data['bindings'].get(ident)
    if (not binding or binding['state'] != 'verified' or binding['actor']['schoolId'] != school_id
            or (generation is not None and binding['generation'] != generation)):
        raise HTTPException(403, 'Личная привязка MAX недействительна')
    teacher(binding['actor'], school_id)
    return binding


def revoke_binding(data, binding):
    binding.update(state='revoked', revision=binding['revision']+1)
    for item in data['outbox'].values():
        if item['bindingId'] == binding['id'] and item['state'] == 'pending':
            item['state'] = 'cancelled'
    for item in data['callbacks'].values():
        if item['bindingId'] == binding['id']:
            item['state'] = 'revoked'


class MaxIdentity:
    def __init__(self, *, integration_id, school_id, db_path, enabled=False,
                 replacement_domain=None, replacement_staff=None, store=None):
        if store is not None:
            if (not isinstance(store, MaxStore) or store.school_id != school_id
                    or store.integration_id != integration_id or store.path != (Path(db_path) if db_path else None)
                    or store.enabled != (enabled is True) or store.replacement_domain != replacement_domain
                    or store.replacement_staff != replacement_staff):
                raise ValueError('Expected matching MAX store identity')
            self.store = store
        else:
            self.store = MaxStore(db_path, school_id, integration_id, enabled=enabled,
                                  replacement_domain=replacement_domain, replacement_staff=replacement_staff)
        self.school_id, self.integration_id = school_id, integration_id

    def issue_challenge(self, profile, ttl_ms=600000, *, before_return=None):
        self.store.require()
        actor = teacher(profile, self.school_id)
        if type(ttl_ms) is not int or not 1000 <= ttl_ms <= 600000:
            raise HTTPException(422, 'Недопустимый срок подтверждения')
        token = secrets.token_urlsafe(32); now = authority.now_ms()
        with self.store.edit() as data:
            teacher(actor,self.school_id)
            for old in data['challenges'].values():
                if old['actor']['membershipId'] == actor['membershipId'] and old['state']=='pending': old['state']='cancelled'
            data['challenges'][token_hash(token)] = {'actor':actor,'state':'pending','createdAt':now,'expiresAt':now+ttl_ms}
            if before_return is not None:
                before_return()
        return {'token':token,'expiresAt':now+ttl_ms}

    def _consume(self, data, event):
        """Only authenticated native bot_started intake reaches this host helper."""
        now = authority.now_ms()
        challenge = data['challenges'].get(event.get('startHash'))
        if (not challenge or challenge['state'] != 'pending' or challenge['expiresAt'] <= now
                or not challenge['createdAt'] <= event['timestamp'] <= now):
            raise HTTPException(403, 'Подтверждение MAX истекло или уже использовано')
        actor = teacher(challenge['actor'],self.school_id)
        for old in data['bindings'].values():
            if old['state']=='verified' and old['senderId']==event['senderId'] and old['actor']['membershipId']!=actor['membershipId']:
                raise HTTPException(409,'Личность MAX уже связана с другим сотрудником')
        for old in data['bindings'].values():
            if old['state']=='verified' and old['actor']['membershipId']==actor['membershipId']:
                revoke_binding(data,old)
        ident = 'mb_'+secrets.token_urlsafe(18)
        binding={'id':ident,'actor':actor,'senderId':event['senderId'],'chatId':event['chatId'],
            'state':'verified','revision':0,'generation':secrets.token_urlsafe(24),'verifiedAt':now}
        data['bindings'][ident]=binding
        challenge['state']='used'
        return binding

    def revoke(self,profile,*,before_return=None):
        self.store.require(); actor=teacher(profile,self.school_id)
        with self.store.edit() as data:
            teacher(actor,self.school_id)
            for b in data['bindings'].values():
                if b['actor']['membershipId']==actor['membershipId'] and b['state']=='verified': revoke_binding(data,b)
            for c in data['challenges'].values():
                if c['actor']['membershipId']==actor['membershipId'] and c['state']=='pending': c['state']='cancelled'
            if before_return is not None:
                before_return()
        return {'revoked':True}

    def status(self,profile):
        actor=teacher(profile,self.school_id)
        if not self.store.enabled: return {'enabled':False,'linked':False,'blocker':'max_disabled'}
        data=self.store.read()
        linked=any(b['state']=='verified' and b['actor']==actor for b in data['bindings'].values())
        return {'enabled':True,'linked':linked}
