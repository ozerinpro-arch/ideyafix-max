"""Loopback school demo with a separate fictional workspace per browser."""
from contextlib import asynccontextmanager
import os
from pathlib import Path
import secrets
import stat
from tempfile import TemporaryDirectory
from threading import Lock
from time import monotonic
from typing import Literal
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .demo_seed import prepare
from .platform_assistant import PlatformAssistant
from .platform_navigation import home_reply
from .platform_runtime import PlatformRuntime
from .platform_transport import document_bytes
from .school_demo_links import DEMO_PORT


PROJECT = Path(__file__).resolve().parent.parent
STATIC = PROJECT / 'app/static/school-demo'
RUNTIME = PROJECT / 'runtime/school-demo'
COOKIE = 'ideyafix_school_demo'
SESSION_IDLE_SECONDS = 30 * 60
SESSION_LIMIT = 8
BODY_LIMIT = 32768
FILE_LIMIT = 2 * 1024 * 1024
ENCODED_FILE_LIMIT = (FILE_LIMIT + 2) // 3 * 4
ROLE = Literal['teacher', 'student', 'planner', 'director']
ACTORS = {'teacher': 710003, 'student': 710004, 'planner': 710006, 'director': 710005}
EXTERNAL_COMMANDS = {'/max', '/people', '/invite', '/role', '/page', '/hackathon'}
CSP = ("default-src 'self'; connect-src 'self'; img-src 'self' data: blob:; "
       "script-src 'self'; style-src 'self'; base-uri 'none'; object-src 'none'; "
       "frame-ancestors 'none'; form-action 'self'")

PERSONAL_TIMETABLES = {'teacher': '/api/staff/my-schedule', 'student': '/api/student/schedule'}
TIMETABLE_FIELDS = {'periodStart', 'publicationVersion', 'publicationState', 'revision', 'complete', 'bindingReady'}
LESSON_FIELDS = {'id', 'className', 'subject', 'day', 'slot', 'start', 'end', 'rooms', 'teachers', 'date'}
ASSIGNMENT_FIELDS = {'id', 'title', 'className', 'subject', 'status', 'dueAt'}


