"""Separate, explicitly enabled MAX ingress; existing cabinet auth is unchanged.

Run behind an exact HTTPS reverse-proxy location: uvicorn server.max_app:create_app
--factory --host 127.0.0.1 --port 18829. No subscription or send runs on import.
"""
from contextlib import asynccontextmanager
import os
import threading
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from .integration_settings import IntegrationSettings, _flag
from .school_integrations import IntegrationResources


def pump_once(resources):
    if resources.max_runtime is None:
        return {'processed': False, 'state': 'disabled'}
    with resources.bind():
        runtime = resources.max_runtime
        incoming = runtime.process_one()
        questions = runtime.queue_questions() if resources.replacement_storage is None else {'queued': 0}
        outgoing = runtime.send_one() if resources.settings.max_send_requested else {'processed': False, 'state': 'sending_disabled'}
    return {'processed': bool(incoming.get('processed') or questions.get('queued') or outgoing.get('processed')),
        'incoming': incoming.get('state', 'idle'), 'queuedQuestions': questions.get('queued', 0),
        'outgoing': outgoing.get('state', 'idle')}


def _worker(resources, stop):
    try:
        with resources.bind():
            resources.max_runtime.recover()
    except Exception:
        # A private store/authority failure is not permission to consume or send.
        return
    while not stop.is_set():
        try:
            worked = pump_once(resources)['processed']
        except Exception:
            worked = False
        stop.wait(0.1 if worked else 1.0)


def create_app(*, settings=None, resources=None, start_worker=None):
    os.environ['PYTHON_DOTENV_DISABLED'] = '1'
    if resources is None:
        from .school_postgres import SchoolPostgres,SchoolPostgresSettings
        selected = SchoolPostgresSettings.from_environment(os.environ.get('SHKOLA_SCHOOL_ID', ''))
        if selected is not None:
            from .school_max import SchoolMaxResources,settings_for_school
            settings = settings or settings_for_school(selected.school)
            resources = SchoolMaxResources(settings, SchoolPostgres(selected))
        else:
            settings = settings or IntegrationSettings.from_environment(os.environ.get('SHKOLA_SCHOOL_ID', ''))
            resources = IntegrationResources(settings)
    else:
        settings = resources.settings
    if start_worker is None:
        start_worker = (resources.replacement_storage is None or getattr(resources,'working_max',False)) and _flag(os.environ, 'SHKOLA_MAX_WORKER_ENABLED')
    stop = threading.Event()
    thread = None

    @asynccontextmanager
    async def lifespan(app):
        nonlocal thread
        if start_worker and resources.max_runtime is not None:
            thread = threading.Thread(target=_worker, args=(resources, stop), name='school-max-worker', daemon=True)
            thread.start()
        yield
        stop.set()
        if thread is not None:
            await run_in_threadpool(thread.join, 40)
        if thread is None or not thread.is_alive():
            resources.close()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.integration_resources = resources

    @app.exception_handler(HTTPException)
    async def expected_error(request, error):
        return JSONResponse({'detail': error.detail}, status_code=error.status_code,
            headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})

    @app.exception_handler(Exception)
    async def private_error(request, error):
        return JSONResponse({'detail': 'Подключение временно недоступно'}, status_code=503,
            headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})

    @app.get('/healthz')
    def health():
        return {'status': 'ready' if resources.max_runtime is not None else 'disabled',
            'workerRunning': bool(thread is not None and thread.is_alive())}

    @app.post('/school-max/webhook')
    async def webhook(request: Request):
        if resources.max_runtime is None or settings.max_update_mode != 'webhook':
            raise HTTPException(404, 'Не найдено')
        host = urlsplit(settings.max_webhook_url).netloc
        headers = list(request.scope['headers'])
        hosts = [value.decode('latin1') for key, value in headers if key.lower() == b'host']
        if hosts != [host] or request.scope.get('raw_path', b'') != b'/school-max/webhook' or request.url.query:
            raise HTTPException(400, 'Неверный адрес подключения')
        content_types = [v.decode('latin1').split(';', 1)[0].strip().lower() for k, v in headers if k.lower() == b'content-type']
        if content_types != ['application/json'] or any(k.lower() == b'content-encoding' for k, _ in headers):
            raise HTTPException(415, 'Нужен JSON без сжатия')
        lengths = [v for k, v in headers if k.lower() == b'content-length']
        if len(lengths) > 1:
            raise HTTPException(400, 'Неоднозначный размер события')
        if lengths:
            try:
                if int(lengths[0]) < 0 or int(lengths[0]) > 65536:
                    raise HTTPException(413, 'Слишком большое событие')
            except ValueError:
                raise HTTPException(400, 'Некорректный размер события') from None
        body = b''
        async for chunk in request.stream():
            body += chunk
            if len(body) > 65536:
                raise HTTPException(413, 'Слишком большое событие')
        normalized_headers = [(key.decode('latin1'), value.decode('latin1')) for key, value in headers]

        def ingest():
            with resources.bind():
                return resources.max_runtime.ingest(body, normalized_headers)

        result = await run_in_threadpool(ingest)
        return JSONResponse(result, headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})

    return app
