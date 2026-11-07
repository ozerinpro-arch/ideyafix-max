"""Private portal upstream: uvicorn server.portal_app:create_app --factory.

Bind uvicorn to loopback only. The gateway authenticates its owner session on
every request, validates external Origin/CSRF, and alone supplies the bridge.
The existing local demo entrypoint keeps its own authentication unchanged.
"""
import mimetypes
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, HTTPException, Request, Depends
from fastapi.exceptions import RequestValidationError
from starlette.responses import FileResponse

from .portal_security import PortalBoundaryMiddleware, load_configuration, private_route


_ROOT_FILES = {"favicon.svg", "teacher-portraits.png", "hero-school.png", "mascot.png", "assistant.svg", "school-orbit.png"}
_ASSET_SUFFIXES = {".js", ".css", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".avif", ".gif", ".ico", ".woff", ".woff2", ".ttf", ".otf"}
_SOURCE_DIRECTORIES = {"src", "server", "tests", "node_modules", "data", "work", "public"}
_OWNER_PROFILE = {"role": "director", "userId": "portal-owner", "name": "Владелец", "title": "Директор", "initials": "ВЛ"}


def _staff_entry_url(value):
    """A configured click-only destination; never an identity/credential bridge."""
    if not value:
        return None
    from .max_transport import validate_webhook_url
    try:
        canonical = validate_webhook_url(value)
        path = urlsplit(canonical).path
        if (canonical != value or len(path) > 121
                or not re.fullmatch(r'/[A-Za-z0-9][A-Za-z0-9_-]*(?:/[A-Za-z0-9][A-Za-z0-9_-]*)*/', path)):
            return None
        return canonical
    except (ValueError, TypeError):
        return None


def _build_file(root: Path, relative: str) -> Path:
    target = root
    try:
        for part in relative.split("/"):
            target = target / part
            if target.is_symlink():
                raise HTTPException(404, "Не найдено")
        if not target.resolve().is_relative_to(root) or not target.is_file():
            raise HTTPException(404, "Не найдено")
    except (OSError, ValueError):
        raise HTTPException(404, "Не найдено") from None
    return target


class SchoolRequestDomainMiddleware:
    """Bind the domain DB's school from the host, never from request JSON."""
    def __init__(self, app, school_id):
        self.app, self.school_id = app, school_id

    async def __call__(self, scope, receive, send):
        from .school_requests import bound_school
        with bound_school(self.school_id):
            await self.app(scope, receive, send)


def create_app():
    secret, ui_root = load_configuration()
    # The portal uses an explicit service environment. Do not load local keys
    # or demo settings as a side effect of reusing the domain routes.
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    from . import auth, core

    app = FastAPI(title="Идеяфикс", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(core.LocalBoundaryMiddleware)
    app.add_exception_handler(RequestValidationError, core.safe_validation_error)
    marker = object()
    owner_profile = _OWNER_PROFILE.copy()
    preview = os.environ.get('SHKOLA_PORTAL_PREVIEW') == 'true'
    school_id = os.environ.get('SHKOLA_SCHOOL_ID', 'preview-school' if preview else 'owner-school')
    app.add_middleware(SchoolRequestDomainMiddleware, school_id=school_id)
    from .integration_settings import IntegrationSettings
    from .school_integrations import IntegrationMiddleware, IntegrationResources, _staff_authority
    resources = IntegrationResources(IntegrationSettings.from_environment(school_id))
    # A configured shared school registry is useful even while MAX is off:
    # owner-side workflows must resolve the current personal planner identity.
    # Validation is read-only and never creates accounts or changes a role.
    if resources.settings.staff_db and not resources.authority_ready:
        resources.authority_ready = _staff_authority(resources.settings)
    staff_entry = (_staff_entry_url(os.environ.get('SHKOLA_STAFF_ENTRY_URL', ''))
                   if not preview and resources.authority_ready else None)
    app.add_middleware(IntegrationMiddleware, resources=resources)
    if preview:
        owner_profile.update(userId='portal-preview', name='Тестовый директор',
                             title='Директор · тестовая копия', initials='ТД')

    def portal_owner(request: Request) -> dict:
        if request.scope.get("portal_bridge_marker") is not marker:
            raise HTTPException(403, "Доступ запрещён")
        role = owner_profile.copy()
        role.update(schoolId=school_id, identitySource='portal-preview' if preview else 'portal-owner',
                    membershipId=role['userId'], membershipRevision=1, memberRevision=1,
                    membershipVerified=True, permissions=['schedule.read', 'schedule.manage', 'launch.read', 'launch.manage', 'launch.status', 'publication.read', 'publication.publish'],
                    teacherRefs=[])
        from .launch_identity import register_portal_principal
        register_portal_principal(role)
        return role

    app.dependency_overrides[auth.session_profile] = portal_owner
    from .launch_identity import install
    install()

    @app.get('/api/staff/me')
    def staff_status(profile=Depends(portal_owner)):
        if staff_entry:
            return {'configured': False, 'mode': 'owner', 'safeEntryUrl': staff_entry,
                    'message': 'Для сотрудников действует отдельный личный кабинет. Войдите в него со своим логином и паролем.'}
        return {'configured': False, 'mode': 'owner',
                'message': 'В этом контуре персональный вход сотрудников не настроен. Требуется отдельный staff-вход.'}

    # Preserve included router branches as well as direct routes. New FastAPI
    # versions keep include_router() branches without a .path instead of
    # flattening them into APIRoute objects. The public include_router() call
    # applies this app's dependency override provider through nested branches.
    # The outer boundary also refuses private paths at every nesting level.
    routes = [route for route in core.app.router.routes
              if not private_route(getattr(route, "path", "")) and getattr(route, "path", None) != "/api/health"]
    app.include_router(APIRouter(routes=routes))

    @app.get("/api/health")
    def health():
        return {"status": "ok", "portal": "closed"}

    @app.api_route("/api", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"], include_in_schema=False)
    @app.api_route("/api/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"], include_in_schema=False)
    def unknown_api(path: str = ""):
        raise HTTPException(404, "Не найдено")

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def production_ui(path: str):
        parts = path.split("/")
        first = parts[0].casefold()
        if first == "api" or first in _SOURCE_DIRECTORIES:
            raise HTTPException(404, "Не найдено")
        if path in {"", "index.html"}:
            target = _build_file(ui_root, "index.html")
        elif path in _ROOT_FILES:
            target = _build_file(ui_root, path)
        elif first == "assets":
            if len(parts) < 2 or Path(path).suffix.casefold() not in _ASSET_SUFFIXES:
                raise HTTPException(404, "Не найдено")
            target = _build_file(ui_root, path)
        elif any("." in part for part in parts):
            raise HTTPException(404, "Не найдено")
        else:
            target = _build_file(ui_root, "index.html")
        return FileResponse(target, media_type=mimetypes.guess_type(target.name)[0])

    return PortalBoundaryMiddleware(app, secret, marker)
