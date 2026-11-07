"""Explicit, school-scoped advisory extraction with durable no-repeat receipts.

The sidecar contains hashes, captured receive time and bounded extracted facts,
never prompts, profiles, provider responses, keys or exception messages. No
environment lookup, domain DB transaction, automatic retries or host wiring.
"""
from __future__ import annotations

from contextlib import contextmanager, closing
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date
import fcntl
import hashlib
import json
import os
import secrets

from fastapi import HTTPException
from .integration_storage import validate_private_path, open_private_sqlite

_service = ContextVar('school_request_extraction_service', default=None)
MAX_RECEIPTS = 10000


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def bounded_result(value):
    """Even a host-injected extractor cannot return authority or arbitrary data."""
    if not isinstance(value, dict) or not set(value) <= {'kind', 'date', 'slot'}:
        return {}
    if 'kind' in value and value['kind'] not in {'unavailability', 'add_lesson', 'change_lesson', 'question'}:
        return {}
    if 'slot' in value and (type(value['slot']) is not int or not 1 <= value['slot'] <= 20):
        return {}
    if 'date' in value:
        try:
            if type(value['date']) is not str or date.fromisoformat(value['date']).isoformat() != value['date']:
                return {}
        except (TypeError, ValueError):
            return {}
    return dict(value)


@dataclass(frozen=True)
class ExtractionAttempt:
    status: str
    advisory: dict


@dataclass(frozen=True)
class ExtractionReceipt:
    school_id: str
    key: str
    fingerprint: str
    proof: str


@dataclass(frozen=True)
class ExtractionResult:
    status: str
    received_at: str
    advisory: dict


@contextmanager
def bound_service(service):
    if service is not None and not isinstance(service, ExtractionService):
        raise TypeError('Explicit ExtractionService required')
    token = _service.set(service)
    try:
        yield service
    finally:
        _service.reset(token)


def current_service(school_id):
    service = _service.get()
    if service is not None and service.school_id != school_id:
        raise HTTPException(403, 'Извлечение не разрешено для этой школы')
    return service


