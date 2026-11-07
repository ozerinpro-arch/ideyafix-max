"""Explicit approval and immutable publication of a working-assumption version.

Uses the existing school's publication ledger and personal snapshot readers.
No normative plan, mutable timetable, calendar, identity, model or channel change.
"""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import json
import sqlite3
from uuid import uuid4
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path as APIPath
from fastapi.routing import APIRoute
from fastapi.exceptions import RequestValidationError
from pydantic import Field, field_validator

from . import db, school_publications as pub
from .auth import session_profile
from .school_plan_sources import load_source_package

KEY = '_schoolExperimentalDrafts'
MAX_APPROVAL_RECEIPTS = 100
ID_PATTERN = r'^exp_[a-f0-9]{32}$'


class VersionInput(pub.StrictInput):
    expectedRevision: int = Field(ge=1, le=2**31-1)
    expectedVersionHash: str = Field(pattern=r'^[a-f0-9]{64}$')


class ApprovalInput(VersionInput):
    requestId: str = Field(pattern=r'^[A-Za-z0-9_-]{16,80}$')
    acceptWorkingAssumptions: Literal[True]

    @field_validator('acceptWorkingAssumptions', mode='before')
    @classmethod
    def explicit_acceptance(cls, value):
        if type(value) is not bool or value is not True:
            raise ValueError('Нужно явно согласовать рабочие предположения выбранной версии.')
        return value


class PreviewInput(VersionInput):
    expectedActiveVersion: int = Field(ge=0, le=2**31-1)


class PublishInput(PreviewInput):
    requestId: str = Field(pattern=r'^[A-Za-z0-9_-]{16,80}$')
    previewHash: str = Field(pattern=r'^[a-f0-9]{64}$')


def _actor(profile, permission='publication.read'):
    from .school_requests import require_bound_school
    pub._actor(profile, permission)
    require_bound_school(profile['schoolId'])
    return profile



def _editor_profile(profile):
    # Reuse the existing, trusted planner→legacy-admin adapter after _actor.
    from .staff_policy import domain_profile
    return domain_profile(profile,('admin','director'),'/api/planning/experimental-school')


def _binding(profile):
    keys = ('userId','membershipId','membershipRevision','schoolId','role','staffRole',
            'domainRole','permissions','membershipVerified','identitySource','teacherRefs','calendarTeacherIds')
    return {key: deepcopy(profile[key]) for key in keys if key in profile}


def _read_state(c):
    row = c.execute('SELECT payload FROM demo_state WHERE id=1').fetchone() if pub._exists(c,'demo_state') else None
    if row is None:
        raise HTTPException(409, 'Экспериментальный черновик не найден.')
    try:
        value=json.loads(row[0])
        if not isinstance(value,dict):
            raise ValueError()
        return value
    except (TypeError,ValueError):
        raise HTTPException(503,'Сохранённые школьные данные требуют проверки.') from None


def _item(state, experiment_id, school_id):
    namespace = state.get(KEY)
    if (not isinstance(namespace,dict) or type(namespace.get('version')) is not int or namespace['version'] != 1
            or namespace.get('schoolId') != school_id or not isinstance(namespace.get('candidates'),list)
            or any(not isinstance(item,dict)or not isinstance(item.get('public'),dict)for item in namespace.get('candidates',[]))):
        raise HTTPException(409, 'Экспериментальный черновик не относится к этой школе.')
    matches = [item for item in namespace['candidates'] if item.get('public',{}).get('id') == experiment_id]
    if len(matches) != 1:
        raise HTTPException(404, 'Экспериментальный черновик не найден.')
    return matches[0]


def _version(c, experiment_id, body, profile):
    from .experimental_edits import read_version, validate_version
    value = read_version(c, experiment_id, _editor_profile(profile))
    if (type(value.get('revision')) is not int or value['revision'] != body.expectedRevision or value['versionHash'] != body.expectedVersionHash
            or value.get('experimentId') != experiment_id):
        raise HTTPException(409, 'Версия черновика изменилась. Обновите таблицу и согласуйте точную версию.')
    # Source provenance remains a condition, never confirmation of assumptions.
    from .experimental_school import assert_sources_current
    assert_sources_current(c,value.get('sourceBinding'),package_loader=load_source_package)
    valid = validate_version(value)
    if valid.get('valid') is not True:
        raise HTTPException(409, 'Версия не прошла независимую проверку ограничений.')
    from .dated_timetable import NAMESPACE, guard_saved_calendar
    state = _read_state(c)
    if NAMESPACE in state:
        guard_saved_calendar(state, profile['schoolId'], value['periodStart'], value['projection']['lessons'])
    return value, valid


