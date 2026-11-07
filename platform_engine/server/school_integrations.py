"""Host-scoped live connections. No environment activation from HTTP bodies."""
from contextlib import contextmanager, closing
from contextvars import ContextVar
from pathlib import Path
import os
import sqlite3
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .auth import session_profile
from .integration_settings import IntegrationSettings
from . import launch_runtime, request_extraction, school_requests

_current = ContextVar('school_live_integrations', default=None)


def current_resources():
    return _current.get()


def _staff_authority(settings):
    if not settings.staff_db:
        return False
    from . import staff_access, launch_identity
    try:
        with staff_access.bound_configuration((Path(settings.staff_db), settings.school_id)):
            path, _ = staff_access.configuration()
            if not path.is_file():
                return False
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as connection:
                staff_access._validate_existing(connection, settings.school_id)
        staff_access.register_configuration((path, settings.school_id))
        launch_identity.install()
        return True
    except (ValueError, RuntimeError, OSError, sqlite3.Error):
        return False


class IntegrationResources:
    def __init__(self, settings: IntegrationSettings, *, extractor=None, transport=None, check_authority=True,
                 replacement_storage=None, replacement_domain=None):
        self.settings = settings
        self.domain_binding = (os.environ.get('SHKOLA_DB_PATH', ''), os.environ.get('AGENT_DB_PATH', ''))
        self.extraction = self.max_runtime = self.identity = self.transport = None
        self.authority_ready = False
        self.initialization_blockers = []
        self.replacement_storage = None
        self._replacement_staff_identity = None
        self.manager_ai_enabled = False
        replacement_staff = None
        if replacement_storage is not None or replacement_domain is not None:
            import httpx
            from .max_transport import MaxClient
            from .replacement_postgres import ReplacementPostgres
            if (type(replacement_storage) is not ReplacementPostgres or type(transport) is not httpx.MockTransport
                    or extractor is not None or check_authority is not True or settings.request_ai_requested
                    or settings.ai_key or settings.ai_db or settings.max_ca_file
                    or (settings.domain_db, settings.agent_db) != self.domain_binding
                    or settings.max_token != 'SYNTHETIC-TOKEN'
                    or not (urlsplit(settings.max_webhook_url).hostname or '').endswith('.example')
                    or not settings.max_config_ready or not settings.max_send_requested
                    or not settings.max_schedule_notices_requested):
                raise ValueError('Expected explicit isolated PostgreSQL MAX rehearsal resources')
            self.replacement_storage = replacement_storage.bind_queue_identity(replacement_domain)
            self._replacement_staff_identity = self._check_replacement_staff()
            staff_path, school, _, device, inode = self._replacement_staff_identity
            replacement_staff = launch_runtime.digest([school, str(staff_path), device, inode])
            self.replacement_storage.queue_identity()
            self.authority_ready = _staff_authority(settings)
            if not self.authority_ready:
                raise ValueError('Expected a valid existing synthetic staff authority')
            transport = MaxClient(settings.max_token, transport=transport)
        else:
            from .schedule_agent import ModelConfiguration
            manager_model = ModelConfiguration.from_environment()
            self.manager_ai_enabled = bool(manager_model.configured and manager_model.requests_enabled)
        if settings.ai_ready:
            if extractor is None:
                from .schedule_agent import ModelConfiguration
                from .request_intents import make_intent_extractor
                extractor = make_intent_extractor(ModelConfiguration(settings.ai_key, settings.ai_model, True))
            self.extraction = request_extraction.ExtractionService(settings.school_id, settings.ai_db, extractor,
                forbidden=tuple(p for p in (settings.max_db, settings.staff_db) if p))
        if settings.max_config_ready:
            if self.replacement_storage is None:
                self.authority_ready = _staff_authority(settings) if check_authority else True
            if self.authority_ready:
                from .max_runtime import MaxRuntime
                if not settings.max_send_requested:
                    transport = None
                if transport is None and settings.max_send_requested:
                    from .max_transport import MaxClient
                    try:
                        transport = MaxClient(settings.max_token, additional_ca_file=settings.max_ca_file or None)
                    except (ValueError, OSError):
                        self.initialization_blockers.append('Не удалось подготовить защищённое соединение с MAX')
                self.transport = transport
                self.max_runtime = MaxRuntime(integration_id=settings.max_integration_id, school_id=settings.school_id,
                    db_path=settings.max_db, webhook_secret=settings.max_secret, transport=transport,
                    enabled=True, extraction_service=self.extraction,
                    schedule_notices_enabled=settings.max_schedule_notices_requested,
                    replacement_storage=self.replacement_storage,replacement_domain=replacement_domain,
                    replacement_staff=replacement_staff)
                self.identity = self.max_runtime.identity
                if self.replacement_storage is not None:
                    try:
                        with school_requests.bound_school(settings.school_id):
                            self.max_runtime.store.read()
                    except (OSError, RuntimeError, ValueError, sqlite3.Error):
                        self.close()
                        raise HTTPException(503, 'Хранилище MAX относится к другой тестовой базе сотрудников') from None

    def _check_replacement_staff(self):
        from . import staff_access
        from .replacement_postgres import _private_directory
        staff_db, school = staff_access.configuration()
        origin = os.environ.get('SHKOLA_STAFF_ORIGIN', '')
        parsed = urlsplit(origin)
        max_dir = Path(self.settings.max_db).parent
        if ((Path(self.settings.staff_db), self.settings.school_id) != (staff_db, school)
                or max_dir != staff_db.parent / 'max-schedule-rehearsal'
                or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
            raise ValueError('Expected the private loopback staff profile for MAX rehearsal')
        self.replacement_storage.validate_staff(school, parsed.scheme, parsed.hostname, staff_db)
        _private_directory(max_dir)
        info = staff_db.stat()
        return staff_db, school, origin, info.st_dev, info.st_ino

    @contextmanager
    def bind(self):
        if self.domain_binding != (os.environ.get('SHKOLA_DB_PATH', ''), os.environ.get('AGENT_DB_PATH', '')):
            raise HTTPException(503, 'Настройка хранилища изменилась; нужен новый запуск')
        if self.replacement_storage is not None:
            try:
                if self._check_replacement_staff() != self._replacement_staff_identity:
                    raise ValueError('Staff profile changed')
            except (OSError, RuntimeError, ValueError):
                raise HTTPException(503, 'Тестовый профиль MAX изменился; нужен новый запуск') from None
        token = _current.set(self)
        try:
            with school_requests.bound_school(self.settings.school_id), request_extraction.bound_service(self.extraction):
                yield self
        finally:
            _current.reset(token)

    def status(self):
        result = self.settings.safe_status()
        result['ai']['enabled'] = self.extraction is not None
        result['ai']['managerEnabled'] = self.manager_ai_enabled
        result['max']['enabled'] = self.max_runtime is not None
        result['max']['sendEnabled'] = bool(self.max_runtime is not None and self.settings.max_send_requested and self.transport is not None)
        result['max']['scheduleNoticeQueueEnabled'] = bool(self.max_runtime and self.max_runtime.schedule_notices_enabled)
        result['max']['personalAuthorityConfigured'] = self.authority_ready
        result['max']['blockers'].extend(self.initialization_blockers)
        if not self.authority_ready:
            result['max']['blockers'].append('Нужны действующие личные школьные учётные записи')
        if self.max_runtime is not None:
            result['max']['queue'] = self.max_runtime.status()
        return result

    def close(self):
        if self.transport is not None and hasattr(self.transport, 'close'):
            self.transport.close()


class IntegrationMiddleware:
    def __init__(self, app, resources):
        self.app, self.resources = app, resources

    async def __call__(self, scope, receive, send):
        from starlette.responses import JSONResponse
        try:
            with self.resources.bind():
                await self.app(scope, receive, send)
        except HTTPException as error:
            if scope['type'] != 'http':
                raise
            await JSONResponse({'detail': error.detail}, status_code=error.status_code)(scope, receive, send)


def _actor(profile, *, teacher=False):
    profile = launch_runtime.actor(profile, 'launch.status')
    school_requests.require_bound_school(profile['schoolId'])
    resources = current_resources()
    if resources is None or resources.settings.school_id != profile['schoolId']:
        raise HTTPException(503, 'Подключения для этой школы не настроены')
    if teacher and profile.get('staffRole') != 'teacher':
        raise HTTPException(403, 'Личную связь создаёт сотрудник в своём школьном кабинете')
    return profile, resources


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class MaxScheduleNoticeInput(EmptyInput):
    membershipId: str = Field(pattern=r'^[A-Za-z0-9_-]{1,80}$')
    expectedPublicationVersion: int = Field(ge=1, le=2**31-1)


def _require_schedule_session(request, profile):
    check = request.scope.get('_staff_require_current_session')
    if check is None:
        raise HTTPException(401, 'Войдите в школьный кабинет')
    check(request, profile)


router = APIRouter(prefix='/api/school/integrations', tags=['school connections'])


@router.get('/status')
def status(profile=Depends(session_profile)):
    _, resources = _actor(profile)
    return resources.status()


@router.get('/max/me')
def my_binding(request: Request, profile=Depends(session_profile)):
    profile, resources = _actor(profile, teacher=True)
    _require_schedule_session(request, profile)
    if resources.identity is None:
        return {'linked': False, 'canLink': False, 'message': 'Личное подключение MAX пока не настроено'}
    result = {**resources.identity.status(profile), 'canLink': resources.replacement_storage is None or getattr(resources, 'working_max', False)}
    _require_schedule_session(request, profile)
    return result


@router.post('/max/link')
def link(body: EmptyInput, request: Request, profile=Depends(session_profile)):
    profile, resources = _actor(profile, teacher=True)
    _require_schedule_session(request, profile)
    if resources.identity is None:
        raise HTTPException(503, 'Личное подключение MAX пока не настроено')
    challenge = resources.identity.issue_challenge(profile,
        before_return=lambda: _require_schedule_session(request, profile))
    host = ('max.ru' if resources.replacement_storage is None or getattr(resources, 'working_max', False)
            else urlsplit(resources.settings.max_webhook_url).netloc)
    _require_schedule_session(request, profile)
    return {'url': 'https://' + host + '/' + resources.settings.max_username + '?start=' + quote(challenge['token'], safe=''),
        'expiresAt': challenge['expiresAt'], 'message': 'Откройте ссылку и начните диалог с ботом для подтверждения личной связи'}


@router.post('/max/revoke')
def revoke(body: EmptyInput, request: Request, profile=Depends(session_profile)):
    profile, resources = _actor(profile, teacher=True)
    _require_schedule_session(request, profile)
    if resources.identity is None:
        raise HTTPException(503, 'Личное подключение MAX пока не настроено')
    result = resources.identity.revoke(profile,
        before_return=lambda: _require_schedule_session(request, profile))
    _require_schedule_session(request, profile)
    return result


@router.get('/max/schedule-notices')
def schedule_notice_targets(request: Request, profile=Depends(session_profile)):
    profile, resources = _actor(profile)
    if profile.get('staffRole') != 'director':
        raise HTTPException(403, 'Передачу уведомления в MAX подтверждает директор школы')
    _require_schedule_session(request, profile)
    result = (resources.max_runtime.schedule_notice_targets(profile) if resources.max_runtime is not None
              else {'enabled': False, 'publicationVersion': None, 'recipients': []})
    _require_schedule_session(request, profile)
    return result


@router.post('/max/schedule-notices/queue')
def queue_schedule_notice(body: MaxScheduleNoticeInput, request: Request, profile=Depends(session_profile)):
    profile, resources = _actor(profile)
    if profile.get('staffRole') != 'director':
        raise HTTPException(403, 'Передачу уведомления в MAX подтверждает директор школы')
    _require_schedule_session(request, profile)
    if resources.max_runtime is None:
        raise HTTPException(503, 'Передача уведомлений расписания в MAX выключена')
    return resources.max_runtime.queue_schedule_notice(profile, body.membershipId, body.expectedPublicationVersion,
        check_session=lambda: _require_schedule_session(request, profile))