class DemoEvent(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    role: ROLE
    event_id: str = Field(min_length=36, max_length=36)

    @field_validator('event_id')
    @classmethod
    def canonical_uuid(cls, value):
        if str(UUID(value)) != value:
            raise ValueError('Canonical UUID required')
        return value


class DemoAction(DemoEvent):
    text: str = Field(min_length=1, max_length=4000)

    @field_validator('text')
    @classmethod
    def valid_text(cls, text):
        if not text.strip() or any(
            (ord(char) < 32 and char not in '\r\n\t')
            or 0x7F <= ord(char) <= 0x9F or 0xD800 <= ord(char) <= 0xDFFF
            for char in text
        ):
            raise ValueError('Invalid demo text')
        return text


class DemoDocument(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    name: str = Field(min_length=1, max_length=160)
    contentType: str = Field(min_length=1, max_length=128)
    base64: str = Field(min_length=1, max_length=ENCODED_FILE_LIMIT)


class DemoUpload(DemoEvent):
    document: DemoDocument


async def read_event(request, model, limit):
    body = bytearray()
    async for part in request.stream():
        if len(body) + len(part) > limit:
            raise HTTPException(413, 'Превышен допустимый размер запроса.')
        body.extend(part)
    try:
        return model.model_validate_json(body)
    except ValidationError:
        raise HTTPException(422, 'Проверьте данные школьного кабинета.') from None


def _verb(text):
    words = text.split(maxsplit=1)
    return words[0].casefold() if words else ''


def _public_reply(reply):
    if reply is None:
        return {'text': 'Выберите действие в школьном кабинете.',
                'buttons': [{'label': 'Главная', 'command': '/home'}]}
    text = reply['text']
    if reply.get('nativeInviteToken'):
        text = 'Тестовое приглашение создано внутри локального стенда.'
    public = {'text': text, 'buttons': [
        {'label': button['label'], 'command': button['command']}
        for button in reply.get('buttons', ())
        if _verb(button['command']) not in EXTERNAL_COMMANDS
    ]}
    for key in ('document', 'image'):
        if key in reply:
            public[key] = reply[key]
    return public


def _timetable(data):
    if data is None:
        return None
    return {
        **{key: value for key, value in data.items() if key in TIMETABLE_FIELDS},
        'lessons': [
            {key: value for key, value in lesson.items() if key in LESSON_FIELDS}
            for lesson in data.get('lessons', [])
        ],
    }


class _Workspace:
    def __init__(self, root):
        self.directory = TemporaryDirectory(prefix='browser-', dir=root)
        try:
            self.runtime = PlatformRuntime(Path(self.directory.name))
            prepare(self.runtime)
            self.assistant = PlatformAssistant(self.runtime)
        except Exception:
            self.close()
            raise
        self.last_seen = monotonic()

    def close(self):
        try:
            if hasattr(self, 'runtime'):
                self.runtime.close()
        finally:
            self.directory.cleanup()

    def read(self, actor, path):
        result = self.runtime.request(actor, 'GET', path)
        return result['data'] if result['status'] == 200 else None

    def state(self, role):
        actor = ACTORS[role]
        profiles = [
            {key: profile[key] for key in ('id', 'name', 'role')}
            for profile in self.assistant.profiles() if profile['id'] in ACTORS.values()
        ]
        current = next((profile for profile in profiles if profile['id'] == actor), None)
        if current is None or self.assistant.authorize(actor).get('status') != 200:
            raise HTTPException(403, 'Доступ этой тестовой роли закрыт.')
        sections = {}
        assignments = []
        if role in PERSONAL_TIMETABLES:
            timetable = self.read(actor, PERSONAL_TIMETABLES[role])
            learning = self.read(actor, f'/api/{role}/learning') or {}
            assignments = [
                {key: value for key, value in assignment.items() if key in ASSIGNMENT_FIELDS}
                for assignment in learning.get('assignments', [])
            ]
        else:
            active = (self.read(actor, '/api/school/publications') or {}).get('active')
            timetable = {**active, 'publicationVersion': active['version']} if active else None
            sections['draftTimetable'] = _timetable(self.read(actor, '/api/timetable'))
        return {
            'role': role,
            'profile': current,
            'profiles': profiles,
            'timetable': _timetable(timetable),
            'assignments': assignments,
            'sections': sections,
            'menu': _public_reply(home_reply(current)),
        }

    def action(self, value):
        reply = self.assistant.handle(ACTORS[value.role], value.text, value.event_id, session_key='school-demo-action')
        return {'reply': _public_reply(reply)}

    def upload(self, value):
        document = value.document.model_dump()
        try:
            name, _, data = document_bytes(document)
            if len(data) > FILE_LIMIT or not name.lower().endswith(('.xlsx', '.csv', '.json', '.txt')):
                raise ValueError('Unsupported local demo document')
        except ValueError:
            raise HTTPException(422, 'Выберите тестовый XLSX, CSV, JSON или TXT до 2 МиБ.') from None
        reply = self.assistant.handle_file(ACTORS[value.role], document, value.event_id, session_key='school-demo-action')
        if reply is None:
            raise HTTPException(409, 'Сначала откройте действие, которое ожидает файл.')
        return {'reply': _public_reply(reply)}


def create_app(runtime_root=None):
    root = Path(runtime_root or RUNTIME).absolute()
    workspaces = {}
    lock = Lock()

    def find_workspace(cookie, *, create):
        now = monotonic()
        for key, workspace in list(workspaces.items()):
            if now - workspace.last_seen > SESSION_IDLE_SECONDS:
                del workspaces[key]
                workspace.close()
        workspace = workspaces.get(cookie)
        if workspace is None:
            if not create:
                raise HTTPException(403, 'Откройте школьный кабинет, чтобы начать локальную сессию.')
            if len(workspaces) >= SESSION_LIMIT:
                raise HTTPException(503, 'Все места на локальном стенде заняты. Попробуйте позже.')
            if any(path.is_symlink() for path in (root, *root.parents)):
                raise ValueError('Invalid demo runtime directory')
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            info = root.stat()
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError('Demo runtime directory must be private')
            cookie = secrets.token_urlsafe(32)
            workspace = workspaces[cookie] = _Workspace(root)
        workspace.last_seen = now
        return cookie, workspace

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            with lock:
                for workspace in workspaces.values():
                    workspace.close()
                workspaces.clear()

    app = FastAPI(title='Идеяфикс · школьный стенд', docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)

    @app.middleware('http')
    async def loopback_boundary(request, call_next):
        hosts = request.headers.getlist('host')
        origins = request.headers.getlist('origin')
        allowed_hosts = {f'127.0.0.1:{DEMO_PORT}', f'localhost:{DEMO_PORT}'}
        valid_host = len(hosts) == 1 and hosts[0] in allowed_hosts
        same_origin = valid_host and origins == ['http://' + hosts[0]]
        cross_site_api = (request.url.path.startswith('/api/') and
                          request.headers.get('sec-fetch-site') not in {None, 'none', 'same-origin'})
        if not valid_host or (origins and not same_origin) or cross_site_api:
            response = JSONResponse({'detail': 'Откройте стенд по его локальному адресу.'}, status_code=403)
        elif request.method not in {'GET', 'HEAD'} and (
            not same_origin or request.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/json'
        ):
            response = JSONResponse({'detail': 'Действие должно исходить из локального школьного кабинета.'}, status_code=403)
        else:
            response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Content-Security-Policy'] = CSP
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        return JSONResponse({'detail': 'Выберите одну из тестовых ролей.'}, status_code=422)

    @app.get('/')
    @app.get('/demo/')
    def index():
        return asset('index.html')

    @app.get('/static/school-demo/{filename}')
    def asset(filename: str):
        path = STATIC / filename
        if filename not in {'index.html', 'app.js', 'styles.css'} or path.is_symlink() or not path.is_file():
            raise HTTPException(404, 'Файл школьного кабинета не найден.')
        return FileResponse(path)

    @app.get('/api/demo/state')
    def state(role: ROLE, request: Request):
        incoming_cookie = request.cookies.get(COOKIE)
        try:
            with lock:
                cookie, workspace = find_workspace(incoming_cookie, create=True)
                response = JSONResponse(workspace.state(role))
        except (OSError, RuntimeError, ValueError):
            raise HTTPException(503, 'Школьный кабинет пока недоступен. Обновите страницу.') from None
        if cookie != incoming_cookie:
            response.set_cookie(COOKIE, cookie, httponly=True, samesite='strict')
        return response

    def perform(value, cookie):
        with lock:
            _, workspace = find_workspace(cookie, create=False)
            if isinstance(value, DemoUpload):
                return workspace.upload(value)
            return workspace.action(value)

    async def perform_event(request, value):
        try:
            return await run_in_threadpool(perform, value, request.cookies.get(COOKIE))
        except (OSError, RuntimeError, ValueError):
            raise HTTPException(503, 'Действие пока недоступно. Повторите запрос.') from None

    @app.post('/api/demo/action')
    async def action(request: Request):
        value = await read_event(request, DemoAction, BODY_LIMIT)
        if _verb(value.text) in EXTERNAL_COMMANDS:
            raise HTTPException(403, 'Эта команда недоступна в локальном школьном кабинете.')
        return await perform_event(request, value)

    @app.post('/api/demo/upload')
    async def upload(request: Request):
        return await perform_event(request, await read_event(request, DemoUpload, ENCODED_FILE_LIMIT + 4096))

    return app


def main():
    import uvicorn

    uvicorn.run(create_app(), host='127.0.0.1', port=DEMO_PORT, proxy_headers=False, access_log=False)


if __name__ == '__main__':
    main()
