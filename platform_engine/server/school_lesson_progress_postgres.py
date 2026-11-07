"""Lesson journal exchange on one explicitly selected school PostgreSQL domain.

Parsing, published-lesson binding, receipts and downloads retain the existing
exchange protocol. Only lessonProgress is exposed; schema provisioning and the
other exchange datasets are outside this adapter.
"""
from contextlib import ExitStack, closing, contextmanager
from copy import deepcopy
import json
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from . import school_data_exchange as exchange, school_lesson_progress as progress, school_ops as ops
from .school_postgres import SchoolPostgres, SchoolPostgresSettings


FORMATS = ('csv', 'xlsx', 'json')
UNAVAILABLE = 'Выбранное школьное хранилище журнала требует проверки'


def _kind(kind):
    if kind != progress.KIND:
        raise HTTPException(422, 'В этом школьном профиле подключён только журнал lessonProgress')


class LessonProgressPostgres:
    def __init__(self, storage: SchoolPostgres):
        if type(storage) is not SchoolPostgres or type(storage.settings) is not SchoolPostgresSettings:
            raise ValueError('Lesson journal requires the selected school PostgreSQL storage')
        self.storage = storage

    def _actor(self, profile):
        actor = exchange._authorized(profile)
        if self.storage.settings.school != actor['schoolId']:
            raise HTTPException(403, 'Журнал относится к другой школе')
        return actor

    def _source(self, connection):
        if connection is None or getattr(connection, 'settings', None) != self.storage.settings:
            raise HTTPException(503, UNAVAILABLE)

    @contextmanager
    def _read(self, profile):
        actor = self._actor(profile)
        selected = self.storage.read_connection()
        if selected is None:
            raise HTTPException(503, UNAVAILABLE)
        with closing(selected) as connection:
            self._source(connection)
            connection.execute('BEGIN')
            state = ops._read_state(connection, required=True)
            exchange._ledger(state, actor['schoolId'])
            yield actor, connection, state

    @staticmethod
    def _preview(body, actor, connection, state, resources):
        # The shared lessonProgress branch does not use draft/configuration.
        # Do not read unrelated planning tables or run their SQLite setup paths.
        return exchange._preview(body, actor, connection, state, None, None, resources)

    def catalog(self, profile):
        with self._read(profile) as (actor, _, state):
            schema = progress.LessonProgressInput.model_json_schema()
            fields = [{'key': key, 'label': exchange.FIELD_LABELS.get(key, key),
                       'type': exchange._field_type(spec), 'required': key in schema.get('required', [])}
                      for key, spec in schema['properties'].items()]
            dataset = {'kind': progress.KIND, 'label': exchange.LABELS[progress.KIND], 'fields': fields,
                       'revision': progress.ledger(state, actor['schoolId'])['revision'],
                       'limit': progress.LIMIT, 'formats': list(FORMATS), 'notice': progress.NOTICE}
            self._actor(profile)
            return {'revision': ops._ops(state)['revision'], 'datasets': [dataset], 'formats': list(FORMATS)}

    def references(self, profile, publication_version=None):
        if publication_version is not None and (type(publication_version) is not int
                or not 1 <= publication_version <= ops.MAX_REVISION):
            raise HTTPException(422, 'Укажите точную опубликованную версию')
        with self._read(profile) as (actor, connection, state):
            result = progress.PublishedLessons(connection, state, actor['schoolId']).references(publication_version)
            self._actor(profile)
            return result

    def preview(self, body, profile):
        body = exchange.PreviewInput.model_validate(body)
        _kind(body.kind)
        with self._read(profile) as (actor, connection, state), ExitStack() as resources:
            result = self._preview(body, actor, connection, state, resources)
            self._actor(profile)
            return result

    def apply(self, body, profile, *, before_commit=None):
        body = exchange.ApplyInput.model_validate(body)
        _kind(body.kind)
        self._actor(profile)
        # Keep any personal read snapshot until after the domain commit. The
        # domain row is locked first, before published binding reads that store.
        with ExitStack() as resources, self.storage.atomic_domain() as (connection, state):
            self._source(connection)
            actor = self._actor(profile)
            replay = exchange._replay(state, actor, body)
            if replay is not None:
                self._actor(profile)
                if before_commit is not None:
                    before_commit()
                return replay
            checked = self._preview(body, actor, connection, state, resources)
            if checked['previewHash'] != body.previewHash or not checked['ready']:
                raise HTTPException(409, 'Предпросмотр изменился или содержит ошибки; повторите проверку')
            journal = progress.ledger(state, actor['schoolId'])
            ops._cas(journal, body.expectedRevision)
            rows = [row for row in checked['rows'] if row['status'] == 'create']
            revision, ids = journal['revision'], []
            if rows:
                stamp, records = ops._stamp(), []
                for row in rows:
                    identity = str(uuid4())
                    ids.append(identity)
                    records.append({**row['record'], 'id': identity, 'createdAt': stamp})
                revision += 1
                state[progress.KEY] = {**journal, 'revision': revision, 'records': [*journal['records'], *records]}
            result = {'revision': revision, 'created': len(rows), 'skipped': checked['counts']['skip'],
                      'recordIds': ids, 'requestId': body.requestId}
            self._actor(profile)
            exchange._receipt(state, actor, body, result)
            if before_commit is not None:
                before_commit()
            return result

    def export(self, body, profile):
        body = exchange.ExportInput.model_validate(body)
        _kind(body.kind)
        with self._read(profile) as (actor, _, state):
            journal = progress.ledger(state, actor['schoolId'])
            if journal['revision'] != body.expectedRevision:
                raise HTTPException(409, 'Версия изменилась: обновите данные перед экспортом')
            fields = progress.LessonProgressInput.model_fields
            records = [{key: deepcopy(row.get(key)) for key in fields} for row in journal['records']
                       if body.includeArchived or not row.get('archived', False)]
            content, media = exchange._download(body.kind, body.format, records)
            self._actor(profile)
            return content, media, journal['revision']

    def template(self, kind, format, profile):
        _kind(kind)
        if format not in FORMATS:
            raise HTTPException(422, 'Неизвестный формат данных')
        with self._read(profile):
            result = exchange._download(kind, format, [])
            self._actor(profile)
            return result


