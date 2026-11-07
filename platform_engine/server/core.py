"""Loopback-only demo API and isolated local timetable draft."""
from copy import deepcopy
from datetime import datetime
from typing import Literal
from uuid import uuid4
from pathlib import Path
import os
import re

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
# Keep local school traces out of hosted telemetry services.
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.responses import JSONResponse
# Load native libraries before accepting requests, outside a run's deadline.
# Cold macOS library validation can otherwise consume the first solve budget.
from ortools.sat.python import cp_model as _cp_model

from .auth import COOKIE_NAME, SESSION_SECONDS, create_session, require_roles, session_profile
from .db import edit_state, read_state, reset_state
from .workspace import school_workspace, demo_only_path, WorkspaceNeedsCleanup

MAX_BODY_BYTES = 64 * 1024
MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_CONFIG_BYTES = 512 * 1024
UPLOAD_PATHS = {"/api/timetable/preview", "/api/timetable/import"}
ALLOWED_HOSTS = {"127.0.0.1:5188", "127.0.0.1:8798", "localhost:5188", "localhost:8798"}
ALLOWED_ORIGINS = {f"http://{host}" for host in ALLOWED_HOSTS}
ARRAY_KEYS = ("schedule", "teachers", "students", "tasks", "documents", "notifications", "history", "replacements", "assignments", "materials")
INTERNAL_KEYS = {"roles", "userIds", "userId", "readBy", "ownerId", "replacementId", "studentId", "classNames", "requiredRoom"}


def request_body_limit(method, path):
    if method == 'POST' and path in {'/api/school/data-exchange/preview', '/api/school/data-exchange/apply'}:
        return 4 * 1024 * 1024
    if method == 'POST' and path in UPLOAD_PATHS:
        return MAX_UPLOAD_BYTES
    if method == 'POST' and (path == '/api/planning/config'
            or re.fullmatch(r'/api/planning/runs/[A-Za-z0-9_-]+/adjust', path) is not None
            or re.fullmatch(r'/api/planning/experimental-school/exp_[a-f0-9]{32}/edits/(preview|apply)', path) is not None):
        return MAX_CONFIG_BYTES
    return MAX_BODY_BYTES


def isolated_test_mode() -> bool:
    """Test suites may mutate only explicitly isolated temporary databases."""
    from .test_isolation import isolated_database_paths
    try:
        isolated_database_paths()
    except ValueError:
        return False
    return True