class ExtractionService:
    def __init__(self, school_id, path, extractor, *, forbidden=()):
        if not isinstance(school_id, str) or not 1 <= len(school_id) <= 120 or not callable(extractor):
            raise ValueError('Explicit school and extractor required')
        self.forbidden = tuple(forbidden)
        self.school_id, self.path, self.extractor = school_id, validate_private_path(path, self.forbidden), extractor

    def _connect(self):
        connection = open_private_sqlite(self.path, {'request_extractions'}, forbidden=self.forbidden)
        connection.execute('PRAGMA busy_timeout=15000')
        connection.execute('''CREATE TABLE IF NOT EXISTS request_extractions (
            school_id TEXT NOT NULL, command_key TEXT NOT NULL, fingerprint TEXT NOT NULL,
            proof TEXT NOT NULL, received_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('started','completed','unknown')),
            result_json TEXT, PRIMARY KEY(school_id,command_key))''')
        connection.commit()
        return connection

    def _identity(self, operation, command_id, actor, payload):
        if actor.get('schoolId') != self.school_id:
            raise HTTPException(403, 'Извлечение относится к другой школе')
        # requestId scope matches the domain: it is shared by all operations and
        # actors within a school, so a collision cannot incur a second call.
        key = digest({'requestId': command_id})
        fingerprint = digest({'operation': operation, 'actor': actor, 'payload': payload})
        return key, fingerprint

    def prepare(self, operation, command_id, actor, payload, text, received_at, *, before_invoke=None):
        key, fingerprint = self._identity(operation, command_id, actor, payload)
        if not isinstance(text, str) or len(text) > 4000:
            raise HTTPException(422, 'Некорректный текст обращения')
        # A per-command OS lock releases on process death. The domain and the
        # sidecar SQLite write transactions are both closed during invocation.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_dir = self.path.parent / (self.path.name + '.locks')
        lock_dir.mkdir(mode=0o700, exist_ok=True)
        lock_path = validate_private_path(lock_dir / digest({'school': self.school_id, 'key': key}), self.forbidden)
        descriptor = os.open(lock_path, os.O_CREAT | os.O_NOFOLLOW | os.O_RDWR, 0o600)
        with os.fdopen(descriptor, 'a+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with closing(self._connect()) as c:
                c.execute('BEGIN IMMEDIATE')
                row = c.execute('SELECT fingerprint,proof,status FROM request_extractions WHERE school_id=? AND command_key=?', (self.school_id, key)).fetchone()
                if row:
                    if row[0] != fingerprint:
                        raise HTTPException(409, 'Идентификатор извлечения уже использован иначе')
                    # No lock holder remains from the old attempt: started is
                    # an indeterminate external outcome, never permission to retry.
                    if row[2] == 'started':
                        c.execute("UPDATE request_extractions SET status='unknown' WHERE school_id=? AND command_key=?", (self.school_id, key))
                    c.commit()
                    return ExtractionReceipt(self.school_id, key, fingerprint, row[1])
                if c.execute('SELECT COUNT(*) FROM request_extractions WHERE school_id=?', (self.school_id,)).fetchone()[0] >= MAX_RECEIPTS:
                    raise HTTPException(429, 'Достигнут лимит квитанций извлечения')
                proof = secrets.token_urlsafe(32)
                c.execute('INSERT INTO request_extractions VALUES(?,?,?,?,?,?,?)',
                    (self.school_id, key, fingerprint, proof, received_at, 'started', None))
                c.commit()
            # A command may have waited for another process. Re-authorize NOW,
            # outside SQLite transactions, before any first external call. A
            # rejected guard is a known zero-call outcome, not provider unknown.
            if before_invoke is not None:
                try:
                    before_invoke()
                except Exception:
                    with closing(self._connect()) as c:
                        c.execute("DELETE FROM request_extractions WHERE school_id=? AND command_key=? AND proof=? AND status='started'",
                            (self.school_id, key, proof))
                        c.commit()
                    raise
            try:
                if callable(getattr(self.extractor, 'extract_with_outcome', None)):
                    attempt = self.extractor.extract_with_outcome(text)
                    if not isinstance(attempt, ExtractionAttempt) or attempt.status not in {'completed', 'unknown'}:
                        raise ValueError('Invalid extraction outcome')
                    result, status = bounded_result(attempt.advisory), attempt.status
                else:
                    result, status = bounded_result(self.extractor(text)), 'completed'
                if status == 'unknown': result = {}
            except Exception:
                result, status = {}, 'unknown'
            # BaseException deliberately leaves durable started; a new process
            # records unknown instead of repeating the possibly completed call.
            with closing(self._connect()) as c:
                c.execute("UPDATE request_extractions SET status=?,result_json=? WHERE school_id=? AND command_key=? AND fingerprint=? AND proof=? AND status='started'",
                    (status, json.dumps(result, ensure_ascii=False), self.school_id, key, fingerprint, proof))
                c.commit()
            return ExtractionReceipt(self.school_id, key, fingerprint, proof)

    def resolve(self, receipt, operation, command_id, actor, payload):
        key, fingerprint = self._identity(operation, command_id, actor, payload)
        if (not isinstance(receipt, ExtractionReceipt) or receipt.school_id != self.school_id
                or receipt.key != key or receipt.fingerprint != fingerprint):
            raise HTTPException(409, 'Квитанция извлечения не соответствует команде')
        # Read only: consuming a receipt inside the domain transaction never
        # creates schema, locks a sidecar writer, or calls a provider.
        connection = open_private_sqlite(self.path, {'request_extractions'}, readonly=True, forbidden=self.forbidden)
        if connection is None:
            raise HTTPException(409, 'Квитанция извлечения не найдена')
        with closing(connection) as c:
            row = c.execute('SELECT fingerprint,proof,received_at,status,result_json FROM request_extractions WHERE school_id=? AND command_key=?', (self.school_id, key)).fetchone()
        if row is None or row[0] != fingerprint or not secrets.compare_digest(row[1], receipt.proof):
            raise HTTPException(409, 'Квитанция извлечения не подтверждена')
        if row[3] == 'started':
            raise HTTPException(409, 'Извлечение ещё выполняется; повторите получение результата')
        return ExtractionResult(row[3], row[2], bounded_result(json.loads(row[4] or '{}')) if row[3] == 'completed' else {})
