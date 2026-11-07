"""Manual reconciliation of confirmed statement rows in the isolated school rehearsal."""

from contextlib import closing, contextmanager
from datetime import date, datetime, timezone
import json
import os
import re
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field, field_validator

from . import db, school_clubs, school_ops, staff_access
from .integration_settings import _flag
from .request_planning import digest
from .test_isolation import isolated_database_paths


KEY = '_schoolClubPayments'
SCHOOL = 'ideyafix-synthetic'
FLAG = 'SHKOLA_CLUB_RECONCILIATION_PREVIEW_ENABLED'
ACCOUNT = r'^test-account-[A-Za-z0-9_-]{1,48}$'
RECEIPT = r'^test-receipt-[A-Za-z0-9_-]{1,48}$'
CHARGE = r'^charge-[a-f0-9]{32}$'
LIMIT = school_clubs.MAX_RECORDS


def preview_enabled(school, host):
    if not _flag(os.environ, FLAG):
        return False
    try:
        domain, agent = isolated_database_paths()
        staff, bound = staff_access.configuration()
        if (school != SCHOOL or bound != school or host not in {'localhost', '127.0.0.1'}
                or not school_clubs.school_workspace() or domain.parent != agent.parent
                or staff.parent != domain.parent or staff in {domain, agent}):
            raise ValueError()
    except (OSError, ValueError, RuntimeError):
        raise RuntimeError('Сверка кружков доступна только в изолированной тестовой школе') from None
    return True


class Binding(school_clubs.Revision):
    chargeId: str = Field(pattern=CHARGE)
    accountRef: str = Field(pattern=ACCOUNT)


class Preview(Binding):
    receiptRef: str = Field(pattern=RECEIPT)
    amountKopeks: int = Field(gt=0, le=school_clubs.MAX_TOTAL_KOPEKS)
    receivedOn: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    sourceConfirmed: bool

    @field_validator('receivedOn')
    @classmethod
    def valid_date(cls, value):
        date.fromisoformat(value)
        return value


class Apply(Preview):
    previewHash: str = Field(pattern=r'^[a-f0-9]{64}$')
    requestId: str = Field(pattern=r'^[A-Za-z0-9_.:-]{16,80}$')


def _ledger(state):
    data = state.get(KEY)
    if data is None:
        return {'schoolId': SCHOOL, 'revision': 0, 'accounts': {}, 'receipts': {}, 'commands': {}}
    if (not isinstance(data, dict) or data.get('schoolId') != SCHOOL
            or type(data.get('revision')) is not int or data['revision'] < 0
            or any(not isinstance(data.get(key), dict) or len(data[key]) > LIMIT
                   for key in ('accounts', 'receipts', 'commands'))):
        raise HTTPException(503, 'Тестовый журнал сверки требует проверки')
    return data


def assert_cancellable(state, charge_id):
    if KEY in state and charge_id in _receipt_totals(_ledger(state)):
        raise HTTPException(409, 'У начисления есть подтверждённое поступление. Сначала нужна отдельная корректировка сверки.')


def _school_rows(rows):
    if not isinstance(rows, list) or len(rows) > LIMIT:
        raise ValueError()
    result = {}
    for row in rows:
        if isinstance(row, dict) and row.get('schoolId', SCHOOL) == SCHOOL:
            identity = row['id']
            if not isinstance(identity, str) or identity in result:
                raise ValueError()
            result[identity] = row
    return result


def _charges(state):
    clubs = school_clubs.store(state)
    try:
        programs = _school_rows(clubs['programs'])
        enrollments = _school_rows(clubs['enrollments'])
        students = _school_rows(school_clubs.students(state))
        eligible = {}
        for identity, charge in _school_rows(clubs['charges']).items():
            if charge.get('status') != 'confirmed' or charge.get('archived') is not False:
                continue
            entry = enrollments.get(charge['enrollmentId'])
            student = students.get(charge['studentId'])
            if (not entry or not student or student.get('archived') is not False
                    or entry['studentId'] != charge['studentId'] or entry['programId'] != charge['programId']
                    or charge['programId'] not in programs):
                continue
            if (not re.fullmatch(CHARGE, identity) or type(charge['amountKopeks']) is not int
                    or not 0 <= charge['amountKopeks'] <= school_clubs.MAX_TOTAL_KOPEKS):
                raise ValueError()
            eligible[identity] = charge
        return eligible
    except (KeyError, ValueError, TypeError):
        raise HTTPException(503, 'Связи клубных начислений требуют проверки') from None


