"""Fail-closed boundary for the private, loopback portal upstream."""
import os
from pathlib import Path
import re
import secrets
import stat

from starlette.responses import JSONResponse


BRIDGE_HEADER = b"x-school-bridge"
_TOKEN = re.compile(rb"[A-Za-z0-9_-]{43,128}\Z")
_SECURITY_HEADERS = {
    b"cache-control": b"no-store",
    b"x-content-type-options": b"nosniff",
    b"referrer-policy": b"no-referrer",
    b"content-security-policy": (
        b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        b"img-src 'self' data:; font-src 'self'; connect-src 'self'; "
        b"object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
    ),
    b"x-frame-options": b"DENY",
}


def load_configuration() -> tuple[bytes, Path]:
    """Read only the dedicated secret file, never the local demo's .env.

    Generate the file with secrets.token_urlsafe(32) or secrets.token_hex(32).
    A single final LF is accepted. The OS account running the upstream must own
    this regular, unlinked 0600 file. Restart the upstream after rotating it.
    """
    invalid = RuntimeError("Invalid closed portal configuration")
    if os.environ.get("SHKOLA_PORTAL_MODE") != "closed":
        raise invalid
    secret_value = os.environ.get("SHKOLA_GATEWAY_SECRET_FILE", "")
    ui_value = os.environ.get("SHKOLA_UI_DIRECTORY", "")
    if not secret_value or not ui_value:
        raise invalid
    secret_path, ui_path = Path(secret_value), Path(ui_value)
    if not secret_path.is_absolute() or not ui_path.is_absolute():
        raise invalid
    try:
        ui_root = ui_path.resolve(strict=True)
        if not ui_root.is_dir() or secret_path.resolve().is_relative_to(ui_root):
            raise invalid
        index = ui_root / "index.html"
        if index.is_symlink() or not index.is_file():
            raise invalid
        descriptor = os.open(secret_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_uid != os.geteuid() or info.st_nlink != 1
                    or not 43 <= info.st_size <= 129):
                raise invalid
            value = os.read(descriptor, 130)
            if len(value) != info.st_size:
                raise invalid
        finally:
            os.close(descriptor)
        if value.endswith(b"\n"):
            value = value[:-1]
        if not _TOKEN.fullmatch(value):
            raise invalid
        return value, ui_root
    except (OSError, ValueError):
        raise invalid from None


def forbidden_path(path: str) -> bool:
    """Reject ambiguous paths before routing or SPA fallback."""
    parts = path.split("/")
    return (not path.startswith("/") or "//" in path or "\\" in path or "%" in path
            or any(ord(character) < 32 or ord(character) == 127 for character in path)
            or any(part in {".", ".."} or part.startswith(".") for part in parts if part)
            or (len(parts) > 1 and parts[1].casefold() == "api" and parts[1] != "api"))


def private_route(path: str) -> bool:
    normalized = path.casefold().rstrip("/")
    return (normalized == "/api/demo" or normalized.startswith("/api/demo/")
            or any(normalized == root or normalized.startswith(root + "/")
                   for root in ("/docs", "/redoc", "/openapi", "/openapi.json")))


class PortalBoundaryMiddleware:
    """Require one exact bridge credential on every HTTP/WS request.

    This wraps the complete FastAPI app so even generated 500 responses and
    refusals receive the same headers and cannot establish browser sessions.
    """
    def __init__(self, app, secret: bytes, marker: object):
        self.app = app
        self._secret = secret
        self._marker = marker

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        async def protected_send(message):
            if message["type"] in {"http.response.start", "websocket.http.response.start"}:
                headers = [(name, value) for name, value in message.get("headers", [])
                           if name.lower() not in {*_SECURITY_HEADERS, b"set-cookie", BRIDGE_HEADER}]
                message = {**message, "headers": [*headers, *_SECURITY_HEADERS.items()]}
            await send(message)

        bridges = [value for name, value in scope.get("headers", []) if name.lower() == BRIDGE_HEADER]
        authorized = len(bridges) == 1 and secrets.compare_digest(bridges[0], self._secret)
        blocked = forbidden_path(scope["path"]) or private_route(scope["path"])
        if not authorized or blocked:
            if scope["type"] == "websocket":
                await protected_send({"type": "websocket.close", "code": 1008})
            else:
                status = 404 if authorized else 403
                await JSONResponse({"detail": "Доступ запрещён" if status == 403 else "Не найдено"}, status_code=status)(scope, receive, protected_send)
            return

        # Browser sessions and bearer credentials are not identities here.
        inner_scope = {**scope, "portal_bridge_marker": self._marker,
                       "headers": [(name, value) for name, value in scope.get("headers", [])
                                   if name.lower() not in {BRIDGE_HEADER, b"cookie", b"authorization"}]}
        await self.app(inner_scope, receive, protected_send)
