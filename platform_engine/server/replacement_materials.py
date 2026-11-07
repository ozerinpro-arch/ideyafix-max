"""Explicit sharing of one private material version for one published replacement.

The preparation service remains the sole reader of the owner's library. This
separate private store contains only explicitly selected snapshots and receipts;
neither managers nor another teacher receive private library listings.
"""
from contextlib import closing, contextmanager, nullcontext
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import re
import sqlite3
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Path as APIPath
from pydantic import Field

from . import db, school_publications as publications, school_replacements as replacements, staff_access
from .auth import session_profile
from .integration_storage import open_private_sqlite, validate_private_path
from .school_requests import SafeRoute, require_bound_school
from .teacher_preparation import MaterialFields, Strict, TeacherPreparationService

TABLES = {'replacement_material_state'}
MAX_GRANTS, MAX_REQUESTS, MAX_RECEIPTS = 100, 1000, 1000
GRANT_ID = r'^share_[a-f0-9]{32}$'
REPLACEMENT_ID = r'^replacement_[a-f0-9]{32}$'
UUID = r'^[a-f0-9]{8}-[a-f0-9]{4}-[1-8][a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$'
NOTICE = ('Разрешение открывает только выбранную версию материала для одного урока. '
          'Чтение не подтверждает доставку или проведение занятия; внешние сообщения не отправляются.')


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _binding(actor):
    return {key: deepcopy(actor.get(key)) for key in ('schoolId', 'userId', 'membershipId',
        'membershipRevision', 'role', 'staffRole', 'permissions', 'teacherRefs', 'calendarTeacherIds')}


class Command(Strict):
    requestId: str = Field(pattern=UUID)
    expectedRevision: int = Field(ge=0, le=2**31 - 1)


class SelectedReplacement(Command):
    replacementId: str = Field(pattern=REPLACEMENT_ID)
    expectedPublicationVersion: int = Field(ge=1, le=2**31 - 1)


class GrantInput(SelectedReplacement):
    recipientMembershipId: str = Field(min_length=1, max_length=160)
    materialId: str = Field(min_length=1, max_length=128)
    expectedMaterialRevision: int = Field(ge=1, le=2**31 - 1)


class RevokeInput(Command):
    reason: str = Field(min_length=1, max_length=2000)