class LocalBoundaryMiddleware:
    """Check exact local Host/Origin and bound bodies before parsing JSON."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {}
        for key, value in scope["headers"]:
            name = key.decode("latin1").lower()
            # Duplicate security headers are ambiguous and therefore refused.
            if name in headers and name in {"host", "origin", "content-length"}:
                await JSONResponse({"detail": "Неоднозначные заголовки запроса"}, status_code=400)(scope, receive, send)
                return
            headers[name] = value.decode("latin1")
        if headers.get("host", "").lower() not in ALLOWED_HOSTS:
            await JSONResponse({"detail": "Демо доступно только через локальный адрес"}, status_code=403)(scope, receive, send)
            return
        origin = headers.get("origin")
        if school_workspace() and demo_only_path(scope['path']):
            await JSONResponse({'detail': 'Раздел недоступен в рабочей школе'}, status_code=404)(scope, receive, send)
            return
        if (origin is not None and origin not in ALLOWED_ORIGINS) or headers.get("sec-fetch-site") == "cross-site":
            await JSONResponse({"detail": "Внешний источник запроса запрещён"}, status_code=403)(scope, receive, send)
            return
        upload = scope["method"] == "POST" and scope["path"] in UPLOAD_PATHS
        limit = request_body_limit(scope['method'], scope['path'])
        try:
            length = int(headers.get("content-length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > limit:
            await JSONResponse({"detail": "Тело запроса превышает допустимый размер или имеет неверную длину"}, status_code=413)(scope, receive, send)
            return
        chunks = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > limit:
                await JSONResponse({"detail": "Тело запроса превышает допустимый размер"}, status_code=413)(scope, receive, send)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        expected_type = "application/octet-stream" if upload else "application/json"
        if body and scope["method"] in {"POST", "PUT", "PATCH"} and headers.get("content-type", "").split(";", 1)[0].strip().lower() != expected_type:
            await JSONResponse({"detail": "Неверный формат запроса"}, status_code=415)(scope, receive, send)
            return
        delivered = False

        async def replay_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        async def security_send(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).extend([
                    (b"x-content-type-options", b"nosniff"),
                    (b"cache-control", b"no-store"),
                    (b"referrer-policy", b"no-referrer"),
                ])
                if scope["path"] == "/api/health" and isolated_test_mode():
                    message["headers"].append((b"x-shkola-test-mode", b"isolated"))
            await send(message)

        await self.app(scope, replay_receive, security_send)


app = FastAPI(title="Идеяфикс · локальная демонстрация", docs_url=None, redoc_url=None)
app.add_middleware(LocalBoundaryMiddleware)


@app.exception_handler(RequestValidationError)
async def safe_validation_error(request: Request, exc: RequestValidationError):
    # Validation details may contain private input or unencodable Unicode.
    return JSONResponse({"detail": "Некорректные данные запроса"}, status_code=422)


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SessionInput(InputModel):
    role: Literal["director", "admin", "teacher", "student", "accountant"]
    teacherId: Literal["t1", "t2"] | None = None

    @model_validator(mode="after")
    def teacher_only(self):
        if self.teacherId is not None and self.role != "teacher":
            raise ValueError("teacherId доступен только для демо-роли учителя")
        return self


class ReplacementInput(InputModel):
    lessonId: str = Field(min_length=1, max_length=80)
    reason: str = Field(min_length=1, max_length=500)


class ApprovalInput(InputModel):
    teacherId: str = Field(min_length=1, max_length=80)


class TaskInput(InputModel):
    title: str = Field(min_length=1, max_length=150)
    description: str = Field(default="", max_length=1000)


class TaskStatusInput(InputModel):
    status: Literal["todo", "progress", "done"]


class SubmitInput(InputModel):
    answer: str = Field(min_length=1, max_length=5000)


class ReviewInput(InputModel):
    grade: int = Field(ge=2, le=5, strict=True)
    feedback: str = Field(min_length=1, max_length=1000)


class AssistantInput(InputModel):
    message: str = Field(min_length=1, max_length=2000)


def find(state: dict, collection: str, object_id: str) -> dict:
    for item in state[collection]:
        if item["id"] == object_id:
            return item
    raise HTTPException(404, "Запись не найдена")


def now() -> str:
    return datetime.now().strftime("%H:%M")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def visible(item: dict, profile: dict) -> bool:
    targets = item.get("userIds", [item["userId"]] if "userId" in item else [profile["userId"]])
    return profile["role"] in item.get("roles", []) and profile["userId"] in targets


def public_item(item: dict, *, assignment=False, material=False) -> dict:
    hidden = INTERNAL_KEYS | ({"teacherId", "className"} if assignment else set()) | ({"teacherId"} if material else set())
    return {key: deepcopy(value) for key, value in item.items() if key not in hidden}


def add_history(state: dict, title: str, detail: str, profile: dict, user_ids=None):
    targets = user_ids or ["d1", "ad1", profile["userId"]]
    related_roles = ["teacher" if user.startswith("t") else "student" if user.startswith("s") else "accountant" for user in targets if user.startswith(("t", "s", "ac"))]
    audiences = list(dict.fromkeys(["director", "admin", profile["role"], *related_roles]))
    state["history"].insert(0, {"id": new_id("h"), "title": title, "detail": detail, "time": now(), "actor": profile["name"], "roles": audiences, "userIds": user_ids or ["d1", "ad1", profile["userId"]]})


def add_notification(state: dict, title: str, text: str, roles: list[str], user_ids=None):
    state["notifications"].insert(0, {"id": new_id("n"), "title": title, "text": text, "time": now(), "read": False, "readBy": [], "roles": roles, **({"userIds": user_ids} if user_ids else {})})


def replacement_candidates(state: dict, lesson: dict, absent_teacher_id: str) -> list[dict]:
    """Resolve subject, teacher timetable and configured teaching room conflicts."""
    same_slot = [other for other in state["schedule"] if other["id"] != lesson["id"] and other["day"] == lesson["day"] and other["slot"] == lesson["slot"]]
    candidates = []
    for teacher in state["teachers"]:
        if teacher["id"] == absent_teacher_id or teacher["subject"] != lesson["subject"]:
            continue
        conflict = None
        busy = next((other for other in same_slot if other["teacherId"] == teacher["id"]), None)
        room = teacher.get("requiredRoom", lesson["room"])
        room_busy = next((other for other in same_slot if other["room"] == room), None)
        if busy:
            conflict = f"Занятие: {busy['className']} · {busy['time']}"
        elif room_busy:
            conflict = f"Кабинет {room} занят · {room_busy['className']}"
        candidates.append({**teacher, "available": conflict is None, "conflict": conflict})
    return sorted(candidates, key=lambda item: (not item["available"], item["id"]))


def teacher_assignment_access(state: dict, assignment: dict, user_id: str) -> bool:
    if assignment["teacherId"] == user_id:
        return True
    return any(lesson["teacherId"] == user_id
               and lesson["className"] == assignment["className"]
               and lesson["subject"] == assignment["subject"]
               for lesson in state["schedule"])


def project_state(state: dict, profile: dict) -> dict:
    state = deepcopy(state)
    reference_state = deepcopy(state)
    role, user = profile["role"], profile["userId"]
    if role == "student":
        student = find(state, "students", user)
        state["students"] = [student]
        state["schedule"] = [lesson for lesson in state["schedule"] if lesson["className"] == student["className"]]
        teacher_ids = {lesson["teacherId"] for lesson in state["schedule"]}
        state["teachers"] = [teacher for teacher in state["teachers"] if teacher["id"] in teacher_ids]
        state["assignments"] = [assignment for assignment in state["assignments"] if assignment["studentId"] == user]
        state["tasks"] = []
        state["documents"] = []
        state["replacements"] = []
        state["materials"] = [material for material in state["materials"] if student["className"] in material["classNames"]]
    elif role == "teacher":
        own_replacements = [replacement for replacement in state["replacements"] if user in {replacement["absentTeacherId"], replacement["teacherId"]}]
        relevant_lesson_ids = {replacement["lessonId"] for replacement in own_replacements}
        state["schedule"] = [lesson for lesson in state["schedule"] if lesson["teacherId"] == user or lesson["id"] in relevant_lesson_ids]
        classes = {lesson["className"] for lesson in state["schedule"]}
        state["students"] = [student for student in state["students"] if student["className"] in classes]
        state["assignments"] = [assignment for assignment in state["assignments"] if teacher_assignment_access(reference_state, assignment, user)]
        state["tasks"] = [task for task in state["tasks"] if task.get("ownerId") == user]
        state["documents"] = [document for document in state["documents"] if visible(document, profile)]
        state["replacements"] = own_replacements
        assigned_material_ids = {lesson["materialId"] for lesson in state["schedule"] if lesson["teacherId"] == user}
        state["materials"] = [material for material in state["materials"] if material["teacherId"] == user or material["id"] in assigned_material_ids]
        candidate_ids = {candidate for replacement in own_replacements for candidate in replacement["candidates"]}
        state["teachers"] = [teacher for teacher in state["teachers"] if teacher["id"] == user or teacher["id"] in candidate_ids]
    elif role == "accountant":
        for collection in ("schedule", "students", "assignments", "materials", "replacements", "teachers", "tasks"):
            state[collection] = []
        state["documents"] = [document for document in state["documents"] if visible(document, profile)]
    legacy_candidates = None
    for replacement in state["replacements"]:
        if replacement["status"] != "pending":
            continue
        lesson = find(reference_state, "schedule", replacement["lessonId"])
        candidates = replacement_candidates(reference_state, lesson, replacement["absentTeacherId"])
        replacement["candidates"] = [teacher["id"] for teacher in candidates]
        replacement["candidateDetails"] = [public_item(teacher) for teacher in candidates]
        if legacy_candidates is None:
            # Keep the shared teachers projection compatible with the first request.
            legacy_candidates = {teacher["id"]: teacher for teacher in candidates}
    if legacy_candidates is not None:
        state["teachers"] = [{**teacher, **legacy_candidates.get(teacher["id"], {})} for teacher in state["teachers"]]
    state["notifications"] = [notification for notification in state["notifications"] if visible(notification, profile)]
    for notification in state["notifications"]:
        notification["read"] = user in notification.get("readBy", [])
    state["history"] = [history for history in state["history"] if visible(history, profile)]
    return {
        "workspaceMode": "school" if school_workspace() else "demo",
        "role": role,
        "profile": {key: profile[key] for key in ("name", "title", "initials", "userId")},
        "school": state["school"],
        **{collection: [public_item(item, assignment=collection == "assignments", material=collection == "materials") for item in state.get(collection, [])] for collection in ARRAY_KEYS},
    }


@app.get("/api/health")
def health():
    read_state()
    return {"status": "ok", "demo": True}


@app.post("/api/demo/session")
def demo_session(body: SessionInput, request: Request, response: Response):
    token, profile = create_session(body.role, request.cookies.get(COOKIE_NAME), body.teacherId)
    response.set_cookie(COOKIE_NAME, token, max_age=SESSION_SECONDS, httponly=True, samesite="strict", secure=False, path="/api")
    return {"demo": True, "role": body.role, "profile": {key: profile[key] for key in ("name", "title", "initials", "userId")}}


@app.get("/api/state")
def get_state(profile: dict = Depends(session_profile)):
    try:
        return project_state(read_state(), profile)
    except WorkspaceNeedsCleanup:
        raise HTTPException(503, 'Рабочий кабинет ожидает завершения очистки демоданных') from None


@app.post("/api/replacements")
def request_replacement(body: ReplacementInput, profile: dict = Depends(require_roles("teacher", "admin", "director"))):
    with edit_state() as state:
        lesson = find(state, "schedule", body.lessonId)
        existing = next((item for item in state["replacements"] if item["lessonId"] == lesson["id"]), None)
        owns_existing = existing and existing["absentTeacherId"] == profile["userId"]
        if profile["role"] == "teacher" and lesson["teacherId"] != profile["userId"] and not owns_existing:
            raise HTTPException(403, "Можно запросить замену только своего урока")
        if existing:
            return public_item(existing)
        if lesson["status"] != "normal":
            raise HTTPException(409, "Урок уже находится в другом состоянии")
        replacement = {"id": new_id("r"), "lessonId": lesson["id"], "absentTeacherId": lesson["teacherId"], "teacherId": None, "reason": body.reason, "status": "pending", "candidates": [teacher["id"] for teacher in replacement_candidates(state, lesson, lesson["teacherId"])]}
        state["replacements"].append(replacement)
        state["tasks"].append({"id": new_id("task"), "title": f"Согласовать замену · {lesson['className']}", "description": body.reason, "category": "Расписание", "status": "todo", "due": "Сегодня", "owner": "Администратор", "ownerId": "ad1", "roles": ["admin", "director"], "replacementId": replacement["id"]})
        add_history(state, "Запрошена замена", f"{lesson['subject']} · {lesson['className']}", profile)
        add_notification(state, "Новая заявка на замену", f"{lesson['subject']} · {lesson['className']} · {lesson['time']}", ["director", "admin"])
        return public_item(replacement)


@app.post("/api/replacements/{replacement_id}/approve")
def approve_replacement(replacement_id: str, body: ApprovalInput, profile: dict = Depends(require_roles("admin", "director"))):
    with edit_state() as state:
        replacement = find(state, "replacements", replacement_id)
        if replacement["status"] != "pending":
            if replacement["teacherId"] == body.teacherId:
                return public_item(replacement)
            raise HTTPException(409, "Замена уже согласована с другим педагогом")
        lesson = find(state, "schedule", replacement["lessonId"])
        candidates = replacement_candidates(state, lesson, replacement["absentTeacherId"])
        candidate = next((teacher for teacher in candidates if teacher["id"] == body.teacherId), None)
        if candidate is None:
            raise HTTPException(409, "Педагог не подходит по предмету")
        if not candidate["available"]:
            raise HTTPException(409, candidate["conflict"])
        replacement.update(teacherId=candidate["id"], status="approved")
        lesson.update(teacherId=candidate["id"], teacherName=candidate["name"], status="replacement")
        for task in state["tasks"]:
            if task.get("replacementId") == replacement_id:
                task["status"] = "done"
        state["tasks"].append({"id": new_id("task"), "title": f"Провести замену · {lesson['className']}", "description": f"{lesson['subject']} · {lesson['time']} · кабинет {lesson['room']}", "category": "Расписание", "status": "todo", "due": "Сегодня", "owner": candidate["name"], "ownerId": candidate["id"], "roles": ["teacher", "director", "admin"], "replacementId": replacement_id})
        add_history(state, "Замена согласована", f"{candidate['name']} · {lesson['subject']} · {lesson['className']}", profile, ["d1", "ad1", replacement["absentTeacherId"], candidate["id"]])
        add_notification(state, "Замена согласована", f"{lesson['className']} · {lesson['time']} · {candidate['name']}", ["director", "admin", "teacher"], ["d1", "ad1", replacement["absentTeacherId"], candidate["id"]])
        return public_item(replacement)


@app.post("/api/lessons/{lesson_id}/complete")
def complete_lesson(lesson_id: str, profile: dict = Depends(require_roles("teacher", "admin", "director"))):
    with edit_state() as state:
        lesson = find(state, "schedule", lesson_id)
        if profile["role"] == "teacher" and lesson["teacherId"] != profile["userId"]:
            raise HTTPException(403, "Проведение отмечает назначенный педагог")
        replacement = next((item for item in state["replacements"] if item["lessonId"] == lesson_id), None)
        if not replacement or replacement["status"] == "pending":
            raise HTTPException(409, "Сначала согласуйте замену")
        if replacement["status"] == "completed":
            return public_item(lesson)
        replacement["status"] = "completed"
        lesson["status"] = "completed"
        for task in state["tasks"]:
            if task.get("replacementId") == replacement["id"]:
                task["status"] = "done"
        add_history(state, "Урок проведён", f"{lesson['subject']} · {lesson['className']} · {lesson['teacherName']}", profile, ["d1", "ad1", replacement["absentTeacherId"], lesson["teacherId"]])
        add_notification(state, "Проведение урока подтверждено", f"{lesson['subject']} · {lesson['className']}", ["director", "admin", "teacher"], ["d1", "ad1", replacement["absentTeacherId"], lesson["teacherId"]])
        return public_item(lesson)


@app.post("/api/tasks")
def create_task(body: TaskInput, profile: dict = Depends(require_roles("teacher", "admin", "director"))):
    with edit_state() as state:
        existing = next((task for task in state["tasks"] if task.get("ownerId") == profile["userId"] and task["title"] == body.title and task["description"] == body.description), None)
        if existing:
            return public_item(existing)
        task = {"id": new_id("task"), "title": body.title, "description": body.description, "category": "Организация", "status": "todo", "due": "Сегодня", "owner": profile["name"], "ownerId": profile["userId"], "roles": ["director", "admin", profile["role"]]}
        state["tasks"].append(task)
        add_history(state, "Задача создана", body.title, profile)
        return public_item(task)


@app.post("/api/tasks/{task_id}/status")
def set_task_status(task_id: str, body: TaskStatusInput, profile: dict = Depends(require_roles("teacher", "admin", "director"))):
    with edit_state() as state:
        task = find(state, "tasks", task_id)
        if profile["role"] == "teacher" and task.get("ownerId") != profile["userId"]:
            raise HTTPException(403, "Можно менять только свою задачу")
        if task["status"] != body.status:
            task["status"] = body.status
            add_history(state, "Статус задачи изменён", f"{task['title']} · {body.status}", profile)
        return public_item(task)


@app.post("/api/documents/{document_id}/approve")
def approve_document(document_id: str, profile: dict = Depends(require_roles("admin", "director", "accountant"))):
    with edit_state() as state:
        document = find(state, "documents", document_id)
        if not visible(document, profile):
            raise HTTPException(403, "Документ недоступен этой роли")
        if document["status"] == "draft":
            raise HTTPException(409, "Черновик ещё не передан на согласование")
        if document["status"] != "approved":
            document["status"] = "approved"
            add_history(state, "Документ согласован", document["title"], profile)
        return public_item(document)


@app.post("/api/assignments/{assignment_id}/submit")
def submit_assignment(assignment_id: str, body: SubmitInput, profile: dict = Depends(require_roles("student"))):
    with edit_state() as state:
        assignment = find(state, "assignments", assignment_id)
        if assignment["studentId"] != profile["userId"]:
            raise HTTPException(403, "Можно отправить только свой ответ")
        if assignment["status"] == "reviewed":
            raise HTTPException(409, "Работа уже проверена")
        if assignment["status"] == "submitted" and assignment["answer"] == body.answer:
            return public_item(assignment, assignment=True)
        assignment.update(answer=body.answer, status="submitted")
        add_history(state, "Работа отправлена", assignment["title"], profile, ["d1", "ad1", profile["userId"], assignment["teacherId"]])
        add_notification(state, "Работа ждёт проверки", assignment["title"], ["teacher", "director"], [assignment["teacherId"], "d1"])
        return public_item(assignment, assignment=True)


@app.post("/api/assignments/{assignment_id}/review")
def review_assignment(assignment_id: str, body: ReviewInput, profile: dict = Depends(require_roles("teacher", "director"))):
    with edit_state() as state:
        assignment = find(state, "assignments", assignment_id)
        if profile["role"] == "teacher" and not teacher_assignment_access(state, assignment, profile["userId"]):
            raise HTTPException(403, "Можно проверить только свою работу")
        if assignment["status"] == "todo":
            raise HTTPException(409, "Ученик ещё не отправил работу")
        if assignment["status"] == "reviewed" and assignment["grade"] == body.grade and assignment["feedback"] == body.feedback:
            return public_item(assignment, assignment=True)
        assignment.update(grade=body.grade, feedback=body.feedback, status="reviewed")
        add_history(state, "Работа проверена", f"{assignment['title']} · {body.grade}", profile, ["d1", "ad1", profile["userId"], assignment["studentId"]])
        add_notification(state, "Работа проверена", f"{assignment['title']} · оценка {body.grade}", ["student"], [assignment["studentId"]])
        return public_item(assignment, assignment=True)


@app.post("/api/notifications/read")
def read_notifications(profile: dict = Depends(session_profile)):
    with edit_state() as state:
        for notification in state["notifications"]:
            if visible(notification, profile) and profile["userId"] not in notification.setdefault("readBy", []):
                notification["readBy"].append(profile["userId"])
    return {"ok": True}


@app.post("/api/demo/reset")
def reset_demo(profile: dict = Depends(require_roles("director", "admin"))):
    reset_state()
    return {"ok": True, "demo": True}


@app.post("/api/assistant")
def assistant(body: AssistantInput, profile: dict = Depends(session_profile)):
    state = project_state(read_state(), profile)
    message = body.message.lower()
    if "замен" in message:
        pending = sum(item["status"] == "pending" for item in state["replacements"])
        reply = f"В доступном вам демо-сценарии заявок на замену: {pending}. Откройте «Замены», проверьте занятость и кабинет, затем согласуйте свободного педагога."
        suggestions = ["Открыть замены", "Показать расписание"]
    elif any(word in message for word in ("задан", "работ", "урок", "учёб")):
        pending = sum(item["status"] == "todo" for item in state["assignments"])
        reply = f"В вашем демо-наборе {len(state['schedule'])} уроков и {pending} неотправленных заданий. Учебные материалы доступны в соответствующем разделе."
        suggestions = ["Открыть задания", "Открыть материалы"]
    elif "документ" in message:
        review = sum(item["status"] == "review" for item in state["documents"])
        reply = f"В доступных вам учебных документах ожидают согласования: {review}. Откройте документ, прочитайте содержание и подтвердите решение."
        suggestions = ["Открыть документы"]
    else:
        reply = "Помогу пройти демонстрационный сценарий: согласовать замену, открыть материал, отправить учебную работу или обновить задачу. Выберите нужный раздел."
        suggestions = ["Показать расписание", "Открыть задачи", "Открыть документы"]
    return {"reply": "Демо-помощник · локальный сценарий. " + reply, "suggestions": suggestions}


# The financial demonstration is maintained separately; no calculations here.
try:
    from .finance import router as finance_router
except ImportError:
    finance_router = None
if finance_router is not None:
    app.include_router(finance_router)

# Local working draft and durable agent are isolated from the synthetic demo.
from .timetable import router as timetable_router
from .schedule_agent import router as schedule_agent_router
app.include_router(timetable_router)
from .dated_timetable import router as dated_timetable_router
app.include_router(dated_timetable_router)
app.include_router(schedule_agent_router)
from .planning import router as planning_router
app.include_router(planning_router)
from .school_ops import router as school_ops_router
from .school_clubs import router as school_clubs_router
app.include_router(school_ops_router)
from .school_data_exchange import router as school_data_exchange_router
from .import_header_analysis import router as import_header_analysis_router
app.include_router(school_data_exchange_router)
app.include_router(import_header_analysis_router)
app.include_router(school_clubs_router)

# Prepared school features share the authenticated host, never demo identities.
from .school_calendar import router as school_calendar_router
from .launch_runtime import router as launch_runtime_router
from .launch_identity import install as install_launch_identity
app.include_router(school_calendar_router)
app.include_router(launch_runtime_router)
install_launch_identity()

from .school_publications import router as school_publications_router
app.include_router(school_publications_router)

from .school_requests import router as school_requests_router
app.include_router(school_requests_router)
from .school_integrations import router as school_integrations_router
app.include_router(school_integrations_router)
from .school_analytics import router as school_analytics_router
app.include_router(school_analytics_router)

from .director_analytics import router as director_analytics_router
app.include_router(director_analytics_router)

from .school_replacements import router as school_replacements_router
app.include_router(school_replacements_router)

from .candidate_projection import router as candidate_projection_router
app.include_router(candidate_projection_router)