def _target(state, charge_id):
    charge = _charges(state).get(charge_id)
    if charge is None:
        raise HTTPException(409, 'Подтверждённое начисление недоступно для сверки')
    return charge


def _charge_identity(charge):
    fields = ('id', 'studentId', 'programId', 'enrollmentId', 'period', 'amountKopeks')
    return digest([SCHOOL, {field: charge[field] for field in fields}])


def _receipt_totals(data, charges=None):
    by_charge = {}
    try:
        for ref, row in data['receipts'].items():
            amount, received_on, charge_id = row['amountKopeks'], row['receivedOn'], row['chargeId']
            if (not re.fullmatch(RECEIPT, ref) or not re.fullmatch(CHARGE, charge_id)
                    or row.get('sourceConfirmed') is not True
                    or not re.fullmatch(ACCOUNT, row['accountRef'])
                    or data['accounts'].get(row['studentId']) != row['accountRef']
                    or not re.fullmatch(r'[a-f0-9]{64}', row['chargeIdentity'])
                    or type(amount) is not int or not 0 < amount <= school_clubs.MAX_TOTAL_KOPEKS
                    or date.fromisoformat(received_on).isoformat() != received_on):
                raise ValueError()
            charge = charges.get(charge_id) if charges is not None else None
            if charge is not None and (charge['studentId'] != row['studentId']
                    or _charge_identity(charge) != row['chargeIdentity']):
                raise ValueError()
            total, last = by_charge.get(charge_id, (0, None))
            by_charge[charge_id] = total + amount, max(last or received_on, received_on)
        return by_charge
    except (KeyError, ValueError, TypeError):
        raise HTTPException(503, 'Тестовые поступления требуют проверки') from None


@contextmanager
def _write_state():
    path = db.database_path()
    if not path.is_file():
        raise HTTPException(409, 'Подтверждённое начисление недоступно для сверки')
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=15)) as connection:
            connection.execute('BEGIN IMMEDIATE')
            yield connection, school_ops._read_state(connection)
    except sqlite3.Error:
        raise HTTPException(503, 'Тестовое хранилище сверки недоступно') from None


def _save(connection, state, data):
    state[KEY] = data
    connection.execute('UPDATE demo_state SET payload=? WHERE id=1', (json.dumps(state, ensure_ascii=False, allow_nan=False),))
    connection.commit()


def _preview(state, data, body):
    school_clubs.current(data, body.expectedRevision)
    if not body.sourceConfirmed:
        raise HTTPException(422, 'Сначала подтвердите поступление по тестовой выписке')
    if body.receiptRef in data['receipts']:
        raise HTTPException(409, 'Поступление с этим кодом уже учтено')
    charge = _target(state, body.chargeId)
    if data['accounts'].get(charge['studentId']) != body.accountRef:
        raise HTTPException(409, 'Лицевой счёт не подтверждён для ученика начисления')
    current, _ = _receipt_totals(data, {body.chargeId: charge}).get(body.chargeId, (0, None))
    received = current + body.amountKopeks
    remaining = charge['amountKopeks'] - received
    if remaining < 0:
        raise HTTPException(409, 'Поступление превышает остаток начисления. Для переплаты нужен отдельный порядок.')
    proposal = {'amountKopeks': body.amountKopeks, 'receivedKopeks': received, 'remainingKopeks': remaining}
    fields = ('expectedRevision', 'chargeId', 'accountRef', 'receiptRef', 'amountKopeks', 'receivedOn', 'sourceConfirmed')
    return {**proposal, 'previewHash': digest([body.model_dump(include=set(fields)), charge, proposal])}, charge


def _replay(state, data, body, profile):
    old = data['commands'].get(body.requestId)
    if old is None:
        return None
    if (old['hash'] != digest(body.model_dump()) or old['member'] != profile['membershipId']
            or old['membershipRevision'] != profile['membershipRevision']):
        raise HTTPException(409, 'Код команды уже использован')
    _receipt_totals(data, {body.chargeId: _target(state, body.chargeId)})
    return old['response']