class ReplacementMaterialService:
    def __init__(self, school_id, path, preparation, *, forbidden=(), storage=None):
        if not isinstance(preparation, TeacherPreparationService) or preparation.school_id != school_id:
            raise ValueError('Use the configured own-material preparation service')
        self.school_id, self.preparation = school_id, preparation
        self.forbidden = (*preparation.forbidden, preparation.path, *forbidden)
        self.storage = storage
        self.path = validate_private_path(path, self.forbidden) if storage is None else None

    def _actor(self, actor, *, manager=False):
        require_bound_school(self.school_id)
        if (not isinstance(actor, dict) or actor.get('schoolId') != self.school_id
                or actor.get('membershipVerified') is not True
                or not isinstance(actor.get('membershipId'), str)):
            raise HTTPException(403, 'Нет действующего школьного доступа')
        current = staff_access.resolve_membership(self.school_id, actor['membershipId'])
        if not current or not staff_access.membership_current(actor) or _binding(current) != _binding(actor):
            raise HTTPException(403, 'Доступ или привязка сотрудника изменились')
        if current.get('staffRole') not in ({'director', 'planner'} if manager else {'teacher'}):
            raise HTTPException(403, 'Содержимое доступно только участникам конкретной передачи')
        return current

    def _empty(self):
        return {'version': 1, 'schoolId': self.school_id, 'revision': 0,
                'grants': {}, 'requests': {}, 'receipts': {}}

    def _validate_store(self, connection, *, empty=False):
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if empty and not tables:
            return
        if (tables != TABLES or [r[1] for r in connection.execute('PRAGMA table_info(replacement_material_state)')]
                != ['school_id', 'payload']):
            raise ValueError('Unknown material-sharing store')
        if connection.execute('SELECT school_id FROM replacement_material_state').fetchall() != [(self.school_id,)]:
            raise ValueError('Foreign material-sharing store')

    def _connect(self, *, readonly=True):
        try:
            existing = open_private_sqlite(self.path, TABLES, readonly=True, forbidden=self.forbidden)
            initialized = False
            if existing is not None:
                with closing(existing):
                    self._validate_store(existing, empty=True)
                    initialized = bool(existing.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone())
            if readonly:
                return open_private_sqlite(self.path, TABLES, readonly=True, forbidden=self.forbidden)
            connection = open_private_sqlite(self.path, TABLES, forbidden=self.forbidden)
            try:
                if not initialized:
                    connection.execute('BEGIN IMMEDIATE')
                    self._validate_store(connection, empty=True)
                    connection.execute('CREATE TABLE IF NOT EXISTS replacement_material_state(school_id TEXT PRIMARY KEY,payload TEXT)')
                    connection.execute('INSERT OR IGNORE INTO replacement_material_state VALUES(?,?)',
                                       (self.school_id, _json(self._empty())))
                    connection.commit()
                self._validate_store(connection)
                return connection
            except BaseException:
                connection.close()
                raise
        except (ValueError, OSError, sqlite3.Error):
            raise HTTPException(503, 'Отдельное хранилище передач недоступно; данные не заменены') from None

    def _load(self, connection):
        if connection is None:
            return self._empty()
        try:
            if self.storage is None and not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
                return self._empty()
            raw = connection.execute('SELECT payload FROM replacement_material_state WHERE school_id=?', (self.school_id,)).fetchone()
            if raw is None:
                # An empty file is a read-only empty state, not an invitation to
                # create private storage during a GET.
                if self.storage is None:
                    self._validate_store(connection, empty=True)
                return self._empty()
            value = json.loads(raw[0])
            if (set(value) != set(self._empty()) or type(value['version']) is not int or value['version'] != 1
                    or value['schoolId'] != self.school_id or type(value['revision']) is not int
                    or not 0 <= value['revision'] < 2**31
                    or any(not isinstance(value[k], dict) for k in ('grants', 'requests', 'receipts'))
                    or len(value['grants']) > MAX_GRANTS or len(value['requests']) > MAX_REQUESTS
                    or len(value['receipts']) > MAX_RECEIPTS or len(raw[0].encode()) > 24 * 1024 * 1024):
                raise ValueError()
            for identifier, row in value['receipts'].items():
                if (not re.fullmatch(UUID, identifier) or set(row) != {'actor', 'fingerprint', 'response'}
                        or row['actor']['schoolId'] != self.school_id or not re.fullmatch(r'[a-f0-9]{64}', row['fingerprint'])
                        or not isinstance(row['response'], dict) or 'material' in row['response']):
                    raise ValueError()
            creation_revisions = set()
            for identifier, row in value['grants'].items():
                required = {'id', 'schoolId', 'owner', 'recipient', 'replacementId', 'lessonId', 'date', 'binding',
                    'material', 'materialHash', 'materialRevision', 'status', 'createdAt', 'revokedAt', 'history'}
                if (not isinstance(row, dict) or not required <= row.keys()
                        or not row.keys() <= required | {'createdRevision', 'revokedRevision'}
                        or not re.fullmatch(GRANT_ID, identifier) or row['id'] != identifier
                        or row['status'] not in {'shared', 'revoked'} or row['schoolId'] != self.school_id
                        or row['owner']['schoolId'] != self.school_id or row['recipient']['schoolId'] != self.school_id
                        or row['materialHash'] != _hash(row['material']) or type(row['materialRevision']) is not int
                        or not 1 <= row['materialRevision'] == row['material']['revision']
                        or not re.fullmatch(REPLACEMENT_ID, row['replacementId']) or not isinstance(row['history'], list)
                        or len(row['history']) != (1 if row['status'] == 'shared' else 2)
                        or (row['revokedAt'] is None) != (row['status'] == 'shared')):
                    raise ValueError()
                MaterialFields.model_validate({k: row['material'][k] for k in MaterialFields.model_fields})
                created = self._sequence(value, identifier, 'shared')
                revoked = self._sequence(value, identifier, 'revoked') if row['status'] == 'revoked' else None
                if (type(row.get('createdRevision', created)) is not int
                        or row.get('createdRevision', created) != created or created in creation_revisions
                        or row.get('revokedRevision', revoked) != revoked or (revoked is not None and revoked <= created)):
                    raise ValueError()
                # Legacy v1 rows have sufficient committed receipts. Normalize
                # only this detached read; never infer order from UUID/clock.
                row.update(createdRevision=created, revokedRevision=revoked)
                creation_revisions.add(created)
            for identifier, row in value['requests'].items():
                if (not re.fullmatch(REPLACEMENT_ID, identifier) or not {'actor', 'createdAt'} <= row.keys()
                        or not row.keys() <= {'actor', 'createdAt', 'revision'}
                        or row['actor']['schoolId'] != self.school_id or not isinstance(row['createdAt'], str)):
                    raise ValueError()
                revision = self._sequence(value, identifier, 'requested', row['actor'])
                if type(row.get('revision', revision)) is not int or row.get('revision', revision) != revision:
                    raise ValueError()
                row['revision'] = revision
            return value
        except (KeyError, ValueError, TypeError, AttributeError, sqlite3.Error):
            raise HTTPException(409, 'История передач повреждена; доступ не открыт') from None

    @staticmethod
    def _sequence(data, identifier, status, actor=None):
        revisions = set()
        for receipt in data['receipts'].values():
            response = receipt['response']
            if status == 'requested':
                matches = (response.get('status') == status and response.get('replacementId') == identifier
                           and receipt['actor'] == actor)
            else:
                grant = response.get('grant', {})
                matches = grant.get('id') == identifier and grant.get('status') == status
            if matches:
                revision = response['revision']
                if type(revision) is not int or not 0 < revision <= data['revision']:
                    raise ValueError('Invalid committed receipt sequence')
                revisions.add(revision)
        if not revisions or (status != 'requested' and len(revisions) != 1):
            raise ValueError('Missing or ambiguous committed receipt sequence')
        return max(revisions)

    @contextmanager
    def _domain(self, *, write=False):
        if self.storage is not None:
            if write:
                with self.storage.locked_domain() as (connection, _):
                    yield connection
            else:
                with closing(self.storage.read_connection()) as connection:
                    connection.execute('BEGIN')
                    yield connection
            return
        path = db.database_path()
        if not path.is_file():
            raise HTTPException(409, 'Сначала нужна опубликованная замена')
        with closing(sqlite3.connect(path.as_uri() + ('?mode=rw' if write else '?mode=ro'), uri=True, timeout=15)) as connection:
            # A grant must not race a changed assignment. This transaction only
            # locks the school DB; no school table or publication is written.
            connection.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield connection

    def _published(self, connection):
        try:
            active = publications.read_active_snapshot(self.school_id, connection)
            if not active:
                return None, []
            records = replacements._ns(replacements._state(connection), self.school_id)['records']
            replacements._origin_publication(connection, active, self.school_id)
            current, seen, selected, selected_lessons = active, set(), [], set()
            lessons = {r['id']: r for r in active['lessons']}
            while current.get('replacementId'):
                identifier = current['replacementId']
                if identifier in seen or len(seen) >= replacements.LIMIT:
                    raise ValueError()
                seen.add(identifier)
                record = records[identifier]
                lesson = lessons.get(record['lessonId'])
                if (lesson is not None and record['lessonId'] not in selected_lessons
                        and replacements._grid([lesson]) == replacements._grid([record['after']])):
                    selected.append(record)
                    selected_lessons.add(record['lessonId'])
                parent = publications._row(connection, 'SELECT snapshot_json FROM school_publications WHERE id=? AND school_id=?',
                                           (current['parentPublicationId'], self.school_id))
                current = json.loads(parent['snapshot_json'])
            return active, selected
        except (KeyError, ValueError, TypeError, AttributeError):
            raise HTTPException(409, 'Связь замены с текущей публикацией требует проверки') from None

    def _respondents(self):
        return [p for p in staff_access.list_respondents(self.school_id)
                if p.get('staffRole') == 'teacher' and staff_access.membership_current(p)]

    def _people(self, record, respondents=None):
        before, after = record['before']['teachers'], record['after']['teachers']
        if len(before) != 1 or len(after) != 1 or before == after:
            raise HTTPException(409, 'Нужна однозначная персональная замена')
        respondents = self._respondents() if respondents is None else respondents
        owner = [p for p in respondents if before[0] in p.get('teacherRefs', [])]
        recipient = [p for p in respondents if after[0] in p.get('teacherRefs', [])]
        if len(owner) != 1 or len(recipient) != 1 or owner[0]['membershipId'] == recipient[0]['membershipId']:
            raise HTTPException(409, 'Подтвердите единственные учётные записи обоих педагогов замены')
        return owner[0], recipient[0]

    def _selected(self, connection, actor, body, *, sender):
        active, records = self._published(connection)
        record = next((r for r in records if r['id'] == body.replacementId), None)
        if not record or active['version'] != body.expectedPublicationVersion:
            raise HTTPException(409, 'Опубликованная замена изменилась; обновите список')
        owner, recipient = self._people(record)
        participant = owner if sender else recipient
        if _binding(actor) != _binding(participant):
            raise HTTPException(403, 'Вы не являетесь выбранным участником этой замены')
        actual_date = (date.fromisoformat(active['periodStart']) + timedelta(days=record['after']['day'])).isoformat()
        if actual_date != record['date']:
            raise HTTPException(409, 'Дата опубликованной замены изменилась')
        binding = {'publicationId': active['publicationId'], 'publicationVersion': active['version'],
            'publicationHash': _hash(active), 'replacementId': record['id'], 'lessonId': record['lessonId'],
            'date': record['date'], 'before': replacements._grid([record['before']])[0],
            'after': replacements._grid([record['after']])[0]}
        return record, owner, recipient, binding

    def _cas(self, data, body):
        if data['revision'] != body.expectedRevision or data['revision'] >= 2**31 - 1:
            raise HTTPException(409, 'Передачи материалов изменились; обновите список')

    def _replay(self, data, actor, body, operation):
        fingerprint = _hash({'operation': operation, 'body': body.model_dump(), 'actor': _binding(actor)})
        old = data['receipts'].get(body.requestId)
        if old:
            if old['fingerprint'] != fingerprint or old['actor'] != _binding(actor):
                raise HTTPException(409, 'UUID уже использован для другого действия')
            self._actor(actor)
            return fingerprint, deepcopy(old['response'])
        if len(data['receipts']) >= MAX_RECEIPTS:
            raise HTTPException(429, 'Достигнут предел квитанций передач')
        return fingerprint, None

    def _save(self, connection, data, actor, body, fingerprint, response):
        data['revision'] += 1
        response = {'revision': data['revision'], **response, 'notice': NOTICE}
        data['receipts'][body.requestId] = {'actor': _binding(actor), 'fingerprint': fingerprint, 'response': deepcopy(response)}
        payload = _json(data)
        if len(payload.encode()) > 24 * 1024 * 1024:
            raise HTTPException(429, 'Достигнут предел выбранных версий материалов')
        self._actor(actor)
        if self.storage is None:
            connection.execute('UPDATE replacement_material_state SET payload=? WHERE school_id=?', (payload, self.school_id))
            connection.commit()
        else:
            connection.execute(
                'INSERT INTO replacement_material_state(school_id,payload) VALUES(?,?) '
                'ON CONFLICT(school_id) DO UPDATE SET payload=excluded.payload', (self.school_id, payload)
            )
        return response

    def _read(self, domain=None):
        if self.storage is not None:
            return self._load(domain)
        connection = self._connect()
        with closing(connection) if connection is not None else nullcontext():
            if connection is not None:
                connection.execute('BEGIN')
            return self._load(connection)

    @contextmanager
    def _write_store(self, domain=None):
        if self.storage is not None:
            yield domain
        else:
            with closing(self._connect(readonly=False)) as connection:
                connection.execute('BEGIN IMMEDIATE')
                yield connection

    @staticmethod
    def _metadata(row):
        return {key: deepcopy(row[key]) for key in ('id', 'replacementId', 'lessonId', 'date',
            'status', 'createdAt', 'revokedAt', 'materialRevision', 'history')}

    def _material_binding(self, connection, material, record):
        if (material.get('lessonId') not in (None, record['lessonId'])
                or material.get('className') not in ('', record['before']['className'])
                or material.get('subject') not in ('', record['before']['subject'])):
            raise HTTPException(409, 'Материал относится к другому уроку, классу или предмету')
        if material.get('lessonId') is None:
            return  # The explicit grant binds this chosen personal draft now.
        try:
            row = publications._row(connection,
                'SELECT snapshot_json FROM school_publications WHERE school_id=? AND version=?',
                (self.school_id, material['publicationVersion']))
            published = json.loads(row['snapshot_json']) if row else None
            lesson = next((r for r in published['lessons'] if r['id'] == material['lessonId']), None)
            when = (date.fromisoformat(published['periodStart']) + timedelta(days=lesson['day'])).isoformat()
            if (published['schoolId'] != self.school_id or material['bindingStatus'] != 'own_published_lesson'
                    or when != record['date'] or replacements._grid([lesson]) != replacements._grid([record['before']])):
                raise ValueError()
        except (KeyError, ValueError, TypeError, AttributeError):
            raise HTTPException(409, 'Материал связан с другой датой или версией урока; выберите соответствующий личный черновик') from None

    def list(self, actor, *, manager=False):
        actor = self._actor(actor, manager=manager)
        data = self._read() if self.storage is None else None
        with self._domain() as domain:
            if self.storage is not None:
                data = self._load(domain)
            active, records = self._published(domain)
            respondents = self._respondents() if records else []
            active_hash = _hash(active)
            items = []
            for record in records:
                role = None
                refs = actor.get('teacherRefs', [])
                if not manager:
                    role = 'sender' if set(refs) & set(record['before']['teachers']) else 'recipient' if set(refs) & set(record['after']['teachers']) else None
                    if role is None:
                        continue
                ready, recipient_id = True, None
                try:
                    owner, recipient = self._people(record, respondents)
                    if not manager and actor['membershipId'] != (owner if role == 'sender' else recipient)['membershipId']:
                        continue
                    if role == 'sender':
                        recipient_id = recipient['membershipId']
                except HTTPException:
                    ready = False
                grant = max((r for r in data['grants'].values() if r['replacementId'] == record['id']),
                            key=lambda r: r['createdRevision'], default=None)
                request = data['requests'].get(record['id'])
                status = grant['status'] if grant else 'requested' if request else 'missing'
                if grant and request and grant['revokedRevision'] is not None and request['revision'] > grant['revokedRevision']:
                    status = 'requested'
                if grant and (grant['binding']['publicationHash'] != active_hash or not ready
                        or _binding(owner) != grant['owner'] or _binding(recipient) != grant['recipient']):
                    status = 'stale'
                items.append({'replacementId': record['id'], 'lessonId': record['lessonId'], 'date': record['date'],
                    'className': record['before']['className'], 'subject': record['before']['subject'],
                    'originalTeacher': record['before']['teachers'][0], 'replacementTeacher': record['after']['teachers'][0],
                    'role': role, 'recipientMembershipId': recipient_id, 'bindingReady': ready, 'status': status,
                    'grant': self._metadata(grant) if grant else None})
            self._actor(actor, manager=manager)
            history = [dict(self._metadata(row), stale=row['binding']['publicationHash'] != active_hash)
                for row in sorted(data['grants'].values(), key=lambda r: r['createdRevision']) if manager or actor['membershipId'] in
                (row['owner']['membershipId'], row['recipient']['membershipId'])]
            return {'revision': data['revision'], 'publicationVersion': active['version'] if active else None,
                    'items': items, 'history': history, 'notice': NOTICE}

    def request(self, actor, body):
        actor = self._actor(actor); body = SelectedReplacement.model_validate(body)
        with self._domain(write=True) as domain:
            data = self._read(domain)
            fingerprint, old = self._replay(data, actor, body, 'request')
            if old is not None:
                return old
            self._cas(data, body)
            record, _, _, _ = self._selected(domain, actor, body, sender=False)
            if len(data['requests']) >= MAX_REQUESTS:
                raise HTTPException(429, 'Достигнут предел запросов материалов')
            if any(g['replacementId'] == record['id'] and g['status'] == 'shared' for g in data['grants'].values()):
                raise HTTPException(409, 'Материал уже выбран; обновите список')
            self._actor(actor)
            with self._write_store(domain) as connection:
                if self.storage is None:
                    data = self._load(connection); self._cas(data, body)
                data['requests'][record['id']] = {'actor': _binding(actor), 'createdAt': datetime.now(timezone.utc).isoformat(),
                                                'revision': data['revision'] + 1}
                return self._save(connection, data, actor, body, fingerprint, {'status': 'requested', 'replacementId': record['id']})

    def grant(self, actor, body):
        actor = self._actor(actor); body = GrantInput.model_validate(body)
        with self._domain(write=True) as domain:
            data = self._read(domain)
            fingerprint, old = self._replay(data, actor, body, 'grant')
            if old is not None:
                return old
            self._cas(data, body)
            record, owner, recipient, binding = self._selected(domain, actor, body, sender=True)
            if recipient['membershipId'] != body.recipientMembershipId:
                raise HTTPException(403, 'Получатель отличается от учителя опубликованной замены')
            if len(data['grants']) >= MAX_GRANTS:
                raise HTTPException(429, 'Достигнут предел выбранных материалов')
            if any(g['replacementId'] == record['id'] and g['status'] == 'shared' for g in data['grants'].values()):
                raise HTTPException(409, 'Сначала отзовите прежнюю передачу этой замены')
            # This is the only library read: the ordinary ownership-checked API,
            # invoked as the original teacher, for this explicitly selected ID.
            material = self.preparation.get_material(actor, body.materialId)['material']
            if material['revision'] != body.expectedMaterialRevision:
                raise HTTPException(409, 'Выбранная версия материала изменилась')
            self._material_binding(domain, material, record)
            stamp = datetime.now(timezone.utc).isoformat()
            row = {'id': 'share_' + uuid4().hex, 'schoolId': self.school_id, 'owner': _binding(owner),
                'recipient': _binding(recipient), 'replacementId': record['id'], 'lessonId': record['lessonId'],
                'date': record['date'], 'binding': binding, 'material': deepcopy(material), 'materialHash': _hash(material),
                'materialRevision': material['revision'], 'status': 'shared', 'createdAt': stamp, 'revokedAt': None,
                'createdRevision': data['revision'] + 1, 'revokedRevision': None,
                'history': [{'action': 'shared', 'at': stamp}]}
            if len(_json(row).encode()) > 192 * 1024:
                raise HTTPException(422, 'Выбранная версия превышает допустимый объём')
            self._actor(recipient)
            self._actor(actor)
            with self._write_store(domain) as connection:
                if self.storage is None:
                    data = self._load(connection); self._cas(data, body)
                data['grants'][row['id']] = row
                return self._save(connection, data, actor, body, fingerprint, {'grant': self._metadata(row)})

    def get(self, actor, identifier):
        actor = self._actor(actor)
        if self.storage is not None:
            # Keep publication and revocation serialized with disclosure.
            with self._domain(write=True) as domain:
                return self._get(actor, identifier, self._load(domain), domain)
        connection = self._connect()
        with closing(connection) if connection is not None else nullcontext():
            if connection is not None:
                connection.execute('BEGIN')
            return self._get(actor, identifier, self._load(connection))

    def _get(self, actor, identifier, data, domain=None):
        row = data['grants'].get(identifier)
        if row is None:
            raise HTTPException(404, 'Передача не найдена')
        if _binding(actor) not in (row['owner'], row['recipient']):
            raise HTTPException(403, 'Содержимое другого педагога закрыто')
        if row['status'] != 'shared':
            raise HTTPException(403, 'Разрешение отозвано')
        with nullcontext(domain) if domain is not None else self._domain() as domain:
            selector = GrantInput(requestId=str(uuid4()), expectedRevision=data['revision'],
                replacementId=row['replacementId'], expectedPublicationVersion=row['binding']['publicationVersion'],
                recipientMembershipId=row['recipient']['membershipId'], materialId=row['material']['id'],
                expectedMaterialRevision=row['materialRevision'])
            _, owner, recipient, binding = self._selected(domain, actor, selector,
                sender=actor['membershipId'] == row['owner']['membershipId'])
            if binding != row['binding'] or _binding(owner) != row['owner'] or _binding(recipient) != row['recipient']:
                raise HTTPException(409, 'Публикация или участники передачи изменились')
            # A peer can lose access after the initial selection. Recheck
            # both authoritative bindings immediately before disclosing
            # content, while the publication/read transactions stay open.
            for participant, expected in ((owner, row['owner']), (recipient, row['recipient'])):
                if _binding(self._actor(participant)) != expected:
                    raise HTTPException(409, 'Привязка участника передачи изменилась')
            self._actor(actor)
            return {'grant': self._metadata(row), 'material': deepcopy(row['material']),
                    'lesson': {k: row[k] for k in ('replacementId', 'lessonId', 'date')}, 'notice': NOTICE}

    def revoke(self, actor, identifier, body):
        actor = self._actor(actor); body = RevokeInput.model_validate(body)
        if self.storage is not None:
            with self._domain(write=True) as domain:
                return self._revoke(domain, actor, identifier, body)
        connection = self._connect()
        if connection is None:
            raise HTTPException(404, 'Передача не найдена')
        connection.close()
        with self._write_store() as connection:
            return self._revoke(connection, actor, identifier, body)

    def _revoke(self, connection, actor, identifier, body):
        data = self._load(connection)
        fingerprint, old = self._replay(data, actor, body, 'revoke:' + identifier)
        if old is not None:
            return old
        self._cas(data, body)
        row = data['grants'].get(identifier)
        if row is None:
            raise HTTPException(404, 'Передача не найдена')
        if row['owner']['membershipId'] != actor['membershipId']:
            raise HTTPException(403, 'Отозвать передачу может её автор')
        if row['status'] != 'shared':
            raise HTTPException(409, 'Разрешение уже отозвано')
        stamp = datetime.now(timezone.utc).isoformat()
        row.update(status='revoked', revokedAt=stamp, revokedRevision=data['revision'] + 1)
        row['history'].append({'action': 'revoked', 'at': stamp, 'reason': body.reason})
        return self._save(connection, data, actor, body, fingerprint, {'grant': self._metadata(row)})


def create_router(service):
    """Mount on the existing staff app; its native role/CSRF policy also applies."""
    router = APIRouter(route_class=SafeRoute, tags=['replacement materials'])

    @router.get('/api/teacher/replacement-materials')
    def own(profile=Depends(session_profile)):
        return service.list(profile)

    @router.get('/api/school/replacement-materials')
    def manager(profile=Depends(session_profile)):
        return service.list(profile, manager=True)

    @router.post('/api/teacher/replacement-materials/requests')
    def request(body: SelectedReplacement, profile=Depends(session_profile)):
        return service.request(profile, body.model_dump())

    @router.post('/api/teacher/replacement-materials/grants')
    def grant(body: GrantInput, profile=Depends(session_profile)):
        return service.grant(profile, body.model_dump())

    @router.get('/api/teacher/replacement-materials/grants/{identifier}')
    def get(identifier: str = APIPath(pattern=GRANT_ID), profile=Depends(session_profile)):
        return service.get(profile, identifier)

    @router.post('/api/teacher/replacement-materials/grants/{identifier}/revoke')
    def revoke(body: RevokeInput, identifier: str = APIPath(pattern=GRANT_ID), profile=Depends(session_profile)):
        return service.revoke(profile, identifier, body.model_dump())

    return router