def _query(request, allowed):
    if any(key not in allowed or len(request.query_params.getlist(key)) != 1
           for key in request.query_params):
        raise HTTPException(422, 'Некорректные параметры запроса')


def router_for(storage, actor, require_session):
    service = LessonProgressPostgres(storage)
    router = APIRouter(prefix=exchange.PREFIX, tags=['director lesson journal exchange'])

    @router.get('/catalog')
    def catalog(request: Request, profile=Depends(actor)):
        _query(request, set())
        result = service.catalog(profile)
        require_session(request, profile)
        return result

    @router.get('/lesson-progress-references')
    def references(request: Request, publicationVersion: int | None = Query(default=None, ge=1, le=ops.MAX_REVISION),
                   profile=Depends(actor)):
        _query(request, {'publicationVersion'})
        data = service.references(profile, publicationVersion)
        require_session(request, profile)
        return Response(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), media_type='application/json',
                        headers={'Content-Disposition': f'attachment; filename="school-lesson-progress-references-v{data["publicationVersion"]}.json"',
                                 'Cache-Control': 'no-store'})

    @router.post('/preview')
    def preview(request: Request, body: exchange.PreviewInput, profile=Depends(actor)):
        _query(request, set())
        result = service.preview(body, profile)
        require_session(request, profile)
        return result

    @router.post('/apply')
    def apply(request: Request, body: exchange.ApplyInput, profile=Depends(actor)):
        _query(request, set())
        result = service.apply(body, profile, before_commit=lambda: require_session(request, profile))
        require_session(request, profile)
        return result

    @router.post('/export')
    def export(request: Request, body: exchange.ExportInput, profile=Depends(actor)):
        _query(request, set())
        content, media, revision = service.export(body, profile)
        require_session(request, profile)
        return Response(content, media_type=media, headers={
            'Content-Disposition': f'attachment; filename="school-{body.kind}-r{revision}.{body.format}"',
            'X-Data-Revision': str(revision)})

    @router.get('/template/{kind}')
    def template(request: Request, kind: str, format: Literal['csv', 'xlsx', 'json'] = Query(default='csv'),
                 profile=Depends(actor)):
        _query(request, {'format'})
        content, media = service.template(kind, format, profile)
        require_session(request, profile)
        return Response(content, media_type=media, headers={
            'Content-Disposition': f'attachment; filename="school-{kind}-template.{format}"'})

    return router
