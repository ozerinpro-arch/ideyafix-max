"""Opaque in-memory DEMO sessions. Role selection is intentionally public.

This is a local demo convenience, not identity verification or production auth.
"""
import secrets
import threading
import time

from fastapi import Depends, HTTPException, Request

COOKIE_NAME = "shkola_demo_session"
SESSION_SECONDS = 8 * 60 * 60
PROFILES = {
    "director": {"name": "Анна Петрова", "title": "Директор · демо", "initials": "АП", "userId": "d1"},
    "admin": {"name": "Ольга Сергеева", "title": "Администратор · демо", "initials": "ОС", "userId": "ad1"},
    "teacher": {"name": "Мария Кузнецова", "title": "Учитель биологии · демо", "initials": "МК", "userId": "t1"},
    "student": {"name": "Алексей Соколов", "title": "Ученик 7А · демо", "initials": "АС", "userId": "s1"},
    "accountant": {"name": "Елена Орлова", "title": "Бухгалтер · демо", "initials": "ЕО", "userId": "ac1"},
}
_sessions: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()


def create_session(role: str, old_token: str | None = None, teacher_id: str | None = None) -> tuple[str, dict]:
    now = time.monotonic()
    token = secrets.token_urlsafe(32)
    profile = {"role": role, **PROFILES[role]}
    if role == "teacher" and teacher_id == "t2":
        profile.update(name="Елена Смирнова", title="Учитель замены · демо", initials="ЕС", userId="t2")
    with _lock:
        for expired in [key for key, (end, _) in _sessions.items() if end <= now]:
            del _sessions[expired]
        if old_token:
            _sessions.pop(old_token, None)
        # Bound memory even when a local client repeatedly creates sessions.
        if len(_sessions) >= 512:
            _sessions.pop(next(iter(_sessions)))
        _sessions[token] = (now + SESSION_SECONDS, profile)
    return token, profile.copy()


def session_profile(request: Request) -> dict:
    if request.scope.get("staff_identity_verified") is True:
        return request.scope["staff_profile"].copy()
    token = request.cookies.get(COOKIE_NAME, "")
    with _lock:
        session = _sessions.get(token)
        if not session or session[0] <= time.monotonic():
            _sessions.pop(token, None)
            raise HTTPException(401, "Выберите роль для локальной демонстрации")
        return session[1].copy()


def require_roles(*roles: str):
    """Dependency factory: Depends(require_roles('director', 'admin'))."""
    def checked(request: Request, profile: dict = Depends(session_profile)) -> dict:
        if profile.get("staffRole"):
            from .staff_policy import enforce, domain_profile
            enforce(profile, request.method, request.url.path)
            profile = domain_profile(profile, roles, request.url.path)
        if profile["role"] not in roles:
            raise HTTPException(403, "Для этой роли действие недоступно")
        return profile
    return checked