def student_receipts(learning, profile):
    student = learning._student(profile, learning._read())
    state = school_clubs.read_school_state()
    data = _ledger(state)
    charges = {key: row for key, row in _charges(state).items() if row['studentId'] == student['id']}
    totals = _receipt_totals(data, charges)
    receipts = []
    for key, charge in charges.items():
        received, last = totals.get(key, (0, None))
        if received > charge['amountKopeks']:
            raise HTTPException(503, 'Тестовая сверка начисления требует проверки')
        receipts.append({'title': charge['programTitle'], 'period': charge['period'],
                         'chargeKopeks': charge['amountKopeks'], 'receivedKopeks': received, 'lastReceivedOn': last})
    notices = [{'title': charges[row['chargeId']]['programTitle'], 'period': charges[row['chargeId']]['period'],
                'receivedOn': row['receivedOn'], 'amountKopeks': row['amountKopeks']}
               for row in data['receipts'].values() if row['chargeId'] in charges and row['studentId'] == student['id']]
    if learning._student(profile, learning._read())['id'] != student['id']:
        raise HTTPException(403, 'Личная привязка ученика изменилась')
    return {'receipts': sorted(receipts, key=lambda row: row['period'], reverse=True),
            'receiptNotices': sorted(notices, key=lambda row: row['receivedOn'], reverse=True)}


def router_for(learning, actor, require_session):
    router = APIRouter(prefix='/api/director/club-reconciliation')

    def authorize(request, profile):
        learning._actor(profile, {'director'})
        require_session(request, profile)

    @router.get('')
    def overview(request: Request, profile=Depends(actor)):
        authorize(request, profile)
        state = school_clubs.read_school_state()
        data = _ledger(state)
        eligible = _charges(state)
        totals = _receipt_totals(data, eligible)
        charges = []
        for key, row in eligible.items():
            received, _ = totals.get(key, (0, None))
            if received > row['amountKopeks']:
                raise HTTPException(503, 'Тестовая сверка начисления требует проверки')
            charges.append({'id': key, 'title': row['programTitle'], 'studentName': row['studentName'],
                            'className': row['className'], 'period': row['period'], 'amountKopeks': row['amountKopeks'],
                            'receivedKopeks': received, 'accountRef': data['accounts'].get(row['studentId'])})
        authorize(request, profile)
        return {'revision': data['revision'], 'charges': charges}

    @router.post('/binding')
    def bind(body: Binding, request: Request, profile=Depends(actor)):
        authorize(request, profile)
        with _write_state() as (connection, state):
            data = _ledger(state)
            student_id = _target(state, body.chargeId)['studentId']
            accounts = data['accounts']
            if accounts.get(student_id) == body.accountRef:
                authorize(request, profile)
                return {'revision': data['revision']}
            school_clubs.current(data, body.expectedRevision)
            if student_id in accounts or body.accountRef in accounts.values() or len(accounts) >= LIMIT:
                raise HTTPException(409, 'Лицевой счёт уже связан с учеником или достигнут лимит связей')
            accounts[student_id] = body.accountRef
            data['revision'] += 1
            authorize(request, profile)
            _save(connection, state, data)
            return {'revision': data['revision']}

    @router.post('/preview')
    def preview(body: Preview, request: Request, profile=Depends(actor)):
        authorize(request, profile)
        state = school_clubs.read_school_state()
        result, _ = _preview(state, _ledger(state), body)
        authorize(request, profile)
        return result

    @router.post('/apply')
    def apply(body: Apply, request: Request, profile=Depends(actor)):
        authorize(request, profile)
        snapshot = school_clubs.read_school_state()
        saved = _replay(snapshot, _ledger(snapshot), body, profile)
        if saved is not None:
            authorize(request, profile)
            return saved
        with _write_state() as (connection, state):
            data = _ledger(state)
            saved = _replay(state, data, body, profile)
            if saved is not None:
                authorize(request, profile)
                return saved
            checked, charge = _preview(state, data, body)
            if body.previewHash != checked['previewHash']:
                raise HTTPException(409, 'Исходные данные изменились. Повторите предварительную сверку.')
            if len(data['receipts']) >= LIMIT or len(data['commands']) >= LIMIT:
                raise HTTPException(409, 'Достигнут лимит тестовых поступлений')
            data['receipts'][body.receiptRef] = {
                **body.model_dump(include={'chargeId', 'accountRef', 'amountKopeks', 'receivedOn', 'sourceConfirmed', 'requestId'}),
                'studentId': charge['studentId'],
                'chargeIdentity': _charge_identity(charge),
                'confirmedAt': datetime.now(timezone.utc).isoformat(),
            }
            data['revision'] += 1
            result = {'revision': data['revision'], 'chargeId': body.chargeId,
                      'receivedKopeks': checked['receivedKopeks'], 'remainingKopeks': checked['remainingKopeks']}
            data['commands'][body.requestId] = {'hash': digest(body.model_dump()), 'member': profile['membershipId'],
                                              'membershipRevision': profile['membershipRevision'], 'response': result}
            authorize(request, profile)
            _save(connection, state, data)
            return result

    return router
