"""Loopback-only HTTP facade of the explicitly synthetic Идеяфикс MAX bot."""
from contextlib import asynccontextmanager
import json
from pathlib import Path
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .domain import ACTORS, BY_ID
from .store import Store
from .transport import SyntheticMax, get_transport, normalize_demo_event
from .max_settings import MaxSettings

ROOT = Path(__file__).resolve().parent
ORIGINS = {'http://127.0.0.1:5211', 'http://localhost:5211'}
HOSTS = {'127.0.0.1:5211', 'localhost:5211'}

class DemoEvent(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    actor_id: int
    text: str = Field(min_length=1, max_length=2000)
    event_id: str = Field(min_length=36, max_length=36)

    @field_validator('actor_id')
    @classmethod
    def known_actor(cls, value):
        if value not in BY_ID:
            raise ValueError('Unknown test actor')
        return value

    @field_validator('text')
    @classmethod
    def valid_text(cls, value):
        if not value.strip() or any(ord(c) < 32 and c not in '\n\t' or 0x7F <= ord(c) <= 0x9F or 0xD800 <= ord(c) <= 0xDFFF for c in value):
            raise ValueError('Invalid demo text')
        return value

    @field_validator('event_id')
    @classmethod
    def event_uuid(cls, value):
        if str(uuid.UUID(value)) != value:
            raise ValueError('Canonical UUID required')
        return value

def create_app(db_path=None, transport=None, *, max_settings=None):
    if transport is not None and type(transport) is not SyntheticMax:
        raise ValueError('Only SyntheticMax is allowed in the demo')
    provider = get_transport() if transport is None else transport
    if max_settings is not None and type(max_settings) is not MaxSettings:
        raise ValueError('Explicit MaxSettings required')
    actual_db = db_path or (max_settings.db_path if max_settings is not None else ROOT.parent / 'runtime/ideyafix-demo.sqlite3')
    if max_settings is not None and Path(actual_db).absolute() != max_settings.db_path.absolute():
        raise ValueError('MAX and demo must use the same explicit test database')
    store = Store(actual_db)
    live_store = None
    if max_settings is not None and max_settings.enabled:
        if max_settings.platform_enabled:
            from .platform_store import PlatformStore
            live_store=PlatformStore(actual_db,bot_username=max_settings.bot_username)
        else:
            from .live_store import LiveStore
            live_store = LiveStore(actual_db)

    @asynccontextmanager
    async def lifespan(app):
        yield
        provider.close()

    app = FastAPI(title='Идеяфикс · тестовый MAX', docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.transport = provider
    app.state.store = store

    @app.middleware('http')
    async def local_boundary(request: Request, call_next):
        from fastapi.responses import JSONResponse
        if request.headers.get('host') not in HOSTS:
            return JSONResponse({'detail': 'Стенд доступен только по локальному адресу'}, status_code=403)
        if request.method not in {'GET', 'HEAD'}:
            if request.headers.get('origin') not in ORIGINS or request.headers.get('content-type', '').split(';')[0] != 'application/json':
                return JSONResponse({'detail': 'Нужны локальный Origin и application/json'}, status_code=403)
            # Bound streaming bodies before pydantic/JSON parsing as well.
            body = b''
            async for part in request.stream():
                body += part
                if len(body) > 16384:
                    return JSONResponse({'detail': 'Событие слишком большое'}, status_code=413)
            request._body = body
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        return response

    @app.get('/api/demo/health')
    def health():
        return dict(status='ok', mode='synthetic', realProviderCalls=0)

    @app.get('/api/demo/bootstrap')
    def bootstrap():
        if max_settings is not None and max_settings.platform_enabled:
            from .platform_profiles import PROFILES
            return dict(actors=live_store.profiles()if live_store is not None else PROFILES,selectedActorId=710001,mode='synthetic',realProviderCalls=0)
        return dict(actors=ACTORS, selectedActorId=710001, mode='synthetic', realProviderCalls=0)

    @app.get('/api/max/status')
    def max_status():
        return dict(configured=live_store is not None, enabled=live_store is not None,
            mode='long_polling' if live_store is not None else 'disabled',
            botUsername=max_settings.bot_username if max_settings is not None else 'ideya_fiks_150_bot',
            workerAlive=live_store.worker_alive() if live_store is not None else False,
            platformEnabled=bool(max_settings is not None and max_settings.platform_enabled),
            sendingEnabled=live_store is not None,
            counts=live_store.safe_status() if live_store is not None else dict(queued=0, accepted=0, rejected=0, unknown=0))

    class InviteRequest(BaseModel):
        model_config = ConfigDict(extra='forbid', strict=True)
        actor_id: int

    @app.post('/api/max/invite')
    def max_invite(value: InviteRequest):
        from .platform_profiles import PROFILES
        allowed={p['id']for p in (live_store.profiles()if live_store is not None else PROFILES)}if max_settings is not None and max_settings.platform_enabled else BY_ID
        if value.actor_id not in allowed:
            raise HTTPException(422, 'Неизвестная тестовая роль')
        if live_store is None or not live_store.worker_alive():
            raise HTTPException(409, 'Приёмник MAX пока не запущен')
        invitation = live_store.issue_invite(value.actor_id)
        return dict(url=f'https://max.ru/{max_settings.bot_username}?start={invitation["payload"]}',
                    expiresAt=invitation['expiresAt'])

    @app.get('/api/demo/state')
    def state(actor_id: int):
        if max_settings is not None and max_settings.platform_enabled:
            from .platform_profiles import PROFILES
            profile=next((p for p in (live_store.profiles()if live_store is not None else PROFILES)if p['id']==actor_id),None)
            if profile is None:raise HTTPException(422,'Неизвестный профиль')
            return dict(actor=profile,messages=[],teams=[],assignments=[],summary={},quickActions=[],
                mode='synthetic',realProviderCalls=0)
        if actor_id not in BY_ID:
            raise HTTPException(422, 'Неизвестный тестовый пользователь')
        return store.view(actor_id)

    @app.post('/api/demo/events')
    def event(value: DemoEvent):
        if max_settings is not None and max_settings.platform_enabled:
            raise HTTPException(403,'Личные действия выполняются в MAX; здесь только консоль подключения')
        normalized = normalize_demo_event(value.actor_id, value.text, value.event_id)
        if normalized.get('senderId') != str(value.actor_id) or normalized.get('text') != value.text:
            raise HTTPException(422, 'Событие MAX не соответствует тестовому пользователю')
        return store.event(value.actor_id, value.text, value.event_id, provider)

    @app.get('/')
    def index():
        return FileResponse(ROOT / 'static/index.html')

    app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')
    return app