def _approval(value, profile):
    approval = value.get('approval')
    if (not isinstance(approval,dict) or type(approval.get('revision')) is not int or approval.get('revision') != value['revision']
            or approval.get('versionHash') != value['versionHash']
            or approval.get('acceptWorkingAssumptions') is not True):
        raise HTTPException(409, 'Нужно согласовать точную текущую версию вместе с её рабочими предположениями.')
    approver = approval.get('actorBinding')
    if not isinstance(approver,dict) or approver.get('schoolId') != profile['schoolId']:
        raise HTTPException(409, 'Не подтверждён сотрудник, согласовавший эту версию.')
    try:
        _actor(approver, 'schedule.manage')
    except HTTPException:
        raise HTTPException(409, 'Доступ согласовавшего сотрудника изменился; требуется новое согласование.') from None
    return approval


def _materialize(value):
    from .experimental_edits import materialized_lessons
    return pub._canonical_grid(materialized_lessons(value))


def _prepare(c, experiment_id, body, profile):
    active = pub.read_active_snapshot(profile['schoolId'], c)
    version = active['version'] if active else 0
    if version != body.expectedActiveVersion:
        raise HTTPException(409, 'Активная публикация изменилась. Повторите предпросмотр.')
    value, validation = _version(c, experiment_id, body, profile)
    approval = _approval(value, profile)
    if (active and active.get('experimentalId') == experiment_id
            and active.get('experimentalRevision') == value['revision']
            and active.get('experimentalVersionHash') == value['versionHash']):
        raise HTTPException(409, 'Эта версия экспериментального черновика уже опубликована.')
    grid = _materialize(value)
    before = pub._canonical_grid(active['lessons']) if active else []
    evidence = {'mode':'working_assumptions','normativeConfirmed':False,'experimentalId':experiment_id,
                'experimentalRevision':value['revision'],'experimentalVersionHash':value['versionHash'],
                'sourceBinding':value['sourceBinding'],'approval':approval,'independentValidation':validation,
                'periodStart':value['periodStart'],'materializedHash':pub._hash(grid),
                'assumptions':deepcopy(value['assumptions']),'coverage':deepcopy(value['coverage']),
                'provenance':deepcopy(value['provenance']),
                'manualCorrections':value.get('manualCorrections') is True,
                'manualCorrectionAudit':deepcopy(value.get('manualCorrectionAudit',[]))}
    content = {'schoolId':profile['schoolId'],'actor':_binding(profile),'activeVersion':version,
               'evidence':evidence,'lessons':grid}
    return {'ready':True,'blockers':[],'activeVersion':version,'previewHash':pub._hash(content),
            'lessons':grid,'diff':pub._diff(before,grid),'evidence':evidence,
            'deliveryReadiness':pub._delivery_readiness()}


