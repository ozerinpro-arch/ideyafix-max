"""Manual school registries, separate from timetable data and account access.

Reads use a SQLite read-only snapshot and never initialize storage. Mutations
use the existing domain transaction, with one compare-and-swap and audit entry.
No URLs are fetched, external messages sent, or accounts provisioned here.
"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from datetime import date, datetime, timezone
import ipaddress
import json
import re
import sqlite3
from typing import Any, Literal
import unicodedata
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from . import db, timetable
from .auth import session_profile
from .workspace import WorkspaceNeedsCleanup, empty_school_state, school_workspace, verify_school_state

MAX_REVISION = 2**31 - 1
HISTORY_LIMIT = 1000
MAX_OPS_BYTES = 8 * 1024 * 1024
RECORD_LIMITS = {
    "students": 5000, "teacherProfiles": 500, "tasks": 2000,
    "documents": 1000, "assignments": 5000, "materials": 2000,
    "absences": 2000, "notifications": 2000,
}
KINDS = tuple(RECORD_LIMITS)
SERVER_FIELDS = {"id", "createdAt", "updatedAt", "archived", "timetableRevision"}


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True, allow_inf_nan=False)

    @field_validator("*", mode="after")
    @classmethod
    def safe_text(cls, value):
        if isinstance(value, str) and any(
            unicodedata.category(char) == "Cs" or
            (unicodedata.category(char) == "Cc" and char not in "\n\r\t")
            for char in value
        ):
            raise ValueError("Недопустимые управляющие символы")
        return value


class StudentInput(StrictInput):
    name: str = Field(min_length=1, max_length=160)
    className: str = Field(default="", max_length=80)
    note: str = Field(default="", max_length=4000)
    attendancePercent: float | None = Field(default=None, ge=0, le=100)
    averageGrade: float | None = Field(default=None, ge=0, le=5)


class TeacherInput(StrictInput):
    name: str = Field(min_length=1, max_length=160)
    subject: str = Field(default="", max_length=160)
    note: str = Field(default="", max_length=4000)
    weeklyHours: float | None = Field(default=None, ge=0, le=100)
    available: bool | None = None


class TaskInput(StrictInput):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    status: Literal["todo", "progress", "done"] = "todo"
    due: str = Field(default="", max_length=10)
    owner: str = Field(default="", max_length=160)

    @field_validator("due")
    @classmethod
    def iso_date(cls, value):
        if value and (not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) or date.fromisoformat(value).isoformat() != value):
            raise ValueError("Требуется дата YYYY-MM-DD")
        return value


class DocumentInput(StrictInput):
    title: str = Field(min_length=1, max_length=200)
    type: str = Field(default="", max_length=80)
    content: str = Field(default="", max_length=20000)
    status: Literal["draft", "review", "approved"] = "draft"


class AssignmentInput(StrictInput):
    title: str = Field(min_length=1, max_length=200)
    subject: str = Field(default="", max_length=160)
    className: str = Field(default="", max_length=80)
    description: str = Field(default="", max_length=4000)
    due: str = Field(default="", max_length=10)
    status: Literal["todo", "submitted", "reviewed"] = "todo"
    studentId: str = Field(default="", max_length=80)
    answer: str = Field(default="", max_length=10000)
    feedback: str = Field(default="", max_length=4000)
    grade: float | None = Field(default=None, ge=0, le=5)

    @field_validator("due")
    @classmethod
    def iso_date(cls, value):
        return TaskInput.iso_date(value)


def _safe_link(value: str) -> None:
    """Only unambiguous public HTTP(S) references; never resolve or fetch them."""
    if len(value) > 2048 or any(char.isspace() for char in value) or "\\" in value:
        raise ValueError("Некорректная ссылка")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if parsed.scheme not in {"http", "https"} or not host or parsed.username is not None or parsed.password is not None:
            raise ValueError("Требуется публичная ссылка http/https без пароля")
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError("Некорректный порт")
        normalized = host.rstrip(".").casefold()
        if "%" in normalized or normalized == "localhost" or normalized.endswith((".localhost", ".local", ".internal", ".test")):
            raise ValueError("Локальные ссылки недоступны")
        try:
            address = ipaddress.ip_address(normalized)
        except ValueError:
            encoded = normalized.encode("idna").decode("ascii").rstrip(".").casefold()
            # Browser URL parsers also accept shortened, octal and hexadecimal
            # IPv4 forms. A numeric final label enters that parser, not DNS.
            numeric_last_label = re.fullmatch(r"(?:[0-9]+|0x[0-9a-f]*)", encoded.rsplit(".", 1)[-1])
            if ("." not in encoded or len(encoded) > 253 or
                    encoded == "localhost" or encoded.endswith((".localhost", ".local", ".internal", ".test")) or
                    not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in encoded.split(".")) or
                    numeric_last_label):
                raise ValueError("Некорректное имя сайта")
        else:
            if not address.is_global:
                raise ValueError("Локальные ссылки недоступны")
    except (UnicodeError, ValueError):
        raise ValueError("Требуется публичная ссылка http/https без пароля") from None


class MaterialInput(StrictInput):
    title: str = Field(min_length=1, max_length=200)
    subject: str = Field(default="", max_length=160)
    type: Literal["text", "link"] = "text"
    content: str = Field(default="", max_length=20000)

    @model_validator(mode="after")
    def safe_url(self):
        if self.type == "link":
            _safe_link(self.content)
        return self


class AbsenceInput(StrictInput):
    teacherName: str = Field(min_length=1, max_length=160)
    day: int = Field(ge=0, le=5)
    reason: str = Field(default="", max_length=2000)
    status: Literal["open", "resolved"] = "open"


class NotificationInput(StrictInput):
    title: str = Field(min_length=1, max_length=200)
    text: str = Field(default="", max_length=4000)
    read: bool = False


class SettingsInput(StrictInput):
    schoolName: str = Field(default="", max_length=200)
    academicYear: str = Field(default="", max_length=40)
    note: str = Field(default="", max_length=4000)


class RevisionInput(StrictInput):
    expectedRevision: int = Field(ge=0, le=MAX_REVISION)


class RecordWriteInput(RevisionInput):
    record: dict[str, Any]


class SettingsWriteInput(RevisionInput):
    settings: dict[str, Any]


MODELS = {
    "students": StudentInput, "teacherProfiles": TeacherInput, "tasks": TaskInput,
    "documents": DocumentInput, "assignments": AssignmentInput, "materials": MaterialInput,
    "absences": AbsenceInput, "notifications": NotificationInput,
}


def _empty_ops() -> dict:
    return {"version": 1, "revision": 0, "records": {kind: [] for kind in KINDS},
            "history": [], "settings": SettingsInput().model_dump()}


def _ops(state: dict) -> dict:
    value = state.get("_schoolOps")
    if value is None:
        return _empty_ops()
    if (not isinstance(value, dict) or value.get("version") != 1 or
            type(value.get("revision")) is not int or not 0 <= value["revision"] <= MAX_REVISION or
            not isinstance(value.get("records"), dict) or
            any(not isinstance(value["records"].get(kind), list) for kind in KINDS) or
            not isinstance(value.get("history"), list) or not isinstance(value.get("settings"), dict)):
        raise HTTPException(503, "Школьное хранилище требует проверки")
    return value


def _table_exists(connection, name: str) -> bool:
    return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _load_timetable(connection) -> dict:
    if connection is None or not _table_exists(connection, "timetable_draft"):
        return timetable._empty()
    row = connection.execute("SELECT payload FROM timetable_draft WHERE id=1").fetchone()
    return timetable._public(json.loads(row[0])) if row else timetable._empty()


def _read_state(connection, *, required=False) -> dict:
    row = connection.execute("SELECT payload FROM demo_state WHERE id=1").fetchone() if required or _table_exists(connection, "demo_state") else None
    if required and row is None:
        raise HTTPException(503, "Школьное хранилище требует проверки")
    state = json.loads(row[0]) if row else empty_school_state()
    try:
        verify_school_state(state)
    except WorkspaceNeedsCleanup:
        raise HTTPException(503, "Рабочий кабинет ожидает завершения очистки демоданных") from None
    return state


def _snapshot(*, raw_draft=False) -> tuple[dict, dict]:
    path = db.database_path()
    if not path.exists():
        return empty_school_state(), timetable._empty()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=15)) as connection:
        connection.execute("BEGIN")
        state = _read_state(connection)
        if raw_draft:
            # Internal analytics selector only; existing HTTP callers keep the
            # public projection. Preserve parser/planning evidence in the same
            # read-only snapshot, never expose it via a request parameter.
            row = connection.execute("SELECT payload FROM timetable_draft WHERE id=1").fetchone() if _table_exists(connection, "timetable_draft") else None
            return state, deepcopy(json.loads(row[0]) if row else timetable._empty())
        return state, _load_timetable(connection)


def _directory(draft: dict) -> dict:
    teachers, classes, rooms = {}, {}, {}
    for lesson in draft["lessons"]:
        class_name = lesson["className"]
        classes[class_name] = classes.get(class_name, 0) + 1
        for room in set(lesson["rooms"]):
            rooms[room] = rooms.get(room, 0) + 1
        for name in set(lesson["teachers"]):
            teacher = teachers.setdefault(name, {"key": name, "name": name, "lessonCount": 0, "classNames": set(), "subjects": set()})
            teacher["lessonCount"] += 1
            teacher["classNames"].add(class_name)
            if lesson["subject"]:
                teacher["subjects"].add(lesson["subject"])
    return {
        "teachers": [{**value, "classNames": sorted(value["classNames"]), "subjects": sorted(value["subjects"])}
                     for _, value in sorted(teachers.items())],
        "classes": [{"name": name, "lessonCount": count} for name, count in sorted(classes.items())],
        "rooms": [{"name": name, "lessonCount": count} for name, count in sorted(rooms.items())],
    }


def _school_only():
    if not school_workspace():
        raise HTTPException(404, "Раздел доступен только в рабочей школе")


def _authorized(profile: dict = Depends(session_profile)) -> dict:
    if (profile.get("role") not in {"director", "admin"} or
            not isinstance(profile.get("userId"), str) or not 1 <= len(profile["userId"]) <= 80):
        raise HTTPException(403, "Для этой роли действие недоступно")
    return profile


def _kind(kind: str):
    if kind not in MODELS:
        raise HTTPException(404, "Неизвестный вид записей")


def _cas(ops: dict, expected_revision: int):
    if ops["revision"] != expected_revision:
        raise HTTPException(409, "Школьные данные изменены: обновите страницу и проверьте правку")
    if ops["revision"] >= MAX_REVISION:
        raise HTTPException(409, "Достигнут лимит ревизий школьных данных")


def _validate(kind: str, record: dict) -> dict:
    try:
        return MODELS[kind].model_validate(record).model_dump()
    except ValidationError:
        raise HTTPException(422, "Некорректные данные запроса") from None


def _record(ops: dict, kind: str, record_id: str) -> dict:
    if not 1 <= len(record_id) <= 80:
        raise HTTPException(404, "Запись не найдена")
    found = next((record for record in ops["records"][kind] if record.get("id") == record_id), None)
    if found is None:
        raise HTTPException(404, "Запись не найдена")
    return found


def _references(ops: dict, kind: str, record: dict):
    if kind == "assignments" and record["studentId"]:
        student = next((row for row in ops["records"]["students"] if row["id"] == record["studentId"] and not row["archived"]), None)
        if student is None:
            raise HTTPException(422, "Ученик не найден или находится в архиве")
    if kind == "absences":
        _, draft = _snapshot()
        if not any(record["teacherName"] in lesson["teachers"] for lesson in draft["lessons"]):
            raise HTTPException(422, "Учитель не найден в действующем расписании")
        return draft["revision"]
    return None


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _commit(state: dict, ops: dict, profile: dict, action: str, kind: str, record: dict, stamp: str):
    ops["revision"] += 1
    title = "Запись ученика" if kind == "students" else record.get("title", record.get("name", record.get("teacherName", "Сведения о школе")))
    ops["history"].insert(0, {"id": str(uuid4()), "at": stamp,
        "actor": profile["userId"], "action": action, "kind": kind,
        "recordId": record.get("id", ""), "title": title})
    del ops["history"][HISTORY_LIMIT:]
    if len(json.dumps(ops, ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_OPS_BYTES:
        raise HTTPException(409, "Достигнут лимит школьного хранилища")
    state["_schoolOps"] = ops


router = APIRouter(prefix="/api/school", tags=["manual school records"], dependencies=[Depends(_school_only)])


@router.get("/workspace")
def get_workspace(profile: dict = Depends(_authorized)):
    state, draft = _snapshot()
    ops = _ops(state)
    settings = deepcopy(ops["settings"])
    return {"revision": ops["revision"],
        "school": {"name": settings["schoolName"] or state.get("school", {}).get("name") or "Идеяфикс"},
        "source": {"sourceName": draft["sourceName"], "revision": draft["revision"],
                   "lessonCount": len(draft["lessons"]), "issueCount": len(draft["issues"])},
        "directory": _directory(draft), "records": {kind: deepcopy(ops["records"][kind]) for kind in KINDS},
        "history": deepcopy(ops["history"]), "settings": settings}


@router.get("/timetable")
def get_timetable(profile: dict = Depends(_authorized)):
    return _snapshot()[1]


@router.post("/records/{kind}")
def create_record(kind: str, body: RecordWriteInput, profile: dict = Depends(_authorized)):
    _kind(kind)
    value = _validate(kind, body.record)
    with db.edit_state() as state:
        ops = _ops(state)
        _cas(ops, body.expectedRevision)
        if len(ops["records"][kind]) >= RECORD_LIMITS[kind]:
            raise HTTPException(409, "Достигнут лимит записей этого раздела")
        revision = _references(ops, kind, value)
        stamp = _stamp()
        record = {**value, "id": str(uuid4()), "createdAt": stamp, "updatedAt": stamp, "archived": False}
        if kind == "absences":
            record["timetableRevision"] = revision
        ops["records"][kind].append(record)
        _commit(state, ops, profile, "create", kind, record, stamp)
        result = {"revision": ops["revision"], "record": deepcopy(record)}
    return result


@router.patch("/records/{kind}/{record_id}")
def update_record(kind: str, record_id: str, body: RecordWriteInput, profile: dict = Depends(_authorized)):
    _kind(kind)
    if not body.record or set(body.record) & SERVER_FIELDS:
        raise HTTPException(422, "Некорректные данные запроса")
    with db.edit_state() as state:
        ops = _ops(state)
        _cas(ops, body.expectedRevision)
        record = _record(ops, kind, record_id)
        if record["archived"]:
            raise HTTPException(409, "Сначала восстановите запись из архива")
        value = _validate(kind, {**{key: value for key, value in record.items() if key not in SERVER_FIELDS}, **body.record})
        absence_binding_changed = kind == "absences" and any(value[field] != record[field] for field in ("teacherName", "day"))
        revision = _references(ops, kind, value) if kind != "absences" or absence_binding_changed else None
        stamp = _stamp()
        record.update(value, updatedAt=stamp)
        if absence_binding_changed:
            record["timetableRevision"] = revision
        _commit(state, ops, profile, "update", kind, record, stamp)
        result = {"revision": ops["revision"], "record": deepcopy(record)}
    return result


def _archive(kind: str, record_id: str, body: RevisionInput, profile: dict, archived: bool):
    _kind(kind)
    with db.edit_state() as state:
        ops = _ops(state)
        _cas(ops, body.expectedRevision)
        record = _record(ops, kind, record_id)
        if record["archived"] is archived:
            raise HTTPException(409, "Запись уже в выбранном состоянии архива")
        if archived and kind == "students" and any(
            row["studentId"] == record_id and not row["archived"] for row in ops["records"]["assignments"]
        ):
            raise HTTPException(409, "Сначала архивируйте связанные задания ученика")
        if not archived and kind != "absences":
            _references(ops, kind, record)
        stamp = _stamp()
        record.update(archived=archived, updatedAt=stamp)
        _commit(state, ops, profile, "archive" if archived else "restore", kind, record, stamp)
        result = {"revision": ops["revision"], "record": deepcopy(record)}
    return result


@router.post("/records/{kind}/{record_id}/archive")
def archive_record(kind: str, record_id: str, body: RevisionInput, profile: dict = Depends(_authorized)):
    return _archive(kind, record_id, body, profile, True)


@router.post("/records/{kind}/{record_id}/restore")
def restore_record(kind: str, record_id: str, body: RevisionInput, profile: dict = Depends(_authorized)):
    return _archive(kind, record_id, body, profile, False)


@router.patch("/settings")
def update_settings(body: SettingsWriteInput, profile: dict = Depends(_authorized)):
    if not body.settings:
        raise HTTPException(422, "Некорректные данные запроса")
    with db.edit_state() as state:
        ops = _ops(state)
        _cas(ops, body.expectedRevision)
        try:
            settings = SettingsInput.model_validate({**ops["settings"], **body.settings}).model_dump()
        except ValidationError:
            raise HTTPException(422, "Некорректные данные запроса") from None
        ops["settings"] = settings
        _commit(state, ops, profile, "update", "settings", {}, _stamp())
        result = {"revision": ops["revision"], "settings": deepcopy(settings)}
    return result
