"""Local, durable scheduling coordination. No model, network or delivery calls."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import fcntl
from fastapi import APIRouter, Depends, HTTPException, Query, Path as APIPath
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .auth import require_roles
from .db import database_path, table_exists
from .scheduling_models import Assignment, PlanningInput, planning_readiness
from .scheduling_solver import solve_plan, validate_solution, solution_metrics, compare_assignments
from .timetable import LessonEditInput, canonical, mutation_hash, read_draft
from .school_source_registry import RegistrySaveInput

ACTIVE = {"queued", "running"}
CANDIDATES = {"optimal", "feasible"}
RUN_LIMIT = 20
QUESTION_LIMIT = 100
PROCESS_TOKEN = secrets.token_hex(16)
LOCAL_NOTICE = "MAX не подключён. Это локальные карточки вопросов; сообщения никому не отправлялись. Локальный срок по умолчанию — 24 ч; политика школы и MAX ещё не настроены."
LESSON_FIELDS = ("id", "className", "day", "slot", "start", "end", "subject", "teachers", "rooms")
EMPTY_SUMMARY = {"added": 0, "changed": 0, "removed": 0, "unchanged": 0, "totalBefore": 0, "totalAfter": 0}
_locks: dict[str, threading.RLock] = {}
_executors: dict[str, ThreadPoolExecutor] = {}
_global_lock = threading.Lock()


def _utc():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _actor(profile):
    if (not isinstance(profile, dict) or profile.get("role") not in {"admin", "director"}
            or not isinstance(profile.get("userId"), str) or not 1 <= len(profile["userId"]) <= 80):
        raise HTTPException(403, "Для этой роли планирование недоступно")
    return profile["userId"], profile["role"]


def _block(code, message, ids=()):
    return {"code": code, "message": message, "entityIds": list(ids)}


def _verify_run_result(snapshot, result):
    """Recheck both grids and derive reports from assignments, never stored claims."""
    if result['status'] not in CANDIDATES:
        return result
    result = deepcopy(result)
    checked = validate_solution(snapshot, result.get('assignments', []))
    result['validation'] = checked
    if not checked['valid']:
        result['status'] = 'invalid'
        result.pop('comparison', None)
        return result
    result.setdefault('metrics', {}).update(solution_metrics(snapshot, result['assignments']))
    control = result.get('control')
    if control is not None:
        checked_control = validate_solution(snapshot, control.get('assignments', []))
        control['validation'] = checked_control
        if not checked_control['valid']:
            result['status'] = 'invalid'
            result.pop('comparison', None)
            result.setdefault('diagnostics', []).append(_block(
                'invalid_control', 'Контрольный вариант не прошёл независимую проверку; сравнение нельзя принять.'))
            return result
        control['metrics'] = solution_metrics(snapshot, control['assignments'])
        result['comparison'] = compare_assignments(snapshot, control['assignments'], result['assignments'])
    else:
        result.pop('comparison', None)
    return result


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class ConfigInput(InputModel):
    expectedRevision: int = Field(ge=0, le=2**31 - 1)
    baseDraftRevision: int = Field(ge=0, le=2**31 - 1)
    input: PlanningInput


class FromDraftInput(InputModel):
    expectedRevision: int = Field(ge=0, le=2**31 - 1)
    expectedDraftRevision: int = Field(ge=0, le=2**31 - 1)
    periodStart: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

    @field_validator("periodStart")
    @classmethod
    def monday(cls, value):
        if date.fromisoformat(value).weekday() != 0:
            raise ValueError("Начало недели должно приходиться на понедельник")
        return value


class SourceImportInput(InputModel):
    expectedSourceRevision: int = Field(ge=0, le=2**31-1)
    expectedConfigRevision: int = Field(ge=0, le=2**31-1)
    expectedDraftRevision: int = Field(ge=0, le=2**31-1)
    periodStart: str = Field(pattern=r'^[0-9]{4}-[0-9]{2}-[0-9]{2}$')
    resolutions: dict
    requestId: str | None = Field(default=None, min_length=16,max_length=80,pattern=r'^[A-Za-z0-9_-]+$')
    expectedPackageHash: str | None = Field(default=None,pattern=r'^[a-f0-9]{64}$')
    expectedCalendarHash: str | None = Field(default=None,pattern=r'^[a-f0-9]{64}$')

    @field_validator('periodStart')
    @classmethod
    def monday(cls, value):
        if date.fromisoformat(value).weekday() != 0:
            raise ValueError('Начало недели должно приходиться на понедельник')
        return value


class RunInput(InputModel):
    expectedConfigRevision: int = Field(ge=0, le=2**31 - 1)
    expectedDraftRevision: int = Field(ge=0, le=2**31 - 1)
    mode: Literal["generate", "repair"] = "generate"
    timeLimitSeconds: int = Field(default=10, ge=1, le=30)
    schoolRequestId: str | None = Field(default=None, min_length=1, max_length=80, pattern=r'^sr_[A-Za-z0-9_-]+$')
    periodStart: str | None = Field(default=None, pattern=r'^[0-9]{4}-[0-9]{2}-[0-9]{2}$')
    requestId: str | None = Field(default=None, min_length=16, max_length=80, pattern=r'^[A-Za-z0-9_-]+$')

    @field_validator('periodStart')
    @classmethod
    def target_monday(cls, value):
        if value is not None and date.fromisoformat(value).weekday() != 0:
            raise ValueError('Начало недели должно приходиться на понедельник')
        return value


class AdjustRunInput(InputModel):
    requestId: str = Field(min_length=16, max_length=80, pattern=r'^[A-Za-z0-9_-]+$')
    expectedConfigRevision: int = Field(ge=0, le=2**31-1)
    expectedDraftRevision: int = Field(ge=0, le=2**31-1)
    expectedRunHash: str = Field(pattern=r'^[a-f0-9]{64}$')
    assignments: list[Assignment] = Field(min_length=1, max_length=2000)


class QuestionInput(InputModel):
    expectedConfigRevision: int = Field(ge=0, le=2**31 - 1)


class AnswerInput(InputModel):
    text: str = Field(min_length=1, max_length=2000)

    @field_validator("text")
    @classmethod
    def unicode_text(cls, value):
        if any(unicodedata.category(char) == "Cs" or (unicodedata.category(char) == "Cc" and char not in "\n\r\t") for char in value):
            raise ValueError("Некорректный текст ответа")
        return value


class SubstitutionInput(InputModel):
    lessonId: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    expectedDraftRevision: int = Field(ge=0, le=2**31 - 1)
    configRevision: int = Field(ge=0, le=2**31 - 1)


class EmptyInput(InputModel):
    pass


def template_input(kind="blank"):
    today = datetime.now(ZoneInfo("Europe/Moscow")).date()
    monday = today - timedelta(days=today.weekday())
    blank = {"periodStart": monday.isoformat(), "confirmed": False, "slots": [], "groups": [], "teachers": [], "rooms": [], "demands": [], "baseline": [], "pins": [], "preferences": {}, "notes": ""}
    if kind == "blank":
        return {"input": blank, "notice": "Пустая нормативная модель. Заполните и подтвердите факты; импортированная сетка не определяет ограничения."}
    blank.update(
        slots=[{"id": "s1", "day": 0, "slot": 1, "start": "08:00", "end": "08:40"}, {"id": "s2", "day": 0, "slot": 2, "start": "08:50", "end": "09:30"}],
        groups=[{"id": "g1", "className": "Тест-7А", "name": "Тест-7А целиком", "size": 20, "kind": "whole", "partition": "", "maxDaily": 8, "unavailableSlotIds": []}, {"id": "g2", "className": "Тест-7Б", "name": "Тест-7Б целиком", "size": 18, "kind": "whole", "partition": "", "maxDaily": 8, "unavailableSlotIds": []}],
        teachers=[{"id": "t1", "name": "Тестовый учитель А", "subjects": ["Математика"], "unavailableSlotIds": [], "maxDaily": 4, "maxWeekly": 10, "maxConsecutive": 3}, {"id": "t2", "name": "Тестовый учитель Б", "subjects": ["Математика"], "unavailableSlotIds": [], "maxDaily": 4, "maxWeekly": 10, "maxConsecutive": 3}],
        rooms=[{"id": "r1", "name": "Тестовый кабинет 1", "capacity": 30, "tags": [], "unavailableSlotIds": []}, {"id": "r2", "name": "Тестовый кабинет 2", "capacity": 30, "tags": [], "unavailableSlotIds": []}],
        demands=[{"id": "d1", "subject": "Математика", "groupIds": ["g1"], "teacherIds": ["t1"], "roomIds": ["r1"], "weeklyCount": 1, "requiredTags": [], "allowedSlotIds": [], "maxPerDay": 1, "syncKey": "", "sourceLessonIds": []}, {"id": "d2", "subject": "Математика", "groupIds": ["g2"], "teacherIds": ["t2"], "roomIds": ["r2"], "weeklyCount": 1, "requiredTags": [], "allowedSlotIds": [], "maxPerDay": 1, "syncKey": "", "sourceLessonIds": []}],
        notes="Тестовый пример двух классов на вымышленных данных. Не заменяет школьный план.",
    )
    return {"input": blank, "notice": "Тестовый пример на вымышленных данных. Он не сохраняется и не заменяет школьные данные без вашего действия."}


class PlanningService:
    def __init__(self, path: str | Path | None = None, *, solver=None):
        self.path = Path(path or database_path()).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.solver = solver or solve_plan
        self.futures = []
        with _global_lock:
            self.lock = _locks.setdefault(str(self.path), threading.RLock())
            self.executor = _executors.setdefault(str(self.path), ThreadPoolExecutor(max_workers=1, thread_name_prefix="local-schedule"))
        with closing(self._connect()) as connection:
            connection.executescript("""
            CREATE TABLE IF NOT EXISTS planning_config (
              id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, base_draft_revision INTEGER NOT NULL,
              input_json TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS planning_runs (
              id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, owner_role TEXT NOT NULL,
              mode TEXT NOT NULL, config_revision INTEGER NOT NULL, draft_revision INTEGER NOT NULL,
              input_hash TEXT NOT NULL, input_json TEXT NOT NULL, status TEXT NOT NULL,
              base_snapshot_json TEXT NOT NULL, base_snapshot_hash TEXT NOT NULL,
              result_json TEXT, diff_json TEXT NOT NULL DEFAULT '[]', message TEXT NOT NULL,
              draft_diff_json TEXT NOT NULL DEFAULT '[]', change_summary_json TEXT NOT NULL,
              proposal_task_id TEXT, proposal_hash TEXT, applied_task_id TEXT,
              cancel_requested INTEGER NOT NULL DEFAULT 0, worker_pid INTEGER NOT NULL,
              worker_token TEXT NOT NULL, time_limit INTEGER NOT NULL,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS planning_questions (
              id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, owner_role TEXT NOT NULL,
              config_revision INTEGER NOT NULL, dedup_key TEXT NOT NULL, payload TEXT NOT NULL,
              UNIQUE(owner_id,owner_role,dedup_key));
            CREATE TABLE IF NOT EXISTS planning_run_requests (
              id INTEGER PRIMARY KEY AUTOINCREMENT,owner_id TEXT NOT NULL,owner_role TEXT NOT NULL,created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS planning_run_receipts (
              request_id TEXT NOT NULL,owner_id TEXT NOT NULL,owner_role TEXT NOT NULL,
              fingerprint TEXT NOT NULL,run_id TEXT NOT NULL,created_at TEXT NOT NULL,
              PRIMARY KEY(request_id,owner_id,owner_role));
            CREATE TABLE IF NOT EXISTS planning_source_settings (
              id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL,
              package_id TEXT NOT NULL,resolutions_json TEXT NOT NULL,preview_json TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS planning_source_receipts (
              request_id TEXT NOT NULL,owner_id TEXT NOT NULL,owner_role TEXT NOT NULL,
              fingerprint TEXT NOT NULL,result_json TEXT NOT NULL,
              PRIMARY KEY(request_id,owner_id,owner_role));
            """)
        self.recover_interrupted()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=15000")
        return connection

    @contextmanager
    def _locked(self):
        with self.lock, open(str(self.path) + ".planning.lock", "a+b") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)

    @staticmethod
    def _draft_revision(connection):
        try:
            row = connection.execute("SELECT payload FROM timetable_draft WHERE id=1").fetchone()
        except sqlite3.OperationalError:
            return 0
        return json.loads(row[0])["revision"] if row else 0

    @staticmethod
    def _draft_snapshot(connection):
        try:
            row = connection.execute("SELECT payload FROM timetable_draft WHERE id=1").fetchone()
        except sqlite3.OperationalError:
            row = None
        draft = json.loads(row[0]) if row else {"revision": 0, "lessons": []}
        return {"revision": draft["revision"], "lessons": [{key: lesson[key] for key in LESSON_FIELDS} for lesson in draft["lessons"]]}

    @staticmethod
    def _draft_diff(snapshot, lessons):
        before = {lesson["id"]: {key: lesson[key] for key in LESSON_FIELDS} for lesson in snapshot["lessons"]}
        after = {lesson["id"]: {key: lesson[key] for key in LESSON_FIELDS} for lesson in lessons}
        changes = [{"lessonId": lesson_id, "before": before.get(lesson_id), "after": after.get(lesson_id)} for lesson_id in sorted(before.keys() | after.keys()) if before.get(lesson_id) != after.get(lesson_id)]
        added = sum(item["before"] is None for item in changes)
        removed = sum(item["after"] is None for item in changes)
        changed = len(changes) - added - removed
        return changes, {"added": added, "removed": removed, "changed": changed, "unchanged": len(before) - removed - changed, "totalBefore": len(before), "totalAfter": len(after)}

    @staticmethod
    def _config(connection):
        row = connection.execute("SELECT * FROM planning_config WHERE id=1").fetchone()
        if row is None:
            return {"revision": 0, "baseDraftRevision": 0, "input": None}
        return {"revision": row[1], "baseDraftRevision": row[2], "input": json.loads(row[3])}

    @staticmethod
    def _calendar_effect(connection, profile, plan, exclude_run_id=None, require_configured=None):
        # Legacy demo inputs keep their contract. School identities consume a
        # finite calendar overlay from the same domain transaction.
        if not profile or profile.get('membershipVerified') is not True or not profile.get('schoolId'):
            return None
        from .school_calendar import calendar_effect_for_plan
        exists = table_exists(connection, 'demo_state')
        row = connection.execute('SELECT payload FROM demo_state WHERE id=1').fetchone() if exists else None
        state = json.loads(row[0]) if row else {}
        from .school_plan_imports import runtime_source_provenance
        effect=calendar_effect_for_plan(state, plan, school_id=profile['schoolId'],
            require_configured=bool(profile.get('staffRole')) if require_configured is None else require_configured,
            source_provenance=runtime_source_provenance(connection,plan))
        if effect['ready']:
            from .school_plan_imports import runtime_source_rules
            source_effect = runtime_source_rules(connection,effect['effectiveInput'])
            effect['effectiveInput'] = source_effect['effectiveInput']
            effect['ready'] = source_effect['ready']
            effect['blockers'].extend(source_effect['blockers'])
        if effect['ready']:
            from .request_planning import prior_absence_effect
            try:
                previous=prior_absence_effect(connection,effect['effectiveInput'],profile,exclude_run_id=exclude_run_id)
                effect.update(previous)
            except HTTPException as error:
                if error.status_code!=409:raise
                effect['ready']=False
                effect['blockers'].append(_block('prior_absence_requires_review',str(error.detail)))
        return effect

    @staticmethod
    def _assert_run_calendar(connection, row):
        base = json.loads(row['base_snapshot_json'])
        from .school_plan_imports import source_binding
        if base.get('sourceBinding')!=source_binding(connection):
            raise HTTPException(409,'Школьные источники изменились после расчёта. Требуется новый кандидат.')
        actor_binding = base.get('actorBinding')
        if actor_binding and actor_binding.get('staffRole'):
            from .launch_identity import membership_current
            if not membership_current(actor_binding):
                raise HTTPException(409, 'Доступ составителя расчёта изменился; требуется новое предложение')
        if base.get('priorAbsenceBinding') is not None:
            from .request_planning import prior_absence_effect
            previous=prior_absence_effect(connection,json.loads(row['input_json']),actor_binding,
                exclude_run_id=row['id'] if row['status']=='approved' else None)
            if previous['priorAbsenceBinding']!=base['priorAbsenceBinding']:
                raise HTTPException(409,'Согласованные отсутствия изменились после расчёта. Требуется новый кандидат.')
        binding = base.get('calendarBinding')
        if binding is not None:
            from .school_calendar import assert_calendar_binding_current
            assert_calendar_binding_current(binding, connection=connection)
        elif actor_binding and actor_binding.get('schoolId'):
            from .school_calendar import _read_state
            from .agent_rules import binding as standing_rules_binding
            if standing_rules_binding(_read_state(connection), actor_binding['schoolId']) is not None:
                raise HTTPException(409, 'Постоянные правила изменились. Для этого школьного варианта нужен новый расчёт с привязкой правил.')
        request_binding = base.get('requestBinding')
        if request_binding is not None:
            from .request_planning import assert_binding_current
            assert_binding_current(connection, request_binding, run_id=row['id'],
                allow_applied=row['status'] == 'approved', applied_draft_revision=row['draft_revision']+1)

    @staticmethod
    def _trusted_rebase(connection, config):
        """Only an exactly applied, verified solver result may advance the base."""
        if config['input'] is None:return None
        actual_revision=PlanningService._draft_revision(connection)
        if actual_revision==config['baseDraftRevision']:return None
        stored=connection.execute('SELECT payload FROM timetable_draft WHERE id=1').fetchone()
        if not stored:return None
        actual=json.loads(stored[0])['lessons']
        keys=(*LESSON_FIELDS,'_planningEventId','_planningGroups')
        exact=lambda rows:sorted([{key:row.get(key) for key in keys}for row in rows],key=lambda row:row['id'])
        rows=connection.execute("SELECT * FROM planning_runs WHERE status='approved' AND config_revision=? AND draft_revision=? AND applied_task_id IS NOT NULL AND cancel_requested=0 ORDER BY updated_at DESC",(config['revision'],actual_revision-1)).fetchall()
        for row in rows:
            try:
                verified=PlanningService._verified_materialization(row)
                if exact(verified)!=exact(actual):continue
                snapshot=json.loads(row['input_json'])
                return {'runId':row['id'],'plan':snapshot,'assignments':json.loads(row['result_json'])['assignments']}
            except (HTTPException,ValueError,KeyError,TypeError):continue
        return None

    @staticmethod
    def _rebase_assignments(proof, plan):
        demands={d['id']:d for d in plan['demands']};old={d['id']:d for d in proof['plan']['demands']}
        events={f"{d['id']}_{n}":d for d in plan['demands']for n in range(d['weeklyCount'])}
        slots={s['id']for s in plan['slots']};rooms={r['id']for r in plan['rooms']}
        fields=('subject','groupIds','teacherIds','roomIds')
        return [deepcopy(a)for a in proof['assignments']if a['eventId']in events and a['slotId']in slots and a['roomId']in rooms
            and events[a['eventId']]['id']in old and all(events[a['eventId']][k]==old[events[a['eventId']]['id']][k]for k in fields)]

    @staticmethod
    def _wrapper(connection, profile=None, period_start=None):
        wrapper = PlanningService._config(connection)
        if period_start is not None and wrapper['input'] is not None:
            wrapper['input'] = {**wrapper['input'], 'periodStart':period_start}
        readiness = planning_readiness(wrapper["input"]) if wrapper["input"] is not None else {"ready": False, "blockers": [_block("missing_input", "Нормативный план не заполнен")], "warnings": []}
        raw_blocks = list(readiness['blockers'])
        if wrapper["input"] is not None:
            plan = wrapper["input"]
            groups = {group["id"]: group for group in plan["groups"]}
            projections = sum(demand["weeklyCount"] * len({canonical(groups[group_id]["className"]) for group_id in demand["groupIds"]}) for demand in plan["demands"])
            if projections > 2000:
                readiness = {**readiness, "ready": False, "blockers": [*readiness["blockers"], _block("projection_limit", "Кандидат создаёт более 2000 строк классов. Сократите объём одного расчёта.")]}
            source_ids = {source for demand in plan["demands"] for source in demand.get("sourceLessonIds", [])}
            if source_ids:
                try:
                    draft_row = connection.execute("SELECT payload FROM timetable_draft WHERE id=1").fetchone()
                except sqlite3.OperationalError:
                    draft_row = None
                actual = {lesson["id"]: lesson for lesson in json.loads(draft_row[0])["lessons"]} if draft_row else {}
                if source_ids - actual.keys():
                    readiness = {**readiness, "ready": False, "blockers": [*readiness["blockers"], _block("unknown_source_lessons", "Сопоставления sourceLessonIds содержат отсутствующие в черновике уроки.", sorted(source_ids - actual.keys()))]}
                wrong_class = [source_id for demand in plan["demands"] for source_id in demand.get("sourceLessonIds", []) if source_id in actual and canonical(actual[source_id]["className"]) not in {canonical(groups[group_id]["className"]) for group_id in demand["groupIds"]}]
                if wrong_class:
                    readiness = {**readiness, "ready": False, "blockers": [*readiness["blockers"], _block("source_class_mismatch", "Класс исходного урока не совпадает с подтверждёнными группами нагрузки.", sorted(wrong_class))]}
        rebase = PlanningService._trusted_rebase(connection,wrapper)
        wrapper['rebasedBase'] = rebase is not None
        if wrapper["input"] is not None and wrapper["baseDraftRevision"] != PlanningService._draft_revision(connection) and rebase is None:
            readiness = {**readiness, "ready": False, "blockers": [*readiness["blockers"], _block("draft_changed", "Черновик изменился. Проверьте ограничения и сохраните их для текущей ревизии.")]}
        effect = PlanningService._calendar_effect(connection, profile, wrapper['input']) if wrapper['input'] is not None else None
        if effect is not None:
            effective_readiness = planning_readiness(effect['effectiveInput'])
            extra_blocks = [item for item in readiness['blockers'] if item not in raw_blocks]
            wrapper.update(effectiveInput=effect['effectiveInput'], calendarBinding=effect['calendarBinding'])
            if effect.get('priorAbsenceBinding') is not None:
                wrapper['priorAbsenceBinding']=effect['priorAbsenceBinding']
            readiness = {**readiness, 'ready': effective_readiness['ready'] and not extra_blocks and effect['ready'],
                         'blockers': [*effective_readiness['blockers'], *extra_blocks, *effect['blockers']],
                         'warnings': [*readiness['warnings'], *effect.get('diagnostics',[])]}
        if rebase is not None:
            effective=deepcopy(wrapper.get('effectiveInput',wrapper['input']))
            effective['baseline']=PlanningService._rebase_assignments(rebase,effective)
            wrapper['effectiveInput']=effective
            readiness['warnings'].append(_block('verified_baseline_rebased','Основа взята из последнего точно применённого расчёта; неизменные занятия сохраняются.'))
        from .school_plan_imports import source_readiness
        source_blocks = source_readiness(connection, wrapper['input'])
        if source_blocks:
            readiness = {**readiness, 'ready':False, 'blockers':[*readiness['blockers'], *source_blocks]}
        return {**wrapper, "readiness": readiness}

    def read_config(self, profile, period_start=None):
        _actor(profile)
        with closing(self._connect()) as connection:
            return self._wrapper(connection, profile, period_start)

    def save_config(self, body: ConfigInput | dict, profile):
        _actor(profile)
        body = ConfigInput.model_validate(body)
        with self._locked(), closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                current = self._config(connection)
                if current["revision"] != body.expectedRevision or self._draft_revision(connection) != body.baseDraftRevision:
                    raise HTTPException(409, "Настройки или черновик изменились. Обновите данные, сохранив свои правки.")
                if current["revision"] >= 2**31 - 1:
                    raise HTTPException(409, "Достигнут предел ревизий настроек")
                connection.execute("INSERT INTO planning_config VALUES(1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,base_draft_revision=excluded.base_draft_revision,input_json=excluded.input_json,updated_at=excluded.updated_at",
                                   (current["revision"] + 1, body.baseDraftRevision, _json(body.input.model_dump()), _utc()))
                connection.execute("UPDATE planning_runs SET status='stale',message=?,updated_at=? WHERE status IN ('optimal','feasible')", ("Настройки изменились после расчёта; требуется новый расчёт.", _utc()))
                connection.commit()
                return self._wrapper(connection, profile)
            except BaseException:
                connection.rollback()
                raise

    def from_draft(self, body: FromDraftInput | dict, profile):
        _actor(profile)
        body = FromDraftInput.model_validate(body)
        with closing(self._connect()) as connection:
            if self._config(connection)["revision"] != body.expectedRevision or self._draft_revision(connection) != body.expectedDraftRevision:
                raise HTTPException(409, "Настройки или черновик изменились")
            saved = self._config(connection)['input']
            snapshot = self._draft_snapshot(connection)
        candidate = deepcopy(saved) if saved else template_input()["input"]
        candidate["periodStart"] = body.periodStart
        candidate['confirmed'] = False
        # Facts visible in rows are observations, never qualifications, capacity,
        # group membership or a normative weekly load inferred from counts.
        observed = [{key: deepcopy(row[key]) for key in LESSON_FIELDS} for row in snapshot['lessons']]
        mapping_blockers = []
        baseline = []
        if saved:
            rows = {row['id']:row for row in observed}
            teachers = {t['id']:canonical(t['name']) for t in candidate['teachers']}
            groups = {g['id']:g for g in candidate['groups']}
            rooms = {r['id']:canonical(r['name']) for r in candidate['rooms']}
            for demand in candidate['demands']:
                sources = demand.get('sourceLessonIds', [])
                classes = {canonical(groups[g]['className']) for g in demand['groupIds']}
                for occurrence in range(demand['weeklyCount']):
                    source = rows.get(sources[occurrence]) if len(sources) == demand['weeklyCount'] else None
                    slots = [s for s in candidate['slots'] if source and all(source[k] == s[k] for k in ('day','slot','start','end'))]
                    room_ids = [rid for rid in demand['roomIds'] if source and len(source['rooms']) == 1 and rooms[rid] == canonical(source['rooms'][0])]
                    if (not source or len(classes) != 1 or canonical(source['className']) not in classes
                            or canonical(source['subject']) != canonical(demand['subject'])
                            or {canonical(t) for t in source['teachers']} != {teachers[t] for t in demand['teacherIds']}
                            or len(slots) != 1 or len(room_ids) != 1):
                        mapping_blockers.append(_block('source_mapping_required', 'Нужно точное сопоставление события и исходного занятия; совместность и кабинеты не угадываются.', [demand['id']]))
                        continue
                    baseline.append({'eventId':f"{demand['id']}_{occurrence}", 'slotId':slots[0]['id'], 'roomId':room_ids[0]})
            # Partial observations must not silently replace a complete explicit
            # baseline. Keep the prior facts and report unresolved mappings.
            if not mapping_blockers: candidate['baseline'] = baseline
        candidate = PlanningInput.model_validate(candidate).model_dump()
        readiness = planning_readiness(candidate)
        return {"input": candidate, 'observedLessons':observed, 'observedDraftRevision':snapshot['revision'], 'baselineMapped':bool(saved and not mapping_blockers),
                "blockers": [*readiness["blockers"], *mapping_blockers, _block("grid_is_not_plan", "Исходные занятия показаны как наблюдения. Они не подтверждают квалификации, вместимость, нагрузку и связи групп.")], "warnings": [],
                "notice": "Кандидат не сохранён и не подтверждён. Основа сопоставлена только по ранее явно заданным sourceLessonIds и точным полям; неизвестные условия не заполнены."}

    def recover_interrupted(self):
        with self._locked(), closing(self._connect()) as connection:
            rows = connection.execute("SELECT id,worker_pid,worker_token,time_limit,created_at FROM planning_runs WHERE status IN ('queued','running')").fetchall()
            for row in rows:
                if row["worker_pid"] == os.getpid() and row["worker_token"] == PROCESS_TOKEN:
                    continue
                try:
                    created = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
                    expired = (datetime.now(timezone.utc) - created).total_seconds() > row["time_limit"] + 60
                except (ValueError, TypeError):
                    expired = True
                alive = True
                try:
                    os.kill(row["worker_pid"], 0)
                except (ProcessLookupError, ValueError, OverflowError):
                    alive = False
                except PermissionError:
                    pass
                if not alive or row["worker_pid"] == os.getpid() or expired:
                    connection.execute("UPDATE planning_runs SET status='interrupted',cancel_requested=1,message=?,updated_at=? WHERE id=? AND status IN ('queued','running')", ("Процесс расчёта прерван. Кандидат не применён; запустите новый расчёт.", _utc(), row["id"]))

    @staticmethod
    def _row(connection, run_id, actor):
        cursor = connection.execute("SELECT * FROM planning_runs WHERE id=?", (run_id,))
        row = cursor.fetchone()
        if row is not None and not isinstance(row, sqlite3.Row):
            row = dict(zip((column[0] for column in cursor.description), row))
        if row is None or (row["owner_id"], row["owner_role"]) != actor:
            raise HTTPException(404, "Расчёт не найден")
        return row

    @staticmethod
    def _run_public(row):
        result = {"id": row["id"], "status": "interrupted" if row["cancel_requested"] and row["status"] in ACTIVE else row["status"],
                  "mode": row["mode"], "configRevision": row["config_revision"], "draftRevision": row["draft_revision"],
                  "message": row["message"], "result": json.loads(row["result_json"]) if row["result_json"] else None,
                  "diff": json.loads(row["diff_json"]), "draftDiff": json.loads(row["draft_diff_json"]), "changeSummary": json.loads(row["change_summary_json"]), "createdAt": row["created_at"], "updatedAt": row["updated_at"]}
        if row["proposal_task_id"]:
            result["proposalTaskId"] = row["proposal_task_id"]
        binding = json.loads(row['base_snapshot_json']).get('requestBinding')
        if binding is not None:
            result.update(schoolRequestId=binding['requestId'], schoolRequestRevision=binding['requestRevision'],
                          targetWeekStart=json.loads(row['input_json'])['periodStart'])
        return result

    def list_runs(self, profile):
        actor = _actor(profile)
        self.recover_interrupted()
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT * FROM planning_runs WHERE owner_id=? AND owner_role=? ORDER BY created_at DESC LIMIT 10", actor).fetchall()
            return {"runs": [self._visible_run(connection, row) for row in rows]}

    def _visible_run(self, connection, row):
        public = self._run_public(row)
        public['effectiveInput'] = json.loads(row['input_json'])
        public['periodStart'] = json.loads(row['input_json'])['periodStart']
        public['currentDraftRevision'] = self._draft_revision(connection)
        public['canAdjust'] = False
        if row['result_json']:
            public['runHash'] = self._run_hash(row)
            try:
                self._adjustment_base(connection, row)
                public['canAdjust'] = True
            except HTTPException:
                pass
        if row["status"] in CANDIDATES and (row["config_revision"] != self._config(connection)["revision"] or row["draft_revision"] != self._draft_revision(connection)):
            public.update(status="stale", message="Настройки или черновик изменились после расчёта. Требуется новый кандидат.")
        if row['status'] in CANDIDATES:
            try:
                self._assert_run_calendar(connection, row)
            except HTTPException as error:
                if error.status_code != 409:
                    raise
                public.update(status='stale', message='Календарь, заявка или доступ изменились после расчёта. Требуется новый кандидат.')
        return public

    def get_run(self, run_id, profile):
        actor = _actor(profile)
        self.recover_interrupted()
        with closing(self._connect()) as connection:
            row = self._row(connection, run_id, actor)
            return self._visible_run(connection, row)

    def create_run(self, body: RunInput | dict, profile, *, standing_rule_guard=None):
        actor = _actor(profile)
        body = RunInput.model_validate(body)
        if body.schoolRequestId is not None:
            if body.mode != 'repair':
                raise HTTPException(409, 'Подбор по школьной заявке доступен только в режиме repair')
            from .request_planning import _manager
            _manager(profile)
        self.recover_interrupted()
        with self._locked(), closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                fingerprint = _hash(body.model_dump())
                if body.requestId:
                    receipt = connection.execute('SELECT fingerprint,run_id FROM planning_run_receipts WHERE request_id=? AND owner_id=? AND owner_role=?', (body.requestId,*actor)).fetchone()
                    if receipt:
                        if receipt['fingerprint'] != fingerprint:
                            raise HTTPException(409, 'Этот идентификатор расчёта уже использован с другими условиями')
                        result = self._visible_run(connection, self._row(connection, receipt['run_id'], actor))
                        connection.commit()
                        return result
                if standing_rule_guard is not None:
                    from .school_calendar import _read_state
                    from .agent_rules import assert_dispatch_context
                    assert_dispatch_context(_read_state(connection), standing_rule_guard['proposal'],
                        profile, standing_rule_guard['taskId'])
                wrapper = self._wrapper(connection, profile, body.periodStart)
                if wrapper["revision"] != body.expectedConfigRevision or self._draft_revision(connection) != body.expectedDraftRevision:
                    raise HTTPException(409, "Настройки или черновик изменились")
                if not wrapper["readiness"]["ready"]:
                    raise HTTPException(409, {'message':'План не готов: проверьте условия выбранной недели.', 'issues':wrapper['readiness']['blockers']})
                if connection.execute("SELECT 1 FROM planning_runs WHERE status IN ('queued','running') LIMIT 1").fetchone():
                    raise HTTPException(409, "Локальный решатель уже занят. Дождитесь завершения или отмените расчёт.")
                # A lease may expire while an old process is still computing.
                # This independent OS lock prevents overlapping actual workers.
                with open(str(self.path) + ".solver.lock", "a+b") as worker_lock:
                    try:
                        fcntl.flock(worker_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        raise HTTPException(409, "Предыдущий локальный рабочий процесс ещё завершается") from None
                    finally:
                        fcntl.flock(worker_lock, fcntl.LOCK_UN)
                connection.execute("DELETE FROM planning_run_requests WHERE created_at<?", (time.time() - 60,))
                if connection.execute("SELECT COUNT(*) FROM planning_run_requests WHERE owner_id=? AND owner_role=?", actor).fetchone()[0] >= 10:
                    raise HTTPException(429, "Не более десяти расчётов за минуту")
                if connection.execute("SELECT COUNT(*) FROM planning_run_requests").fetchone()[0] >= 200:
                    raise HTTPException(429, "Локальный предел запросов на расчёт; повторите через минуту")
                # Never evict a candidate that is still awaiting a human decision.
                self._reserve_run_slot(connection)
                run_id, stamp = "run_" + secrets.token_urlsafe(24), _utc()
                snapshot = wrapper.get('effectiveInput', wrapper["input"])
                base_snapshot = self._draft_snapshot(connection)
                from .school_plan_imports import source_binding
                binding=source_binding(connection)
                if binding is not None:base_snapshot['sourceBinding']=binding
                if wrapper.get('calendarBinding') is not None:
                    # Existing baseSnapshotHash and proposal hash cover this
                    # binding without changing the legacy DB schema.
                    base_snapshot['calendarBinding'] = wrapper['calendarBinding']
                    from .request_planning import canonical_manager
                    base_snapshot['actorBinding'] = {**canonical_manager(profile), 'domainRole':profile['role']}
                if wrapper.get('priorAbsenceBinding') is not None:
                    base_snapshot['priorAbsenceBinding']=wrapper['priorAbsenceBinding']
                if body.schoolRequestId is not None:
                    from .request_planning import build_effect
                    effect = build_effect(connection, body.schoolRequestId, snapshot, base_snapshot, profile)
                    snapshot = effect['effectiveInput']
                    base_snapshot['requestBinding'] = effect['requestBinding']
                    if effect.get('materializationRunId') is not None:
                        base_snapshot['materializationRunId']=effect['materializationRunId']
                connection.execute("""INSERT INTO planning_runs(id,owner_id,owner_role,mode,config_revision,draft_revision,input_hash,input_json,status,base_snapshot_json,base_snapshot_hash,change_summary_json,message,worker_pid,worker_token,time_limit,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (run_id, *actor, body.mode, wrapper["revision"], body.expectedDraftRevision, _hash(snapshot), _json(snapshot), "queued", _json(base_snapshot), _hash(base_snapshot), _json(EMPTY_SUMMARY), "Расчёт поставлен в локальную очередь.", os.getpid(), PROCESS_TOKEN, body.timeLimitSeconds, stamp, stamp))
                connection.execute("INSERT INTO planning_run_requests(owner_id,owner_role,created_at) VALUES(?,?,?)", (*actor, time.time()))
                if body.requestId:
                    connection.execute('INSERT INTO planning_run_receipts VALUES(?,?,?,?,?,?)', (body.requestId,*actor,fingerprint,run_id,stamp))
                connection.commit()
                result = self._run_public(self._row(connection, run_id, actor))
            except BaseException:
                connection.rollback()
                raise
        self.futures = [future for future in self.futures if not future.done()]
        self.futures.append(self.executor.submit(self._execute, run_id))
        return result

    def _execute(self, run_id):
        with open(str(self.path) + ".solver.lock", "a+b") as worker_lock:
            fcntl.flock(worker_lock, fcntl.LOCK_EX)
            try:
                self._execute_serial(run_id)
            finally:
                fcntl.flock(worker_lock, fcntl.LOCK_UN)

    def _execute_serial(self, run_id):
        with self._locked(), closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM planning_runs WHERE id=?", (run_id,)).fetchone()
            if row is None or row["status"] not in ACTIVE:
                return
            if row["cancel_requested"]:
                connection.execute("UPDATE planning_runs SET status='interrupted',updated_at=? WHERE id=?", (_utc(), run_id))
                return
            connection.execute("UPDATE planning_runs SET status='running',message=?,updated_at=? WHERE id=?", ("Локальный CP-SAT выполняет расчёт. Процент готовности неизвестен.", _utc(), run_id))
            snapshot, mode, limit = json.loads(row["input_json"]), row["mode"], row["time_limit"]
        start = time.monotonic()
        try:
            result = self.solver(snapshot, mode=mode, time_limit=limit)
            if not isinstance(result, dict) or result.get("status") not in {"optimal", "feasible", "infeasible", "unknown", "invalid"}:
                raise ValueError("Invalid solver result")
            result = _verify_run_result(snapshot, result)
            result.setdefault("metrics", {})["serviceElapsedSeconds"] = round(time.monotonic() - start, 6)
            _json(result)
            status = result["status"]
            messages = {"optimal": "Найден оптимальный кандидат. Требуется проверка и подтверждение.", "feasible": "Найден допустимый кандидат; оптимальность не доказана. Требуется подтверждение.", "infeasible": "При заданных жёстких ограничениях допустимое расписание не найдено (доказано решателем).", "unknown": "Решатель не установил результат за отведённое время. Это не доказательство невозможности.", "invalid": "Результат не прошёл независимую проверку и не может быть применён."}
            message = messages[status]
            before = {item["eventId"]: item for item in snapshot.get("baseline", [])}
            after = {item["eventId"]: item for item in result.get("assignments", [])}
            diff = [{"eventId": event_id, "before": before.get(event_id), "after": after.get(event_id)} for event_id in sorted(before.keys() | after.keys()) if before.get(event_id) != after.get(event_id)]
            if status in CANDIDATES:
                materialized = self.materialize({**dict(row), "result_json": _json(result)})
                draft_diff, summary = self._draft_diff(json.loads(row["base_snapshot_json"]), materialized)
            else:
                draft_diff, summary = [], EMPTY_SUMMARY
        except Exception:
            status, result, diff, message = "failed", None, [], "Локальный расчёт завершился ошибкой. Черновик не изменён; проверьте входные данные и повторите."
            draft_diff, summary = [], EMPTY_SUMMARY
        with self._locked(), closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM planning_runs WHERE id=?", (run_id,)).fetchone()
            if row is None or row["status"] not in ACTIVE:
                return
            if row["cancel_requested"]:
                status, result, diff, message = "interrupted", None, [], "Расчёт отменён. Поздний результат отброшен; кандидат не применялся."
                draft_diff, summary = [], EMPTY_SUMMARY
            elif self._config(connection)["revision"] != row["config_revision"] or self._draft_revision(connection) != row["draft_revision"]:
                status, message = "stale", "Настройки или черновик изменились во время расчёта. Кандидат применять нельзя."
            connection.execute("UPDATE planning_runs SET status=?,result_json=?,diff_json=?,draft_diff_json=?,change_summary_json=?,message=?,updated_at=? WHERE id=?", (status, _json(result) if result is not None else None, _json(diff), _json(draft_diff), _json(summary), message, _utc(), run_id))

    def cancel_run(self, run_id, profile):
        actor = _actor(profile)
        with self._locked(), closing(self._connect()) as connection:
            row = self._row(connection, run_id, actor)
            if row["status"] == "approved":
                raise HTTPException(409, "Подтверждённый расчёт уже применён")
            if row["status"] not in {"rejected", "interrupted"}:
                # Keep the internal active status until bounded solver execution
                # ends; this prevents starting a second simultaneous worker.
                next_status = row["status"] if row["status"] in ACTIVE else "interrupted"
                connection.execute("UPDATE planning_runs SET status=?,cancel_requested=1,message=?,updated_at=? WHERE id=?", (next_status, "Расчёт отменён. Результат не может быть подтверждён.", _utc(), run_id))
            return self._run_public(self._row(connection, run_id, actor))

    @staticmethod
    def _run_hash(row):
        return _hash({"runId": row["id"], "owner": [row["owner_id"], row["owner_role"]], "configRevision": row["config_revision"], "draftRevision": row["draft_revision"], "inputHash": row["input_hash"], "baseSnapshotHash": row["base_snapshot_hash"], "draftDiff": json.loads(row["draft_diff_json"]), "changeSummary": json.loads(row["change_summary_json"]), "result": json.loads(row["result_json"])})

    def adjust_run(self, run_id, body: AdjustRunInput | dict, profile):
        actor = _actor(profile)
        body = AdjustRunInput.model_validate(body)
        if profile.get('schoolId'):
            from .request_planning import canonical_manager
            canonical_manager(profile)
        fingerprint = _hash({'runId':run_id, 'body':body.model_dump(), 'actor':list(actor), 'schoolId':profile.get('schoolId')})
        derived_id = 'run_' + _hash({'actor':list(actor), 'schoolId':profile.get('schoolId'), 'requestId':body.requestId})[:48]
        with self._locked(), closing(self._connect()) as connection:
            try:
                connection.execute('BEGIN IMMEDIATE')
                previous = connection.execute('SELECT * FROM planning_runs WHERE id=?', (derived_id,)).fetchone()
                if previous:
                    self._row(connection, derived_id, actor)
                    if json.loads(previous['base_snapshot_json']).get('adjustmentFingerprint') != fingerprint:
                        raise HTTPException(409, 'Идентификатор перестановки уже использован иначе')
                    connection.commit()
                    return self._visible_run(connection, previous)
                row = self._row(connection, run_id, actor)
                self._assert_run_calendar(connection, row)
                if (self._config(connection)['revision'] != body.expectedConfigRevision
                        or row['config_revision'] != body.expectedConfigRevision
                        or self._draft_revision(connection) != body.expectedDraftRevision
                        or self._run_hash(row) != body.expectedRunHash):
                    raise HTTPException(409, 'Кандидат, условия или черновик изменились; обновите расчёт')
                base = self._adjustment_base(connection, row)
                configured = self._config(connection)['input']
                configured = {**configured,'periodStart':json.loads(row['input_json'])['periodStart']}
                effect = self._calendar_effect(connection, profile, configured)
                effective = effect['effectiveInput'] if effect else configured
                effective['baseline']=json.loads(row['input_json'])['baseline']
                if (effect and not effect['ready']) or _hash(effective) != row['input_hash']:
                    raise HTTPException(409, 'Подтверждённые условия отличаются от условий кандидата')
                self._reserve_run_slot(connection, keep_ids=(run_id,))
                assignments = [a.model_dump() for a in body.assignments]
                snapshot = json.loads(row['input_json'])
                validation = validate_solution(snapshot, assignments)
                if not validation['valid']:
                    raise HTTPException(422, {'message':'Перестановки нарушают подтверждённые ограничения. Кандидат не сохранён.', 'issues':validation['issues']})
                base.update(adjustmentFingerprint=fingerprint, adjustmentSourceRunId=run_id,
                    materializationRunId=base.get('materializationRunId', run_id))
                result = {'status':'feasible', 'assignments':assignments, 'validation':validation, 'diagnostics':[],
                    'metrics':{'manualAdjusted':True, 'optimalityProven':False, 'eventCount':len(assignments)}}
                previous_result = json.loads(row['result_json'])
                if previous_result.get('control') is not None:
                    result['control'] = deepcopy(previous_result['control'])
                result = _verify_run_result(snapshot, result)
                if result['status'] not in CANDIDATES:
                    raise HTTPException(422, 'Контрольный вариант требует перепроверки; перестановки не сохранены.')
                derived = dict(row)
                derived.update(id=derived_id, result_json=_json(result), base_snapshot_json=_json(base), base_snapshot_hash=_hash(base))
                materialized = self.materialize(derived)
                draft_diff, summary = self._draft_diff(base, materialized)
                baseline = {a['eventId']:a for a in snapshot['baseline']}
                diff = [{'eventId':a['eventId'], 'before':baseline.get(a['eventId']), 'after':a} for a in assignments if a != baseline.get(a['eventId'])]
                stamp = _utc()
                connection.execute('''INSERT INTO planning_runs(id,owner_id,owner_role,mode,config_revision,draft_revision,input_hash,input_json,status,base_snapshot_json,base_snapshot_hash,result_json,diff_json,draft_diff_json,change_summary_json,message,worker_pid,worker_token,time_limit,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (derived_id,*actor,row['mode'],row['config_revision'],base['revision'],row['input_hash'],row['input_json'],'feasible',_json(base),_hash(base),_json(result),_json(diff),_json(draft_diff),_json(summary),
                     'Ручные перестановки независимо проверены. Это допустимый кандидат без доказательства оптимальности; требуется просмотр и отдельное согласование.',os.getpid(),PROCESS_TOKEN,0,stamp,stamp))
                connection.commit()
                return self._visible_run(connection, self._row(connection,derived_id,actor))
            except BaseException:
                connection.rollback()
                raise

    @staticmethod
    def _reserve_run_slot(connection, *, keep_ids=()):
        # Same bounded history for computed and manually adjusted candidates.
        # Never evict their source or any active/pending human decision. Exact
        # command recovery is checked before retention can remove old history.
        while connection.execute('SELECT COUNT(*) FROM planning_runs').fetchone()[0] >= RUN_LIMIT:
            retained = set(keep_ids)
            # The applied grid remains the authoritative base for the next
            # week and for an absence on a generated event. Never evict it
            # while it is still the actual draft, nor a live repair's source.
            revision=PlanningService._draft_revision(connection)
            retained.update(row['id']for row in connection.execute("SELECT id FROM planning_runs WHERE status='approved' AND draft_revision=?",(revision-1,)))
            for item in connection.execute("SELECT base_snapshot_json FROM planning_runs WHERE status IN ('queued','running','optimal','feasible')"):
                proof=json.loads(item[0]).get('requestBinding',{}).get('sourceMappingProof')
                if proof and proof.get('runId'):retained.add(proof['runId'])
            completed = connection.execute("""SELECT id FROM planning_runs
                WHERE status NOT IN ('queued','running','optimal','feasible')
                AND (proposal_task_id IS NULL OR applied_task_id IS NOT NULL OR status='rejected')
                ORDER BY created_at,id""").fetchall()
            oldest = next((item['id'] for item in completed if item['id'] not in retained), None)
            if oldest is None:
                raise HTTPException(429, 'Завершите проверку накопленных кандидатов; текущие предложения сохранены')
            connection.execute('DELETE FROM planning_runs WHERE id=?', (oldest,))

    def _adjustment_base(self, connection, row):
        """Retune only an exact current ordinary candidate, keeping its event seed.

        An applied run is historical evidence, never edited in place. Its new
        candidate is based on the actual committed revision and must be approved
        separately. Request overlays keep their dedicated freshness lifecycle.
        """
        base = json.loads(row['base_snapshot_json'])
        if (base.get('requestBinding') is not None or row['cancel_requested']
                or self._config(connection)['revision'] != row['config_revision']):
            raise HTTPException(409, 'Этот расчёт нельзя использовать для свободных перестановок')
        self._assert_run_calendar(connection, row)
        lessons = self._verified_materialization(row)
        revision = self._draft_revision(connection)
        if row['status'] in CANDIDATES:
            if row['proposal_task_id'] or revision != row['draft_revision']:
                raise HTTPException(409, 'Кандидат устарел или уже передан на согласование')
            return base
        if row['status'] != 'approved' or not row['applied_task_id'] or revision != row['draft_revision'] + 1:
            raise HTTPException(409, 'Нужна текущая версия согласованного черновика')
        stored = connection.execute('SELECT payload FROM timetable_draft WHERE id=1').fetchone()
        actual = json.loads(stored[0])['lessons'] if stored else []
        keys = (*LESSON_FIELDS, '_planningEventId', '_planningGroups')
        exact = lambda rows: sorted([{k: r.get(k) for k in keys} for r in rows], key=lambda r:r['id'])
        if exact(actual) != exact(lessons):
            raise HTTPException(409, 'Черновик или сведения о связанных группах отличаются от согласованного расчёта')
        # Preserve calendar/actor provenance and the original event seed, while
        # replacing the old before-grid with this actual revision's full grid.
        return {**base, **self._draft_snapshot(connection)}

    @staticmethod
    def _verified_materialization(row):
        snapshot, result, base = json.loads(row["input_json"]), json.loads(row["result_json"]), json.loads(row["base_snapshot_json"])
        if (_hash(snapshot) != row["input_hash"] or _hash(base) != row["base_snapshot_hash"] or base["revision"] != row["draft_revision"]
                or not validate_solution(snapshot, result["assignments"])["valid"]):
            raise HTTPException(409, "Сохранённый кандидат или исходный снимок не прошёл проверку")
        lessons = PlanningService.materialize(row)
        diff, summary = PlanningService._draft_diff(base, lessons)
        if diff != json.loads(row["draft_diff_json"]) or summary != json.loads(row["change_summary_json"]):
            raise HTTPException(409, "Полный список изменений отличается от сохранённого кандидата")
        return lessons

    def prepare_run_proposal(self, run_id, profile):
        actor = _actor(profile)
        with closing(self._connect()) as connection:
            row = self._row(connection, run_id, actor)
            self._assert_run_calendar(connection, row)
            if row["cancel_requested"] or row["status"] not in CANDIDATES:
                raise HTTPException(409, "Расчёт не содержит доступного для подтверждения кандидата")
            if self._config(connection)["revision"] != row["config_revision"] or self._draft_revision(connection) != row["draft_revision"]:
                raise HTTPException(409, "Расчёт устарел; запустите новый")
            self._verified_materialization(row)
            return {"kind": "planning", "runId": run_id, "expectedRevision": row["draft_revision"], "configRevision": row["config_revision"], "hash": self._run_hash(row)}

    def propose_run(self, run_id, profile):
        actor = _actor(profile)
        from .schedule_agent import get_runtime
        runtime = get_runtime()
        with self._locked(), closing(self._connect()) as connection:
            row = self._row(connection, run_id, actor)
            if row["cancel_requested"]:
                raise HTTPException(409, "Расчёт отменён и не может быть предложен к подтверждению")
            existing = row["proposal_task_id"]
            if existing:
                task_id = existing
            else:
                proposal = self.prepare_run_proposal(run_id, profile)
                task_id = "agt_" + secrets.token_urlsafe(24)
                connection.execute("UPDATE planning_runs SET proposal_task_id=?,proposal_hash=?,updated_at=? WHERE id=?", (task_id, proposal["hash"], _utc(), run_id))
        if existing:
            try:
                return runtime.get_task(task_id, profile)
            except HTTPException as error:
                if error.status_code != 404:
                    raise
        proposal = self.prepare_run_proposal(run_id, profile)
        return runtime.create_task({"message": "Проверить и подтвердить рассчитанное расписание", "proposal": proposal}, profile, task_id=task_id)

    @staticmethod
    def materialize(row):
        snapshot, result = json.loads(row["input_json"]), json.loads(row["result_json"])
        base = json.loads(row['base_snapshot_json']) if 'base_snapshot_json' in row.keys() and row['base_snapshot_json'] else {}
        seed = base.get('materializationRunId', row['id'])
        if not isinstance(seed, str) or not 1 <= len(seed) <= 80:
            raise HTTPException(409, 'Не подтверждено происхождение размещений кандидата')
        groups = {item["id"]: item for item in snapshot["groups"]}
        teachers = {item["id"]: item for item in snapshot["teachers"]}
        rooms = {item["id"]: item for item in snapshot["rooms"]}
        slots = {item["id"]: item for item in snapshot["slots"]}
        events = {f"{demand['id']}_{occurrence}": (demand, occurrence) for demand in snapshot["demands"] for occurrence in range(demand["weeklyCount"])}
        lessons = []
        for assignment in sorted(result["assignments"], key=lambda item: item["eventId"]):
            demand, occurrence = events[assignment["eventId"]]
            event_groups = [groups[group_id] for group_id in demand["groupIds"]]
            classes = sorted({canonical(group["className"]): group["className"] for group in event_groups}.values())
            slot, room = slots[assignment["slotId"]], rooms[assignment["roomId"]]
            names = [teachers[teacher_id]["name"] for teacher_id in demand["teacherIds"]]
            event_key = "pe_" + hashlib.sha256((seed + "|" + assignment["eventId"]).encode()).hexdigest()[:24]
            for class_name in classes:
                source_ids = demand.get("sourceLessonIds", [])
                lesson_id = source_ids[occurrence] if len(classes) == 1 and source_ids else "pl_" + hashlib.sha256((event_key + "|" + class_name).encode()).hexdigest()[:24]
                lessons.append({"id": lesson_id, "className": class_name, "day": slot["day"], "slot": slot["slot"], "start": slot["start"], "end": slot["end"], "subject": demand["subject"], "teachers": names, "rooms": [room["name"]], "edited": True,
                                "raw": demand["subject"] + " (" + room["name"] + ")\n" + ", ".join(names),
                                "sourceCell": "Локальный расчёт!" + assignment["eventId"], "_planningEventId": event_key,
                                "_planningGroups": [{key: group[key] for key in ("id", "className", "kind", "partition")} for group in event_groups if canonical(group["className"]) == canonical(class_name)]})
        if len(lessons) > 2000 or len({lesson["id"] for lesson in lessons}) != len(lessons):
            raise HTTPException(409, "Материализация превышает лимит строк или содержит повторные идентификаторы")
        return lessons

    def apply_run_proposal(self, proposal, profile, idempotency_key, expected_hash):
        actor = _actor(profile)
        from .timetable import apply_batch, batch_hash
        with self._locked(), closing(self._connect()) as connection:
            row = self._row(connection, proposal["runId"], actor)
            digest = self._run_hash(row) if row["result_json"] else ""
            if (proposal.get("kind") != "planning" or proposal.get("hash") != digest or expected_hash != digest
                    or proposal.get("expectedRevision") != row["draft_revision"] or proposal.get("configRevision") != row["config_revision"]
                    or row["proposal_task_id"] != idempotency_key or row["cancel_requested"]
                    or row["status"] not in CANDIDATES | {"approved"}):
                raise HTTPException(409, "Кандидат или подтверждение изменены, отменены либо недоступны")
            lessons = self._verified_materialization(row)
            source_name = "Автоматический расчёт"
            command_hash = batch_hash(lessons, row["draft_revision"], source_name)
            request_binding = json.loads(row['base_snapshot_json']).get('requestBinding')
            if request_binding is not None:
                # Recover the fact of an already-committed exact approval before
                # testing whether its old inputs could authorize a NEW mutation.
                # No stale owner/date/publication can undo this historical fact.
                from .request_planning import _manager
                _manager(profile)  # current canonical manager + explicit host school
                exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='timetable_command_receipts'").fetchone()
                receipt = connection.execute('SELECT command_hash,actor_id,actor_role,result FROM timetable_command_receipts WHERE command_key=?',
                    (idempotency_key,)).fetchone() if exists else None
                if receipt is not None:
                    if (tuple(receipt[:3]) != (command_hash, *actor) or row['status'] != 'approved'
                            or row['applied_task_id'] != idempotency_key or request_binding['schoolId'] != profile['schoolId']):
                        raise HTTPException(409, 'Квитанция не подтверждает точное применение этого расчёта')
                    recovered = json.loads(receipt['result'])
                    diff, _ = self._draft_diff({'lessons': lessons}, recovered['lessons'])
                    if recovered['revision'] != row['draft_revision']+1 or diff:
                        raise HTTPException(409, 'Сохранённая квитанция отличается от применённого кандидата')
                    return recovered
            self._assert_run_calendar(connection, row)

            def guard(transaction):
                self._assert_run_calendar(transaction, row)
                config = self._config(transaction)
                fresh = self._row(transaction, row["id"], actor)
                if config["revision"] != row["config_revision"] or fresh["status"] not in CANDIDATES or fresh["cancel_requested"] or fresh["proposal_task_id"] != idempotency_key or self._run_hash(fresh) != digest:
                    raise HTTPException(409, "Настройки или кандидат изменились до подтверждения")
                transaction.execute("UPDATE planning_runs SET status='approved',applied_task_id=?,message=?,updated_at=? WHERE id=?", (idempotency_key, "Подтверждённый кандидат сохранён в локальном черновике. Публикации и отправки нет.", _utc(), row["id"]))
                request_binding = json.loads(row['base_snapshot_json']).get('requestBinding')
                if request_binding is not None:
                    from .request_planning import mark_applied
                    mark_applied(transaction, request_binding, row['id'], row['draft_revision']+1, profile)

            return apply_batch(lessons, row["draft_revision"], profile, idempotency_key=idempotency_key, expected_hash=command_hash, source_name=source_name, guard=guard)

    def reject_run_proposal(self, proposal, profile):
        actor = _actor(profile)
        with self._locked(), closing(self._connect()) as connection:
            row = self._row(connection, proposal["runId"], actor)
            if row["status"] in CANDIDATES and self._run_hash(row) == proposal["hash"]:
                connection.execute("UPDATE planning_runs SET status='rejected',message=?,updated_at=? WHERE id=?", ("Кандидат отклонён оператором. Черновик не изменён.", _utc(), row["id"]))

    def questions(self, profile):
        actor = _actor(profile)
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT payload FROM planning_questions WHERE owner_id=? AND owner_role=? ORDER BY rowid DESC LIMIT ?", (*actor, QUESTION_LIMIT)).fetchall()
        items = []
        for row in rows:
            item = json.loads(row[0])
            item["overdue"] = item["status"] != "answered" and item["deadline"] < _utc()
            if item["overdue"]:
                item["status"] = "escalated"
            items.append(item)
        return {"questions": items, "notice": LOCAL_NOTICE}

    def prepare_questions(self, body: QuestionInput | dict, profile):
        actor = _actor(profile)
        body = QuestionInput.model_validate(body)
        with self._locked(), closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            config = self._wrapper(connection, profile)
            if config["revision"] != body.expectedConfigRevision:
                raise HTTPException(409, "Настройки изменились; обновите вопросы")
            roster = {}
            for collection, kind, label in (("teachers", "teacher", "name"), ("rooms", "room", "name"), ("groups", "group", "name")):
                for item in (config["input"] or {}).get(collection, []):
                    roster[item["id"]] = {"type": kind, "id": item["id"], "label": item[label]}
            for blocker in config["readiness"]["blockers"]:
                dedup = _hash({"revision": config["revision"], "code": blocker["code"], "entityIds": sorted(blocker.get("entityIds", []))})
                if connection.execute("SELECT 1 FROM planning_questions WHERE owner_id=? AND owner_role=? AND dedup_key=?", (*actor, dedup)).fetchone():
                    continue
                if connection.execute("SELECT COUNT(*) FROM planning_questions").fetchone()[0] >= QUESTION_LIMIT:
                    oldest = connection.execute("SELECT id,payload FROM planning_questions ORDER BY rowid").fetchall()
                    answered = next((old for old in oldest if json.loads(old["payload"])["status"] == "answered"), None)
                    if answered is None:
                        raise HTTPException(429, "Достигнут предел неотвеченных локальных карточек вопросов")
                    connection.execute("DELETE FROM planning_questions WHERE id=?", (answered["id"],))
                stamp = _utc()
                item = {"id": "q_" + secrets.token_urlsafe(18), **blocker, "targets": [roster[entity] for entity in blocker.get("entityIds", []) if entity in roster] or [{"type": "config", "id": "plan", "label": "Оператор нормативного плана"}], "status": "draft", "answer": None, "deadline": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(timespec="microseconds").replace("+00:00", "Z"), "overdue": False, "createdAt": stamp, "updatedAt": stamp, "channel": "local", "delivery": "not_connected", "notice": LOCAL_NOTICE}
                connection.execute("INSERT INTO planning_questions VALUES(?,?,?,?,?,?)", (item["id"], *actor, config["revision"], dedup, _json(item)))
            connection.commit()
        return self.questions(profile)

    def answer_question(self, question_id, body: AnswerInput | dict, profile):
        actor = _actor(profile)
        body = AnswerInput.model_validate(body)
        with self._locked(), closing(self._connect()) as connection:
            row = connection.execute("SELECT payload,owner_id,owner_role FROM planning_questions WHERE id=?", (question_id,)).fetchone()
            if row is None or (row["owner_id"], row["owner_role"]) != actor:
                raise HTTPException(404, "Вопрос не найден")
            item = json.loads(row["payload"])
            if item["answer"] is not None and item["answer"] != body.text:
                raise HTTPException(409, "Ответ уже зафиксирован; используйте новую карточку для новых сведений")
            if item["answer"] is None:
                item.update(answer=body.text, status="answered", updatedAt=_utc(), overdue=False)
                connection.execute("UPDATE planning_questions SET payload=? WHERE id=?", (_json(item), question_id))
            return item

    def substitutions(self, body: SubstitutionInput | dict, profile):
        _actor(profile)
        body = SubstitutionInput.model_validate(body)
        config, draft = self.read_config(profile), read_draft()
        return self._substitution_candidates(config, draft, body)

    @staticmethod
    def _substitution_candidates(config, draft, body):
        if config["revision"] != body.configRevision or draft["revision"] != body.expectedDraftRevision:
            raise HTTPException(409, "Настройки или черновик изменились")
        if not config["readiness"]["ready"]:
            return {"candidates": [], "blockers": config["readiness"]["blockers"], "notice": "Подбор доступен только для подтверждённого нормативного плана. Это предложения без назначения."}
        plan = config.get('effectiveInput', config["input"])
        lesson = next((item for item in draft["lessons"] if item["id"] == body.lessonId), None)
        if lesson is None:
            raise HTTPException(404, "Урок не найден")
        mapped = [demand for demand in plan["demands"] if body.lessonId in demand.get("sourceLessonIds", [])]
        slots = {slot["id"]: slot for slot in plan["slots"]}
        target_slots = [slot for slot in slots.values() if (slot["day"], slot["start"], slot["end"]) == (lesson["day"], lesson["start"], lesson["end"])]
        mapped_names = {canonical(teacher["name"]) for teacher in plan["teachers"] if mapped and teacher["id"] in mapped[0]["teacherIds"]}
        if (len(mapped) != 1 or len(target_slots) != 1 or canonical(mapped[0]["subject"]) != canonical(lesson["subject"])
                or len(lesson["teachers"]) != 1 or canonical(lesson["teachers"][0]) not in mapped_names):
            return {"candidates": [], "blockers": [_block("confirmed_mapping_required", "Нужно однозначное подтверждённое сопоставление sourceLessonIds, интервала и одного заменяемого преподавателя.", [body.lessonId])], "notice": "Совпадения по похожим именам не используются; назначение не выполнялось."}
        target_slot = target_slots[0]
        if config.get('calendarBinding'):
            affected_groups = {item['id']: item for item in plan['groups']}
            if any(target_slot['id'] in affected_groups[group_id]['unavailableSlotIds'] for group_id in mapped[0]['groupIds']):
                return {'candidates': [], 'blockers': [_block('group_unavailable', 'Время недоступно для учебной группы по подтверждённым условиям')], 'notice': 'Измените дату или условия отдельным согласованным решением.'}
            if any(canonical(room['name']) in {canonical(name) for name in lesson['rooms']} and target_slot['id'] in room['unavailableSlotIds'] for room in plan['rooms']):
                return {'candidates': [], 'blockers': [_block('room_unavailable', 'Кабинет недоступен по подтверждённому календарю')], 'notice': 'Замена преподавателя не устраняет недоступность кабинета.'}
        candidates = []
        overlaps = lambda a, b: a["day"] == b["day"] and a["start"] < b["end"] and b["start"] < a["end"]
        for teacher in plan["teachers"]:
            if canonical(lesson["subject"]) not in {canonical(subject) for subject in teacher["subjects"]} or canonical(teacher["name"]) == canonical(lesson["teachers"][0]):
                continue
            assigned = [row for row in draft["lessons"] if row["id"] != lesson["id"] and canonical(teacher["name"]) in {canonical(name) for name in row["teachers"]}]
            # Shared events projected to several classes consume one teaching event.
            assigned = list({row.get("planningEventId") or row["id"]: row for row in assigned}.values())
            reasons = []
            if any(overlaps(target_slot, slots[slot_id]) for slot_id in teacher["unavailableSlotIds"]):
                reasons.append("Подтверждённая недоступность в это время")
            if any(overlaps(lesson, other) for other in assigned):
                reasons.append("Пересечение с текущим занятием")
            daily = sum(other["day"] == lesson["day"] for other in assigned)
            if daily + 1 > teacher["maxDaily"]:
                reasons.append("Превышение дневной нагрузки")
            if len(assigned) + 1 > teacher["maxWeekly"]:
                reasons.append("Превышение недельной нагрузки")
            ordinals = {other["slot"] for other in assigned if other["day"] == lesson["day"]} | {lesson["slot"]}
            longest = current = 0
            previous = -2
            for ordinal in sorted(ordinals):
                current = current + 1 if ordinal == previous + 1 else 1
                previous = ordinal
                longest = max(longest, current)
            if longest > teacher["maxConsecutive"]:
                reasons.append("Превышение последовательных занятий")
            editable = {key: lesson[key] for key in ("className", "day", "slot", "start", "end", "subject", "teachers", "rooms")}
            editable["teachers"] = [teacher["name"]]
            candidates.append({"teacherId": teacher["id"], "name": teacher["name"], "available": not reasons, "reasons": reasons, "dailyLoad": daily, "weeklyLoad": len(assigned), "proposal": {"lessonId": lesson["id"], "expectedRevision": draft["revision"], "lesson": editable} if not reasons else None})
        candidates.sort(key=lambda item: (not item["available"], item["dailyLoad"], item["weeklyLoad"], item["teacherId"]))
        response = {"candidates": candidates, "blockers": [], "notice": "Подбор по подтверждённой квалификации, доступности и текущей сетке. Выбор требует точного предложения и отдельного подтверждения; назначений нет."}
        if config.get("calendarBinding") is not None:
            response["calendarBinding"] = config["calendarBinding"]
        return response

    @staticmethod
    def _substitution_hash(proposal):
        body = LessonEditInput.model_validate({"expectedRevision": proposal["expectedRevision"], "lesson": proposal["lesson"]})
        payload = {"kind": "substitution", "lessonId": proposal["lessonId"], **body.model_dump(), "configRevision": proposal["configRevision"], "replacementTeacherId": proposal["replacementTeacherId"]}
        if proposal.get('calendarBinding') is not None:
            payload['calendarBinding'] = proposal['calendarBinding']
        return _hash(payload)

    @staticmethod
    def _check_substitution_candidate(response, proposal):
        candidate = next((candidate for candidate in response["candidates"] if candidate["teacherId"] == proposal["replacementTeacherId"]), None)
        expected = {"lessonId": proposal["lessonId"], "expectedRevision": proposal["expectedRevision"], "lesson": proposal["lesson"]}
        if candidate is None or not candidate["available"] or candidate["proposal"] != expected:
            raise HTTPException(409, "Подтверждённые условия подбора замены изменились или точная правка отличается")

    def prepare_substitution_proposal(self, proposal, profile):
        if proposal.get('calendarBinding') is not None:
            from .school_calendar import assert_calendar_binding_current
            with closing(self._connect()) as connection:
                assert_calendar_binding_current(proposal['calendarBinding'], connection=connection)
        response = self.substitutions({"lessonId": proposal["lessonId"], "expectedDraftRevision": proposal["expectedRevision"], "configRevision": proposal["configRevision"]}, profile)
        self._check_substitution_candidate(response, proposal)
        bound = dict(proposal)
        if response.get('calendarBinding') is not None:
            bound['calendarBinding'] = response['calendarBinding']
        return {"hash": self._substitution_hash(bound), 'proposal': bound}

    def apply_substitution_proposal(self, proposal, profile, idempotency_key, expected_hash):
        _actor(profile)
        from .timetable import update_lesson
        digest = self._substitution_hash(proposal)
        if digest != expected_hash:
            raise HTTPException(409, "Содержание подтверждения замены изменено")
        body = LessonEditInput.model_validate({"expectedRevision": proposal["expectedRevision"], "lesson": proposal["lesson"]})

        def guard(transaction):
            if proposal.get('calendarBinding') is not None:
                from .school_calendar import assert_calendar_binding_current
                assert_calendar_binding_current(proposal['calendarBinding'], connection=transaction)
            config = self._wrapper(transaction, profile)
            source = transaction.execute("SELECT payload FROM timetable_draft WHERE id=1").fetchone()
            draft = json.loads(source[0]) if source else {"revision": 0, "lessons": []}
            for lesson in draft["lessons"]:
                if lesson.get("_planningEventId"):
                    lesson["planningEventId"] = lesson["_planningEventId"]
            response = self._substitution_candidates(config, draft, SubstitutionInput(lessonId=proposal["lessonId"], expectedDraftRevision=proposal["expectedRevision"], configRevision=proposal["configRevision"]))
            self._check_substitution_candidate(response, proposal)

        with self._locked():
            return update_lesson(proposal["lessonId"], body, profile, idempotency_key=idempotency_key, expected_hash=mutation_hash(proposal["lessonId"], body), guard=guard)


class SafePlanningRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def safe_handler(request):
            try:
                return await original(request)
            except RequestValidationError:
                raise HTTPException(422, "Некорректный запрос планирования") from None
        return safe_handler


router = APIRouter(prefix="/api/planning", tags=["local-planning"], route_class=SafePlanningRoute)
_services: dict[str, PlanningService] = {}
_service_lock = threading.Lock()
_authorized = require_roles("admin", "director")


def get_service():
    path = str(database_path())
    with _service_lock:
        if path not in _services:
            _services[path] = PlanningService(path)
        return _services[path]


def prepare_run_proposal(run_id, profile):
    return get_service().prepare_run_proposal(run_id, profile)


def apply_run_proposal(proposal, profile, idempotency_key, expected_hash):
    return get_service().apply_run_proposal(proposal, profile, idempotency_key, expected_hash)


def reject_run_proposal(proposal, profile):
    return get_service().reject_run_proposal(proposal, profile)


def prepare_substitution_proposal(proposal, profile):
    return get_service().prepare_substitution_proposal(proposal, profile)


def apply_substitution_proposal(proposal, profile, idempotency_key, expected_hash):
    return get_service().apply_substitution_proposal(proposal, profile, idempotency_key, expected_hash)


@router.get("/config")
def config_get(profile: dict = Depends(_authorized)):
    return get_service().read_config(profile)


@router.post("/config")
def config_save(body: ConfigInput, profile: dict = Depends(_authorized)):
    return get_service().save_config(body, profile)


@router.get("/readiness")
def readiness_get(periodStart: str | None = Query(default=None, pattern=r'^[0-9]{4}-[0-9]{2}-[0-9]{2}$'), profile: dict = Depends(_authorized)):
    if periodStart is not None:
        try:
            RunInput.target_monday(periodStart)
        except ValueError:
            raise HTTPException(422, 'Выберите понедельник нужной недели') from None
    return {**get_service().read_config(profile, periodStart)["readiness"], "notice": LOCAL_NOTICE}


@router.get('/sources')
def sources_get(profile: dict = Depends(_authorized)):
    from .school_plan_imports import read_sources
    return read_sources(get_service(),profile)


@router.get('/school-source-registry')
def source_registry_get(profile: dict = Depends(_authorized)):
    from .school_source_registry import read_registry
    return read_registry(profile)


@router.post('/school-source-registry')
def source_registry_save(body: RegistrySaveInput, profile: dict = Depends(_authorized)):
    from .school_source_registry import save_registry
    return save_registry(body,profile)


@router.post('/sources/preview')
def sources_preview(body: SourceImportInput, profile: dict = Depends(_authorized)):
    from .school_plan_imports import preview_sources
    return preview_sources(get_service(),body,profile)


@router.post('/sources/import')
def sources_import(body: SourceImportInput, profile: dict = Depends(_authorized)):
    from .school_plan_imports import save_sources
    return save_sources(get_service(),body,profile)


@router.get("/template")
def template_get(kind: Literal["blank", "example"] = Query(default="blank"), profile: dict = Depends(_authorized)):
    from .workspace import school_workspace
    if kind == 'example' and school_workspace():
        raise HTTPException(404, 'Демонстрационные условия недоступны в рабочей школе')
    return template_input(kind)


@router.post("/from-draft")
def from_draft_post(body: FromDraftInput, profile: dict = Depends(_authorized)):
    return get_service().from_draft(body, profile)


@router.post("/runs")
def run_create(body: RunInput, profile: dict = Depends(_authorized)):
    return get_service().create_run(body, profile)


@router.get("/runs")
def run_list(profile: dict = Depends(_authorized)):
    return get_service().list_runs(profile)


@router.get("/runs/{run_id}")
def run_get(run_id: str = APIPath(min_length=1, max_length=80, pattern=r"^run_[A-Za-z0-9_-]+$"), profile: dict = Depends(_authorized)):
    return get_service().get_run(run_id, profile)


@router.post("/runs/{run_id}/cancel")
def run_cancel(body: EmptyInput, run_id: str = APIPath(min_length=1, max_length=80, pattern=r"^run_[A-Za-z0-9_-]+$"), profile: dict = Depends(_authorized)):
    return get_service().cancel_run(run_id, profile)


@router.post("/runs/{run_id}/propose")
def run_propose(body: EmptyInput, run_id: str = APIPath(min_length=1, max_length=80, pattern=r"^run_[A-Za-z0-9_-]+$"), profile: dict = Depends(_authorized)):
    return get_service().propose_run(run_id, profile)


@router.post('/runs/{run_id}/adjust')
def run_adjust(body: AdjustRunInput, run_id: str = APIPath(min_length=1, max_length=80, pattern=r'^run_[A-Za-z0-9_-]+$'), profile: dict = Depends(_authorized)):
    return get_service().adjust_run(run_id, body, profile)


@router.get("/questions")
def question_list(profile: dict = Depends(_authorized)):
    return get_service().questions(profile)


@router.post("/questions")
def question_prepare(body: QuestionInput, profile: dict = Depends(_authorized)):
    return get_service().prepare_questions(body, profile)


@router.post("/questions/{question_id}/answer")
def question_answer(body: AnswerInput, question_id: str = APIPath(min_length=1, max_length=80, pattern=r"^q_[A-Za-z0-9_-]+$"), profile: dict = Depends(_authorized)):
    return get_service().answer_question(question_id, body, profile)


@router.post("/substitutions")
def substitution_candidates(body: SubstitutionInput, profile: dict = Depends(_authorized)):
    return get_service().substitutions(body, profile)


# Assumed school candidates are isolated from normative runs and publication.
from .experimental_school import router as experimental_school_router
router.include_router(experimental_school_router)
from .experimental_edit_routes import router as experimental_edit_router
from .experimental_publications import router as experimental_publication_router
router.include_router(experimental_edit_router)
router.include_router(experimental_publication_router)