def approve_experiment(experiment_id, body, profile):
    _actor(profile,'schedule.manage')
    body = body if isinstance(body,ApprovalInput) else ApprovalInput.model_validate(body)
    fingerprint = pub._hash({'experimentId':experiment_id, **body.model_dump(exclude={'requestId'})})
    path = db.database_path()
    if not path.is_file():
        raise HTTPException(409,'Нет экспериментального черновика для согласования.')
    with closing(sqlite3.connect(path.as_uri()+'?mode=rw',uri=True,timeout=15,isolation_level=None)) as c:
        c.execute('BEGIN IMMEDIATE')
        try:
            _actor(profile,'schedule.manage')
            state = _read_state(c); item = _item(state,experiment_id,profile['schoolId'])
            receipts = item.setdefault('approvalReceipts',{})
            if not isinstance(receipts,dict):
                raise HTTPException(409,'Квитанции согласования требуют проверки.')
            previous = receipts.get(body.requestId)
            binding = _binding(profile)
            if previous is not None:
                if not isinstance(previous,dict)or not isinstance(previous.get('response'),dict):
                    raise HTTPException(409,'Сохранённая квитанция согласования повреждена.')
                if previous.get('fingerprint') != fingerprint or previous.get('actorBinding') != binding:
                    raise HTTPException(409,'Этот идентификатор согласования уже использован другим действием.')
                _actor(profile,'schedule.manage'); c.rollback()
                return deepcopy(previous['response'])
            if len(receipts) >= MAX_APPROVAL_RECEIPTS:
                raise HTTPException(429,'Достигнут предел квитанций согласования этой версии.')
            value, valid = _version(c,experiment_id,body,profile)
            approval = {'revision':value['revision'],'versionHash':value['versionHash'],
                        'actorBinding':binding,'approvedAt':datetime.now(timezone.utc).isoformat(),
                        'acceptWorkingAssumptions':True,'normativeConfirmed':False,
                        'independentValidation':valid}
            item['approval'] = approval
            response = {'experimentId':experiment_id,'revision':value['revision'],'versionHash':value['versionHash'],
                        'approval':deepcopy(approval),'approved':True,'normativeConfirmed':False}
            receipts[body.requestId] = {'fingerprint':fingerprint,'actorBinding':binding,'response':deepcopy(response)}
            from .experimental_school import _save
            _save(state, state[KEY])
            _actor(profile,'schedule.manage')
            c.execute('UPDATE demo_state SET payload=? WHERE id=1',(pub._json(state),))
            c.commit(); return response
        except BaseException:
            c.rollback(); raise


def preview_experimental_publication(experiment_id, body, profile):
    _actor(profile)
    body = body if isinstance(body,PreviewInput) else PreviewInput.model_validate(body)
    c = pub._read_connection()
    if c is None:
        raise HTTPException(409,'Нет экспериментального черновика для публикации.')
    try:
        c.execute('BEGIN')
        return _prepare(c,experiment_id,body,profile)
    finally:
        c.close()


def publish_experimental_publication(experiment_id, body, profile):
    _actor(profile,'publication.publish')
    body = body if isinstance(body,PublishInput) else PublishInput.model_validate(body)
    fingerprint = pub._hash({'kind':'experimental_publication','experimentId':experiment_id,
                             **body.model_dump(exclude={'requestId'})})
    path = db.database_path()
    if not path.is_file():
        raise HTTPException(409,'Нет согласованной экспериментальной версии.')
    with closing(sqlite3.connect(path.as_uri()+'?mode=rw',uri=True,timeout=15,isolation_level=None)) as c:
        c.execute('BEGIN IMMEDIATE')
        try:
            _actor(profile,'publication.publish')
            if pub._exists(c,'school_publication_receipts'):
                previous = pub._row(c,'SELECT * FROM school_publication_receipts WHERE request_id=?',(body.requestId,))
                if previous:
                    key=(profile['schoolId'],profile['userId'],profile.get('staffRole',profile['role']),profile['membershipId'],fingerprint)
                    if key != (previous['school_id'],previous['actor_id'],previous['actor_role'],previous['membership_id'],previous['request_hash']):
                        raise HTTPException(409,'Идентификатор публикации уже использован другим действием.')
                    _actor(profile,'publication.publish'); c.rollback()
                    return json.loads(previous['response_json'])
            prepared = _prepare(c,experiment_id,body,profile)
            if prepared['previewHash'] != body.previewHash:
                raise HTTPException(409,'Состав или согласование версии изменились. Повторите предпросмотр.')
            if pub._exists(c,pub.TABLE) and c.execute('SELECT COUNT(*) FROM school_publications WHERE school_id=?',(profile['schoolId'],)).fetchone()[0] >= pub.MAX_PUBLICATIONS:
                raise HTTPException(409,'Достигнут предел опубликованных версий.')
            pub._schema(c)
            evidence=prepared['evidence'];stamp=datetime.now(timezone.utc).isoformat();ident='pub_'+uuid4().hex
            snapshot={'publicationId':ident,'schoolId':profile['schoolId'],'version':prepared['activeVersion']+1,
                      'draftRevision':evidence['sourceBinding']['draftRevision'],'runId':experiment_id,
                      'periodStart':evidence['periodStart'],'calendarRevision':None,'calendarHash':None,
                      'lessons':prepared['lessons'],'publishedAt':stamp,'mode':'working_assumptions',
                      'workingAssumptions':True,'normativeConfirmed':False,'experimentalId':experiment_id,
                      'experimentalRevision':body.expectedRevision,'experimentalVersionHash':body.expectedVersionHash,
                      'assumptions':evidence['assumptions'],'coverage':evidence['coverage'],
                      'manualCorrections':evidence['manualCorrections'],
                      'manualCorrectionAudit':evidence['manualCorrectionAudit']}
            role=profile.get('staffRole',profile['role'])
            c.execute('INSERT INTO school_publications VALUES(?,?,?,?,?,?,?,?,?,?)',
                      (ident,profile['schoolId'],snapshot['version'],body.requestId,snapshot['draftRevision'],experiment_id,
                       pub._json(snapshot),pub._json(evidence),stamp,profile['userId']))
            c.execute('INSERT INTO school_publication_audit VALUES(?,?,?,?,?,?,?,?)',
                      (uuid4().hex,profile['schoolId'],ident,profile['userId'],role,'published_working_assumptions',stamp,pub._json(evidence)))
            outbox=pub._insert_outbox(c,ident,profile['schoolId'],snapshot,stamp)
            response={'publication':snapshot,'outbox':outbox,'deliveryReadiness':pub._delivery_readiness(),
                      'notice':'Рабочая версия опубликована внутри школы. Предположения не стали нормативными фактами; внешние сообщения не отправлялись.'}
            c.execute('INSERT INTO school_publication_receipts VALUES(?,?,?,?,?,?,?)',
                      (body.requestId,profile['schoolId'],profile['userId'],role,profile['membershipId'],fingerprint,pub._json(response)))
            _actor(profile,'publication.publish');_approval({'revision':body.expectedRevision,'versionHash':body.expectedVersionHash,'approval':evidence['approval']},profile)
            c.commit();return response
        except BaseException:
            c.rollback();raise


