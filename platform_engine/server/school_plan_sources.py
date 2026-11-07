"""Verified school-source package and conservative, side-effect-free plan preview.

Source cells are provenance, never timetable lesson IDs. Persistence and access
control belong to the caller; this module neither writes a config nor publishes.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date
import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from .scheduling_models import (
    Demand, Group, Identifier, PlanningInput, Room, RoomTransition, StrictModel, Subject, Teacher,
    WorkWindow, canonical,
)
from .subject_names import normalise_subject_name
from .school_rule_updates import SourceRuleUpdate

PACKAGE_PATH = Path(__file__).resolve().parent / "assets" / "school-inputs-2026-10-24.json"
SourceCellId = Annotated[str, StringConstraints(pattern=r"^[A-Z]{1,3}[1-9][0-9]{0,5}$")]
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]


class SubjectPart(StrictModel):
    subject: Subject
    weeklyCount: int = Field(ge=1, le=40)


class SourceCell(StrictModel):
    sourceCell: SourceCellId
    sourceId: Identifier
    sourceLocator: str = Field(min_length=1, max_length=160)
    row: int = Field(ge=1)
    classLabel: str = Field(min_length=1, max_length=80)
    rawCount: int | str
    teacher: str = Field(min_length=1, max_length=120)
    rawSubject: Subject
    normalizedSubjects: list[SubjectPart] = Field(max_length=10)
    status: Literal["confirmed", "requires_resolution"]
    blockers: list[str] = Field(max_length=20)


class ConfirmedCount(StrictModel):
    weeklyCount: int = Field(ge=1, le=40)
    sourceId: Identifier
    sourceLocator: str = Field(min_length=1, max_length=160)
    reason: Reason


class ConfirmedCountPattern(ConfirmedCount):
    teachingDays: int = Field(ge=1, le=6)
    consecutiveSlots: bool = False

    @model_validator(mode='after')
    def distribution(self):
        if self.teachingDays > self.weeklyCount:
            raise ValueError('Число учебных дней превышает число занятий')
        if self.consecutiveSlots and self.teachingDays != 1:
            raise ValueError('Непрерывная пара должна находиться в одном дне')
        return self


class SourcePackage(StrictModel):
    schemaVersion: Literal[1]
    packageId: Identifier
    asOf: str
    sources: list[dict] = Field(max_length=10)
    cells: list[SourceCell] = Field(max_length=2000)
    conditions: list[dict] = Field(max_length=200)
    sourceRows: list[dict] = Field(max_length=200)
    groupObservations: list[dict] = Field(max_length=200)
    confirmedCorrections: list[dict] = Field(max_length=20)
    unresolvedQuestions: list[dict] = Field(max_length=100)
    confirmedCounts: dict[SourceCellId, ConfirmedCount] = Field(default_factory=dict)
    curriculumObservations: list[dict] = Field(default_factory=list, max_length=200)
    countPatterns: dict[Literal['2/1', '4/2'], ConfirmedCountPattern] = Field(default_factory=dict)
    ruleUpdates: list[SourceRuleUpdate] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def unique_cells(self):
        ids = [cell.sourceCell for cell in self.cells]
        if len(ids) != len(set(ids)):
            raise ValueError("Повторная исходная ячейка в пакете")
        condition_ids = [condition.get("id") for condition in self.conditions]
        if any(not isinstance(key, str) or not key for key in condition_ids) or len(condition_ids) != len(set(condition_ids)):
            raise ValueError("Условия источников должны иметь уникальные id")
        if not self.confirmedCounts.keys() <= set(ids):
            raise ValueError("Подтверждённые часы ссылаются на неизвестную исходную ячейку")
        source_ids = {item.get("id") for item in self.sources}
        if any(item.sourceId not in source_ids for item in self.confirmedCounts.values()):
            raise ValueError("Подтверждённые часы ссылаются на неизвестный источник")
        if any(item.sourceId not in source_ids for item in [*self.countPatterns.values(), *self.ruleUpdates]):
            raise ValueError('Уточнение ссылается на неизвестный источник')
        pairs = [(item.conditionId, canonical(item.teacher)) for item in self.ruleUpdates]
        if len(pairs) != len(set(pairs)):
            raise ValueError('Повторное уточнение одного условия и педагога')
        hashes = {item['id']: hashlib.sha256(item['text'].encode('utf-8')).hexdigest()
                  for item in self.conditions if isinstance(item.get('text'), str)}
        if any(hashes.get(item.conditionId) != item.textHash for item in self.ruleUpdates):
            raise ValueError('Уточнение не связано с точным текстом исходного условия')
        return self


class SubjectOverride(StrictModel):
    subjects: list[SubjectPart] = Field(min_length=1, max_length=10)
    reason: Reason


class TeacherAssignmentOverride(StrictModel):
    teacherId: Identifier
    reason: Reason


class SharedLesson(StrictModel):
    sourceCells: list[SourceCellId] = Field(min_length=2, max_length=200)
    reason: Reason


class TeacherLimits(StrictModel):
    # Qualifications and bounds must be explicit, never inferred from a cell.
    subjects: list[Subject] = Field(min_length=1, max_length=100)
    unavailableSlotIds: list[Identifier] = Field(max_length=100)
    maxDaily: int = Field(ge=1, le=20)
    maxWeekly: int = Field(ge=1, le=100)
    maxConsecutive: int = Field(ge=1, le=20)
    workWindows: list[WorkWindow] = Field(default_factory=list, max_length=100)
    presenceEvents: list[WorkWindow] = Field(default_factory=list, max_length=100)
    requiredBreaks: list[WorkWindow] = Field(default_factory=list, max_length=100)
    minTransitionMinutes: int = Field(default=0, ge=0, le=180)
    maxDailySpanMinutes: int | None = Field(default=None, ge=1, le=1440)
    maxDailyGapMinutes: int | None = Field(default=None, ge=0, le=1440)


class ExistingSourceLink(StrictModel):
    demandId: Identifier
    sourceCells: list[SourceCellId] = Field(min_length=1, max_length=200)
    subject: Subject
    weeklyCount: int = Field(ge=1, le=40)
    teacherIds: list[Identifier] = Field(default_factory=list, max_length=8)
    teacherOverrideApplied: bool = False


class SourceConditionResolution(StrictModel):
    mode: Literal["manual_attestation"]
    reason: Reason
    planHash: str = Field(pattern=r"^[a-f0-9]{64}$")
    # Legacy records remain readable, but an absent hash is never trusted.
    packageHash: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")
    calendarHash: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")


class UnallocatedRowResolution(StrictModel):
    mode: Literal["not_additional_load", "allocated"]
    reason: Reason
    demandIds: list[Identifier] = Field(default_factory=list, max_length=2000)
    weeklyCount: int | None = Field(default=None, ge=1, le=2000)

    @model_validator(mode="after")
    def allocation_shape(self):
        if len(self.demandIds) != len(set(self.demandIds)):
            raise ValueError("Строка повторно ссылается на одно назначение")
        if self.mode == "allocated" and not self.demandIds:
            raise ValueError("Распределённая строка должна ссылаться на назначения")
        if self.mode == "not_additional_load" and (self.demandIds or self.weeklyCount is not None):
            raise ValueError("Итоговая строка без дополнительной нагрузки не должна создавать назначения")
        return self


class SourceResolutions(StrictModel):
    classMap: dict[str, str] = Field(default_factory=dict)
    groupMap: dict[SourceCellId, list[Identifier]] = Field(default_factory=dict)
    teacherMap: dict[str, Identifier] = Field(default_factory=dict)
    teacherOverrides: dict[SourceCellId, TeacherAssignmentOverride] = Field(default_factory=dict)
    roomIds: dict[SourceCellId, list[Identifier]] = Field(default_factory=dict)
    groups: list[Group] = Field(default_factory=list, max_length=200)
    teachers: list[Teacher] = Field(default_factory=list, max_length=300)
    rooms: list[Room] = Field(default_factory=list, max_length=300)
    roomTransitions: list[RoomTransition] = Field(default_factory=list, max_length=1000)
    teacherLimits: dict[Identifier, TeacherLimits] = Field(default_factory=dict)
    countOverrides: dict[SourceCellId, Annotated[int, Field(ge=1, le=40)]] = Field(default_factory=dict)
    subjectOverrides: dict[SourceCellId, SubjectOverride] = Field(default_factory=dict)
    sharedLessons: list[SharedLesson] = Field(default_factory=list, max_length=1000)
    excludedCells: dict[SourceCellId, Reason] = Field(default_factory=dict)
    # Supplied by the durable wrapper from the PREVIOUS accepted provenance,
    # not inferred from IDs and not obtained from an untrusted source document.
    existingProvenance: list[ExistingSourceLink] = Field(default_factory=list, max_length=2000)
    sourceConditions: dict[str, SourceConditionResolution] = Field(default_factory=dict)
    unallocatedRows: dict[Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]{0,5}$")], UnallocatedRowResolution] = Field(default_factory=dict)

    @model_validator(mode="after")
    def no_duplicate_bindings(self):
        pairs = [(rule.fromRoomId, rule.toRoomId) for rule in self.roomTransitions]
        if len(pairs) != len(set(pairs)):
            raise ValueError("Повторное правило направленного перехода")
        for mapping in (self.groupMap, self.roomIds):
            for values in mapping.values():
                if not values or len(values) != len(set(values)):
                    raise ValueError("Привязка ячейки должна содержать непустые уникальные id")
        used = []
        for lesson in self.sharedLessons:
            if len(lesson.sourceCells) != len(set(lesson.sourceCells)):
                raise ValueError("Общее занятие повторяет исходную ячейку")
            used.extend(lesson.sourceCells)
        if len(used) != len(set(used)):
            raise ValueError("Исходная ячейка используется в нескольких общих занятиях")
        if set(used) & self.excludedCells.keys():
            raise ValueError("Исключённая ячейка одновременно включена в общее занятие")
        linked = [link.demandId for link in self.existingProvenance]
        if len(linked) != len(set(linked)):
            raise ValueError("Повторная связь исходника с назначением")
        for link in self.existingProvenance:
            if len(link.sourceCells) != len(set(link.sourceCells)):
                raise ValueError("Повторная ячейка в сохранённой связи исходника")
        source_parts = [(cell, canonical(link.subject)) for link in self.existingProvenance for cell in link.sourceCells]
        if len(source_parts) != len(set(source_parts)):
            raise ValueError("Одна часть исходной ячейки связана с несколькими назначениями")
        row_parts = [demand_id for row in self.unallocatedRows.values() for demand_id in row.demandIds]
        if len(row_parts) != len(set(row_parts)):
            raise ValueError("Назначение одновременно распределено на несколько строк без классов")
        return self


def load_source_package() -> dict:
    """Read only the bundled server-side package and return a detached copy."""
    return SourcePackage.model_validate(json.loads(PACKAGE_PATH.read_text(encoding="utf-8"))).model_dump(mode="json")


def source_conditions(package: dict) -> list[dict]:
    """Expose tariff day observations without guessing their priority/meaning.

    These are additional evidence, not DOCX paragraphs. A manual review is
    bound to the same exact source package and complete planning model.
    """
    tariff = next((item for item in package.get("sources", []) if item.get("id") == "tariff"), {})
    return deepcopy(package.get("conditions", [])) + [
        {"id": f"tariff-methoddays-row-{row['row']}", "kind": "tariff_method_days",
         "teacher": row["teacher"],
         "text": f"{row['teacher']}, {row['rawSubject']}: «Методдни и часы» — {row['rawMethodDays']}. Подтвердите запреты и приоритет при расхождении с пожеланиями.",
         "source": {"sourceId": "tariff", "file": tariff.get("file", "Тарификация"), "locator": f"U{row['row']}"}}
        for row in package.get("sourceRows", []) if row.get("rawMethodDays")
    ]


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _same_source_facts(existing: dict, compiled: dict) -> bool:
    """Source identity/load stays protected; later scheduling rules stay local.

    Lists of entity IDs express participation/alternatives rather than order.
    Dates, week types, permitted slots, tags, sync constraints and original
    lesson links are validated by PlanningInput and intentionally not compared
    against the default constraints of a newly compiled demand.
    """
    return (existing["subject"] == compiled["subject"]
            and existing["weeklyCount"] == compiled["weeklyCount"]
            and all(set(existing[field]) == set(compiled[field])
                    for field in ("groupIds", "teacherIds", "roomIds")))


def _parts(cell: SourceCell, resolution: SourceResolutions,
           confirmed_counts: dict[str, ConfirmedCount] | None = None) -> list[dict]:
    override = resolution.subjectOverrides.get(cell.sourceCell)
    count = resolution.countOverrides.get(cell.sourceCell)
    raw_count = cell.rawCount if type(cell.rawCount) is int else None
    if count is None:
        count = raw_count
    if count is None and confirmed_counts and cell.sourceCell in confirmed_counts:
        # Only this exact cell has independent weekly-count evidence. This is
        # not a general interpretation of a tariff slash or a week cycle.
        count = confirmed_counts[cell.sourceCell].weeklyCount
    if override:
        parts = [part.model_dump() for part in override.subjects]
        if count is None:
            return []  # A slash requires a separate explicit count resolution.
        if sum(part["weeklyCount"] for part in parts) != count:
            raise ValueError(f"{cell.sourceCell}: сумма предметов не совпадает с подтверждённым числом часов")
        return parts
    if count is None:
        return []
    # Recompute authorised labels from the original facts, rather than trusting
    # a client-modified normalizedSubjects array.
    if cell.rawSubject == "Родной язык":
        if cell.teacher in {"Мельникова Н.И.", "Гилазетдинова Г.Г."}:
            return [{"subject": "Гос. язык РТ", "weeklyCount": count}]
        if count == 2:
            return [{"subject": "Родной русский язык", "weeklyCount": 1},
                    {"subject": "Родная русская литература", "weeklyCount": 1}]
        return []
    if "/" in cell.rawSubject:
        return []
    return [{"subject": normalise_subject_name(cell.classLabel, cell.rawSubject), "weeklyCount": count}]


def apply_count_patterns(source: SourcePackage, plan: dict, provenance: list[dict]) -> dict:
    """Bind confirmed session patterns to physical events, including shared cells.

    A two-part subject split may still be one pair. Four sessions on two days
    never implies a 2+2 distribution, fixed weekdays or a room assignment.
    """
    value = deepcopy(plan)
    demands = {item['id']: item for item in value['demands']}
    applied, unresolved, seen = [], [], set()
    for cell in source.cells:
        pattern = source.countPatterns.get(cell.rawCount) if isinstance(cell.rawCount, str) else None
        if pattern is None:
            continue
        ids = sorted({entry['demandId'] for entry in provenance
                      if cell.sourceCell in entry['sourceCells'] and entry['demandId'] in demands})
        if not ids:
            continue  # Missing source bindings retain the compiler's own blocker.
        events = [f'{key}_{n}' for key in ids for n in range(demands[key]['weeklyCount'])]
        identity = tuple(events)
        if identity in seen:
            continue
        seen.add(identity)
        issue = None
        if len(events) != pattern.weeklyCount:
            issue = 'Число физических занятий не совпадает с подтверждённой записью тарификации.'
        elif pattern.teachingDays > 1 and len(ids) != 1:
            issue = 'Распределение нескольких частей предмета по дням требует отдельного подтверждения.'
        elif any(demands[key].get('teachingDays') not in (None, pattern.teachingDays) for key in ids):
            issue = 'Сохранённое число учебных дней противоречит подтверждённому правилу; автоматическая перезапись запрещена.'
        if issue:
            unresolved.append({'code': 'source_session_pattern_conflict', 'sourceCells': [cell.sourceCell],
                               'demandIds': ids, 'message': issue})
            continue
        for key in ids:
            demands[key]['teachingDays'] = pattern.teachingDays
        if pattern.consecutiveSlots:
            chain = {'id': 'src_pair_' + _digest(events)[:28], 'eventIds': events,
                     'sameDay': True, 'consecutiveSlots': True, 'maxGapMinutes': None}
            if len(ids) > 1:
                chain['unorderedPair'] = True  # The source states adjacency, not subject order.
            prior = next((entry for entry in value['eventChains'] if entry['id'] == chain['id']), None)
            if prior is not None and prior != chain:
                unresolved.append({'code': 'source_session_pattern_conflict', 'sourceCells': [cell.sourceCell],
                                   'message': 'Сохранённая связка изменена; автоматическая перезапись запрещена.'})
                continue
            if prior is None:
                value['eventChains'].append(chain)
        applied.append({'sourceCell': cell.sourceCell, 'rawCount': cell.rawCount,
                        'demandIds': ids, 'eventIds': events, **pattern.model_dump(mode='json')})
    if value != plan:
        value['confirmed'] = False
    return {'input': PlanningInput.model_validate(value).model_dump(mode='json'),
            'applied': applied, 'unresolved': unresolved}


def compile_source_plan(package, resolutions, base_input, period_start) -> dict:
    """Return an additive preview; never overwrite manual records or save.

    ``resolutions`` is SourceResolutions JSON. Cell keys are Excel coordinates
    (e.g. D78). Explicit sharedLessons merge cells into ONE weekly demand per
    subject; all participating classes must have explicit group bindings.
    Source provenance is returned separately from PlanningInput. Existing exact
    generated demands are reused with their validated scheduling constraints;
    changed source facts or matching manual demands are blocked for review.
    ``sourceLessonIds`` remain empty for new demands and are preserved on reuse.
    """
    source = SourcePackage.model_validate(package)
    package_hash = _digest(source.model_dump(mode="json"))
    conditions = source_conditions(source.model_dump(mode="json"))
    resolved = SourceResolutions.model_validate(resolutions)
    # Validate Monday even when no cells can yet be compiled.
    if date.fromisoformat(period_start).weekday() != 0:
        raise ValueError("Начало периода должно приходиться на понедельник")
    if base_input is None:
        base_input = {"periodStart": period_start, "slots": [], "groups": [], "teachers": [], "rooms": [], "demands": []}
    plan = PlanningInput.model_validate(base_input).model_dump(mode="json")
    plan["periodStart"] = period_start
    cells = {cell.sourceCell: cell for cell in source.cells}
    for mapping in (resolved.groupMap, resolved.roomIds, resolved.countOverrides, resolved.subjectOverrides, resolved.teacherOverrides, resolved.excludedCells):
        if not mapping.keys() <= cells.keys():
            raise ValueError("Решение ссылается на неизвестную исходную ячейку")
    if not resolved.classMap.keys() <= {cell.classLabel for cell in source.cells}:
        raise ValueError("classMap содержит неизвестное исходное обозначение класса")
    source_teacher_names = {cell.teacher for cell in source.cells} | {row.get("teacher") for row in source.sourceRows}
    if not resolved.teacherMap.keys() <= source_teacher_names:
        raise ValueError("teacherMap содержит неизвестное исходное имя педагога")
    if not resolved.sourceConditions.keys() <= {condition["id"] for condition in conditions}:
        raise ValueError("Решение ссылается на неизвестное условие источника")
    unallocated_rows = {str(row["row"]): row for row in source.sourceRows
                        if not row.get("sourceCells") and row.get("rawTotal") not in (None, "", 0)}
    if not resolved.unallocatedRows.keys() <= unallocated_rows.keys():
        raise ValueError("Решение строки без классов ссылается на неизвестную или уже распределённую строку")
    for shared in resolved.sharedLessons:
        if not set(shared.sourceCells) <= cells.keys():
            raise ValueError("Общее занятие ссылается на неизвестную исходную ячейку")

    # Add explicit entities only; a conflicting id is never an implicit edit.
    teachers_to_add = list(resolved.teachers)
    for teacher_id, limits in resolved.teacherLimits.items():
        existing = next((teacher for teacher in [*plan["teachers"], *[t.model_dump() for t in teachers_to_add]] if teacher["id"] == teacher_id), None)
        if existing:
            if any(existing.get(key) != value for key, value in limits.model_dump().items()):
                raise ValueError("teacherLimits конфликтует с сохранённым педагогом; измените его отдельно")
            continue
        raw_names = [name for name, mapped_id in resolved.teacherMap.items() if mapped_id == teacher_id]
        if len(raw_names) != 1:
            raise ValueError("Для неоднозначного имени нужен явный объект teachers")
        teachers_to_add.append(Teacher(id=teacher_id, name=raw_names[0], **limits.model_dump()))
    for field, additions in (("groups", resolved.groups), ("teachers", teachers_to_add), ("rooms", resolved.rooms)):
        by_id = {item["id"]: item for item in plan[field]}
        for item in additions:
            value = item.model_dump(mode="json")
            if item.id in by_id and by_id[item.id] != value:
                raise ValueError(f"Нельзя перезаписать существующий {field}: {item.id}")
            if item.id not in by_id:
                plan[field].append(value)
                by_id[item.id] = value
    transition_pairs = {(rule['fromRoomId'], rule['toRoomId']): rule for rule in plan['roomTransitions']}
    for rule in resolved.roomTransitions:
        key = (rule.fromRoomId, rule.toRoomId)
        value = rule.model_dump(mode="json")
        if key in transition_pairs and transition_pairs[key] != value:
            raise ValueError("Нельзя перезаписать существующее правило перехода; измените его отдельно")
        if key not in transition_pairs:
            plan['roomTransitions'].append(value)
            transition_pairs[key] = value
    # Catch invalid supplied availability, duplicate names and capacities before
    # compiling anything. No caller-provided object is mutated.
    plan = PlanningInput.model_validate(plan).model_dump(mode="json")
    groups = {item["id"]: item for item in plan["groups"]}
    teachers = {item["id"]: item for item in plan["teachers"]}
    rooms = {item["id"]: item for item in plan["rooms"]}
    demands = {item["id"]: item for item in plan["demands"]}
    for link in resolved.existingProvenance:
        if not set(link.sourceCells) <= cells.keys():
            raise ValueError("Сохранённая связь ссылается на неизвестную исходную ячейку")
        if link.demandId not in demands:
            raise ValueError("Сохранённая связь ссылается на отсутствующее назначение")
    unresolved, provenance, skipped, added, reused = [], [], [], [], []
    covered = set()
    units = [sorted(shared.sourceCells) for shared in resolved.sharedLessons]
    shared_cells = {key for unit in units for key in unit}
    units += [[key] for key in cells if key not in shared_cells and key not in resolved.excludedCells]

    def block(code, keys, message):
        unresolved.append({"code": code, "sourceCells": list(keys), "message": message})

    for keys in units:
        records = [cells[key] for key in keys]
        parts = [_parts(cell, resolved, source.confirmedCounts) for cell in records]
        before = len(unresolved)
        if any(not part for part in parts):
            block("count_or_subject_unresolved", keys, "Подтвердите недельный объём и предмет; дробь и одиночный родной не интерпретируются автоматически.")
        elif any(part != parts[0] for part in parts[1:]):
            block("shared_load_mismatch", keys, "Объединяемые ячейки должны задавать одинаковые предметы и часы.")
        teacher_ids, group_ids, permitted_rooms = [], [], []
        for cell in records:
            key = cell.sourceCell
            mapped_class = resolved.classMap.get(cell.classLabel)
            if not mapped_class:
                block("class_map_required", [key], f"Подтвердите обозначение класса {cell.classLabel}.")
            selected_groups = resolved.groupMap.get(key, [])
            if not selected_groups or any(group_id not in groups for group_id in selected_groups):
                block("group_map_required", [key], "Нужны существующие группы и явная привязка ячейки.")
            elif mapped_class and any(groups[group_id]["className"] != mapped_class for group_id in selected_groups):
                block("group_class_mismatch", [key], "Группа не принадлежит подтверждённому классу ячейки.")
            group_ids.extend(selected_groups)
            assignment = resolved.teacherOverrides.get(key)
            teacher_id = assignment.teacherId if assignment else resolved.teacherMap.get(cell.teacher)
            if not teacher_id or teacher_id not in teachers:
                block("teacher_map_required", [key], "Нужен фактический педагог с явными квалификацией и пределами нагрузки.")
            elif any(canonical(part["subject"]) not in {canonical(subject) for subject in teachers[teacher_id]["subjects"]} for part in parts[0]):
                block("teacher_subject_unconfirmed", [key], "Предмет отсутствует в подтверждённых квалификациях педагога.")
            if teacher_id:
                teacher_ids.append(teacher_id)
            room_ids = resolved.roomIds.get(key, [])
            if not room_ids or any(room_id not in rooms for room_id in room_ids):
                block("room_map_required", [key], "Нужны явно разрешённые кабинеты с подтверждённой вместимостью.")
            permitted_rooms.append(set(room_ids))
        if len(unresolved) != before:
            continue
        teacher_ids = sorted(set(teacher_ids))
        # Cells attributed to different teachers are separate appointments, not
        # automatically a co-teaching session. Resolve such modules explicitly.
        if len(teacher_ids) != 1:
            block("shared_teacher_mismatch", keys, "Для общего занятия должен быть однозначно подтверждён один фактический педагог.")
            continue
        group_ids = sorted(set(group_ids))
        room_ids = sorted(set.intersection(*permitted_rooms))
        required_size = sum(groups[group_id]["size"] for group_id in group_ids)
        room_ids = [room_id for room_id in room_ids if rooms[room_id]["capacity"] >= required_size]
        if not room_ids:
            block("no_common_room", keys, "Нет общего разрешённого кабинета достаточной вместимости.")
            continue
        prior_unit_links = [link for link in resolved.existingProvenance if set(keys) & set(link.sourceCells)]
        expected_ids = {"src_" + _digest([source.packageId, keys, part["subject"]])[:28] for part in parts[0]}
        # Ownership covers the complete source cell, including every native
        # language/literature part. A subject change must not leave old parts
        # in the model while admitting a second set of the same source hours.
        if any(link.demandId not in expected_ids or set(link.sourceCells) != set(keys) for link in prior_unit_links):
            block("source_cell_already_linked", keys, "Ячейка уже связана с другим назначением или составом предметов; сначала согласуйте замену или объединение.")
            continue
        candidates = []
        for part in parts[0]:
            demand_id = "src_" + _digest([source.packageId, keys, part["subject"]])[:28]
            candidate = Demand(id=demand_id, subject=part["subject"], groupIds=group_ids,
                               teacherIds=teacher_ids, roomIds=room_ids, weeklyCount=part["weeklyCount"]).model_dump(mode="json")
            prior_links = [link for link in prior_unit_links if link.demandId == demand_id]
            if demand_id in demands:
                if not _same_source_facts(demands[demand_id], candidate):
                    previous = demands[demand_id]
                    # A manager's exact per-cell assignment is an explicit
                    # source-setting change. Update only a still source-owned
                    # teacher field, preserving every local scheduling rule.
                    teacher_change = (len(prior_links) == 1 and prior_links[0].teacherIds
                        and set(prior_links[0].teacherIds) == set(previous["teacherIds"])
                        and (any(key in resolved.teacherOverrides for key in keys) or prior_links[0].teacherOverrideApplied)
                        and _same_source_facts({**previous, "teacherIds": candidate["teacherIds"]}, candidate))
                    if teacher_change:
                        candidates.append(({**deepcopy(previous), "teacherIds": candidate["teacherIds"]}, "reassigned"))
                    else:
                        block("existing_source_demand_changed", keys, "Подтверждённые предмет, группы, педагог, кабинеты или часы назначения изменены; автоматическая перезапись запрещена.")
                else:
                    candidates.append((deepcopy(demands[demand_id]), "reused"))
            elif any(demand["subject"] == candidate["subject"]
                     and set(demand["groupIds"]) == set(candidate["groupIds"])
                     and set(demand["teacherIds"]) == set(candidate["teacherIds"])
                     for demand in demands.values()):
                block("possible_duplicate_demand", keys, "В модели уже есть назначение с тем же предметом, группами и педагогом; проверьте связь с исходником.")
            else:
                candidates.append((candidate, "added"))
        if len(unresolved) != before:
            continue
        # All parts of one cell (especially native language + literature) are
        # admitted atomically, so a partial split cannot lose source hours.
        trial = deepcopy(plan)
        trial["demands"] += [candidate for candidate, action in candidates if action == "added"]
        replacements = {candidate["id"]: candidate for candidate, action in candidates if action == "reassigned"}
        trial["demands"] = [replacements.get(demand["id"], demand) for demand in trial["demands"]]
        try:
            PlanningInput.model_validate(trial)
        except ValueError:
            block("planning_model_rejected", keys, "Назначение нарушает ограничения модели: проверьте пересечения групп и пределы объёма.")
            continue
        plan = trial
        for candidate, action in candidates:
            if action == "added":
                demands[candidate["id"]] = candidate
                added.append(candidate["id"])
            else:
                reused.append(candidate["id"])
                if action == "reassigned":
                    demands[candidate["id"]] = candidate
            provenance.append({"demandId": candidate["id"], "sourceCells": keys,
                               "subject": candidate["subject"], "weeklyCount": candidate["weeklyCount"],
                               "shared": len(keys) > 1, "action": action,
                               "teacherIds": candidate["teacherIds"],
                               "teacherOverrideApplied": any(key in resolved.teacherOverrides for key in keys),
                               "teacherAssignments": [{"sourceCell": key, "originalTeacher": cells[key].teacher,
                                   **resolved.teacherOverrides[key].model_dump()} for key in keys if key in resolved.teacherOverrides]})
        covered.update(keys)

    for key, reason in resolved.excludedCells.items():
        skipped.append({"sourceCell": key, "reason": reason})
        block("source_cell_excluded", [key], "Назначение исключено из расчёта: " + reason)
    from .school_source_rules import apply_source_rules
    rules = apply_source_rules(source.model_dump(mode="json"), resolved.model_dump(mode="json"), plan, provenance)
    patterns = apply_count_patterns(source, rules['input'], provenance)
    plan = patterns['input']
    unresolved.extend(patterns['unresolved'])
    compiled_conditions = {item["conditionId"] for item in rules["appliedConditions"]}
    rule_details = {}
    for issue in rules["unresolved"]:
        rule_details.setdefault(issue["conditionId"], []).append(issue["message"])
        if issue["code"] in {"source_rule_empty_domain", "source_rule_required_load",
                             "source_rule_effective_date_required", "source_rule_update_stale"}:
            # An empty slot intersection must never become unrestricted via []
            # or be cleared by an unrelated manual attestation.
            unresolved.append(deepcopy(issue))
    # A manual attestation is bound to BOTH this full model and exact source
    # package. Reusing a condition id after editing evidence does not preserve
    # its approval. Legacy records without packageHash are untrusted.
    plan["confirmed"] = False
    input_hash = _digest(PlanningInput.model_validate(plan).model_dump(mode="json"))
    resolved_conditions = []
    for condition in conditions:
        if condition["id"] in compiled_conditions:
            continue
        attestation = resolved.sourceConditions.get(condition["id"])
        if attestation and attestation.planHash == input_hash and attestation.packageHash == package_hash:
            resolved_conditions.append({"conditionId": condition["id"], **attestation.model_dump()})
        else:
            unresolved.append({"code": "stale_condition_attestation" if attestation else "source_condition_uncompiled", "sourceCells": [],
                               "conditionId": condition["id"], "source": condition.get("source"),
                               "message": condition["text"],
                               "clarification": " ".join(rule_details.get(condition["id"], [])) + " Проверьте выполнение этого конкретного условия в модели и календаре; сохраните ручную аттестацию с причиной, текущими inputHash и packageHash."})
    row_provenance = []
    for key, row in unallocated_rows.items():
        row_resolution = resolved.unallocatedRows.get(key)
        row_error = None
        if row_resolution is None:
            row_error = "Подтвердите, является ли строка итогом без дополнительных часов, или укажите назначения, распределяющие её нагрузку."
        elif row_resolution.mode == "allocated":
            linked = [demands[demand_id] for demand_id in row_resolution.demandIds if demand_id in demands]
            teacher_id = resolved.teacherMap.get(row["teacher"])
            expected_count = row_resolution.weeklyCount
            if expected_count is None and type(row.get("rawTotal")) is int:
                expected_count = row["rawTotal"]
            expected_subjects = set()
            if row["rawSubject"] == "Родной язык":
                expected_subjects = {"Гос. язык РТ"} if row["teacher"] in {"Мельникова Н.И.", "Гилазетдинова Г.Г."} else {"Родной русский язык", "Родная русская литература"}
            else:
                for demand in linked:
                    for group_id in demand["groupIds"]:
                        expected_subjects.add(normalise_subject_name(groups[group_id]["className"], row["rawSubject"]))
            if len(linked) != len(row_resolution.demandIds):
                row_error = "Указанные назначения строки не найдены в текущей модели."
            elif not teacher_id or any(teacher_id not in demand["teacherIds"] for demand in linked):
                row_error = "Назначения строки должны содержать её явно сопоставленного фактического педагога."
            elif any(canonical(demand["subject"]) not in {canonical(subject) for subject in expected_subjects} for demand in linked):
                row_error = "Предметы указанных назначений не соответствуют исходной строке."
            elif expected_count is None:
                row_error = "Подтвердите weeklyCount строки: дробный итог нельзя интерпретировать автоматически."
            elif sum(demand["weeklyCount"] for demand in linked) != expected_count:
                row_error = "Сумма занятий по ссылкам не равна подтверждённой нагрузке строки."
        if row_error:
            unresolved.append({"code": "unallocated_source_row", "sourceCells": [],
                               "row": row["row"], "message": f"{row['teacher']}, {row['rawSubject']}, итог {row.get('rawTotal')}: {row_error}"})
        else:
            row_provenance.append({"row": row["row"], **row_resolution.model_dump(mode="json")})
    unresolved_cells = sorted(cells.keys() - covered - resolved.excludedCells.keys())
    coverage = {"sourceCellCount": len(cells), "compiledCellCount": len(covered),
                "excludedCellCount": len(skipped), "unresolvedCellCount": len(unresolved_cells),
                "unresolvedCount": len(unresolved), "conditionCount": len(source.conditions),
                "compiledConditionCount": len(compiled_conditions),
                "partiallyCompiledConditionCount": len(rules["partialConditions"]),
                "methodDayConditionCount": len(conditions) - len(source.conditions),
                "totalConditionCount": len(conditions),
                "attestedConditionCount": sum(not item["conditionId"].startswith("tariff-methoddays-row-") for item in resolved_conditions),
                "attestedMethodDayConditionCount": sum(item["conditionId"].startswith("tariff-methoddays-row-") for item in resolved_conditions),
                "unallocatedRowCount": len(unallocated_rows), "resolvedUnallocatedRowCount": len(row_provenance),
                "addedDemandCount": len(added), "reusedDemandCount": len(reused),
                "compiledWeeklyEvents": sum(item["weeklyCount"] for item in provenance),
                "unresolvedCells": unresolved_cells,
                "allSourceCellsAccounted": not unresolved_cells,
                "complete": not unresolved and not skipped}
    # Preview imports must be reviewed explicitly even when every binding was
    # supplied. Incomplete source facts can never masquerade as a confirmed plan.
    return {"packageId": source.packageId, "packageHash": package_hash,
            "inputHash": input_hash,
            "input": PlanningInput.model_validate(plan).model_dump(mode="json"),
            "unresolved": unresolved, "coverage": coverage, "provenance": provenance,
            "excluded": skipped, "addedDemandIds": added, "reusedDemandIds": reused,
            "resolvedConditions": resolved_conditions,
            "appliedConditions": rules["appliedConditions"], "partialConditions": rules["partialConditions"],
            "sessionPatterns": patterns['applied'],
            "rowProvenance": row_provenance,
            "resolutions": resolved.model_dump(mode="json"),
            "notice": "Предпросмотр добавляет только подтверждённые назначения. Существующая модель не перезаписывается; сохранение выполняет вызывающий сервис."}