def publication_state(connection, experiment_id, profile):
    _actor(profile)
    active=pub.read_active_snapshot(profile['schoolId'],connection)
    result={'activeVersion':active['version']if active else 0,
            'publishedVersion':active['version']if active and active.get('experimentalId')==experiment_id else None,
            'publishedExperimentalRevision':active.get('experimentalRevision')if active and active.get('experimentalId')==experiment_id else None,
            'approvalValid':False,'canPublish':False,'blockers':[]}
    try:
        from .experimental_edits import read_version
        value=read_version(connection,experiment_id,_editor_profile(profile))
        body=VersionInput(expectedRevision=value['revision'],expectedVersionHash=value['versionHash'])
        current,_=_version(connection,experiment_id,body,profile)
        _approval(current,profile)
        result['approvalValid']=True
        result['canPublish']=not(active and active.get('experimentalId')==experiment_id
            and active.get('experimentalRevision')==value['revision']
            and active.get('experimentalVersionHash')==value['versionHash'])
    except HTTPException as error:
        result['blockers']=[{'code':'experimental_approval_required','message':str(error.detail)}]
    return result


class SafePublicationRoute(APIRoute):
    def get_route_handler(self):
        original=super().get_route_handler()
        async def handler(request):
            try:return await original(request)
            except RequestValidationError:
                raise HTTPException(422,'Некорректные поля согласования или публикации.') from None
        return handler


router=APIRouter(prefix='/experimental-school',tags=['experimental publication'],route_class=SafePublicationRoute)

def _reader(profile=Depends(session_profile)):
    return _actor(profile)

def _manager(profile=Depends(session_profile)):
    return _actor(profile,'schedule.manage')

def _publisher(profile=Depends(session_profile)):
    return _actor(profile,'publication.publish')

@router.post('/{experiment_id}/approval')
def approval_route(body:ApprovalInput,experiment_id:str=APIPath(pattern=ID_PATTERN),profile=Depends(_manager)):
    return approve_experiment(experiment_id,body,profile)

@router.post('/{experiment_id}/publication/preview')
def preview_route(body:PreviewInput,experiment_id:str=APIPath(pattern=ID_PATTERN),profile=Depends(_reader)):
    return preview_experimental_publication(experiment_id,body,profile)

@router.post('/{experiment_id}/publication/publish')
def publish_route(body:PublishInput,experiment_id:str=APIPath(pattern=ID_PATTERN),profile=Depends(_publisher)):
    return publish_experimental_publication(experiment_id,body,profile)
