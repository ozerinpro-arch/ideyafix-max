"""Strict, bounded planning inputs. No school facts are inferred here."""
from __future__ import annotations

from datetime import date
from typing import Annotated, Literal
import unicodedata

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationInfo, field_validator, model_validator

MAX_EVENTS = 2000
MAX_CANDIDATES = 250000
MAX_SLOTS = 100
MAX_TEACHERS = 300
MAX_ROOMS = 300
MAX_GROUPS = 200
SCHOOL_DAYS = 6

Identifier = Annotated[str, StringConstraints(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")]
Name = Annotated[str, StringConstraints(min_length=1, max_length=120)]
Subject = Annotated[str, StringConstraints(min_length=1, max_length=160)]
Tag = Annotated[str, StringConstraints(min_length=1, max_length=80)]


def canonical(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def minutes(value: str) -> int:
    hour, minute = map(int, value.split(":"))
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("Некорректное время")
    return hour * 60 + minute


def _check_text(value):
    if isinstance(value, str):
        if any(unicodedata.category(char) in {"Cc", "Cs"} for char in value):
            raise ValueError("Управляющие символы и некорректный Unicode запрещены")
    elif isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, (list, tuple)):
                raise ValueError("Вложенные списки не входят в модель планирования")
            _check_text(item)


def _unique(values, label, *, normalize=False):
    keys = [canonical(value) if normalize else value for value in values]
    if len(keys) != len(set(keys)):
        raise ValueError(f"Повторяющиеся {label}")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True, revalidate_instances="always")

    @field_validator("*", mode="before")
    @classmethod
    def safe_strings(cls, value):
        _check_text(value)
        return value


class Slot(StrictModel):
    id: Identifier
    day: int = Field(ge=0, le=SCHOOL_DAYS - 1)
    slot: int = Field(ge=1, le=20)
    start: str = Field(pattern=r"^[0-9]{2}:[0-9]{2}$")
    end: str = Field(pattern=r"^[0-9]{2}:[0-9]{2}$")

    @model_validator(mode="after")
    def interval(self):
        if minutes(self.start) >= minutes(self.end):
            raise ValueError("Окончание слота должно быть позже начала")
        return self


class Group(StrictModel):
    id: Identifier
    className: str = Field(min_length=1, max_length=80)
    name: Name
    size: int = Field(ge=1, le=1000)
    kind: Literal["whole", "subgroup"]
    partition: str = Field(default="", max_length=80)
    maxDaily: int | None = Field(default=None, ge=1, le=20)
    unavailableSlotIds: list[Identifier] = Field(default_factory=list, max_length=MAX_SLOTS)
    requiredBreaks: list[WorkWindow] = Field(default_factory=list, max_length=100)
    minTransitionMinutes: int = Field(default=0, ge=0, le=180)
    attendanceUnitIds: list[Identifier] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def unique_availability(self):
        _unique(self.unavailableSlotIds, "слоты недоступности группы")
        _unique(self.attendanceUnitIds, "атомарные единицы состава группы")
        return self


class WorkWindow(StrictModel):
    day: int = Field(ge=0, le=SCHOOL_DAYS - 1)
    start: str = Field(pattern=r"^[0-9]{2}:[0-9]{2}$")
    end: str = Field(pattern=r"^[0-9]{2}:[0-9]{2}$")

    @model_validator(mode="after")
    def interval(self):
        if minutes(self.start) >= minutes(self.end):
            raise ValueError("Окончание рабочего окна должно быть позже начала")
        return self


class Teacher(StrictModel):
    id: Identifier
    name: Name
    subjects: list[Subject] = Field(max_length=100)
    unavailableSlotIds: list[Identifier] = Field(max_length=MAX_SLOTS)
    maxDaily: int = Field(ge=1, le=20)
    maxWeekly: int = Field(ge=1, le=100)
    maxConsecutive: int = Field(ge=1, le=20)
    # Empty means no additional window restriction, never inferred from baseline.
    workWindows: list[WorkWindow] = Field(default_factory=list, max_length=100)
    # Explicit duties/meetings that require presence and prohibit lessons.
    presenceEvents: list[WorkWindow] = Field(default_factory=list, max_length=100)
    requiredBreaks: list[WorkWindow] = Field(default_factory=list, max_length=100)
    minTransitionMinutes: int = Field(default=0, ge=0, le=180)
    maxDailySpanMinutes: int | None = Field(default=None, ge=1, le=1440)
    maxDailyGapMinutes: int | None = Field(default=None, ge=0, le=1440)

    @model_validator(mode="after")
    def unique_lists(self):
        _unique(self.subjects, "предметы преподавателя", normalize=True)
        _unique(self.unavailableSlotIds, "слоты недоступности преподавателя")
        return self


class Room(StrictModel):
    id: Identifier
    name: str = Field(min_length=1, max_length=80)
    capacity: int = Field(ge=1, le=5000)
    tags: list[Tag] = Field(max_length=100)
    unavailableSlotIds: list[Identifier] = Field(max_length=MAX_SLOTS)

    @model_validator(mode="after")
    def unique_lists(self):
        _unique(self.tags, "метки кабинета", normalize=True)
        _unique(self.unavailableSlotIds, "слоты недоступности кабинета")
        return self


class RoomTransition(StrictModel):
    fromRoomId: Identifier
    toRoomId: Identifier
    minMinutes: int = Field(ge=0, le=180)


class Demand(StrictModel):
    # _39 adds three characters; all generated event IDs remain <=80.
    id: str = Field(min_length=1, max_length=77, pattern=r"^[A-Za-z0-9_-]+$")
    subject: Subject
    groupIds: list[Identifier] = Field(min_length=1, max_length=MAX_GROUPS)
    teacherIds: list[Identifier] = Field(min_length=1, max_length=8)
    roomIds: list[Identifier] = Field(min_length=1, max_length=MAX_ROOMS)
    weeklyCount: int = Field(ge=1, le=40)
    requiredTags: list[Tag] = Field(default_factory=list, max_length=100)
    allowedSlotIds: list[Identifier] = Field(default_factory=list, max_length=MAX_SLOTS)
    maxPerDay: int = Field(default=20, ge=1, le=20)
    # Inactive extensions retain the exact normalized form of saved models.
    teachingDays: int | None = Field(default=None, ge=1, le=SCHOOL_DAYS,
                                   exclude_if=lambda value: value is None)
    syncKey: str = Field(default="", max_length=80)
    sourceLessonIds: list[Identifier] = Field(default_factory=list, max_length=40)
    # Inclusive dates. Partial weeks retain the explicit weeklyCount; they are
    # never prorated or rounded by the solver/calendar overlay.
    activeFrom: str | None = Field(default=None, pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    activeUntil: str | None = Field(default=None, pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    weekTypes: list[Tag] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def unique_lists(self):
        for field in ("groupIds", "teacherIds", "roomIds", "allowedSlotIds", "sourceLessonIds"):
            _unique(getattr(self, field), field)
        _unique(self.requiredTags, "необходимые метки", normalize=True)
        _unique(self.weekTypes, "типы недель нагрузки", normalize=True)
        for value in (self.activeFrom, self.activeUntil):
            if value is not None:
                date.fromisoformat(value)
        if self.activeFrom is not None and self.activeUntil is not None and self.activeFrom > self.activeUntil:
            raise ValueError("Начало действия нагрузки должно быть не позже окончания")
        if self.sourceLessonIds and len(self.sourceLessonIds) != self.weeklyCount:
            raise ValueError("sourceLessonIds должен быть пустым или содержать запись на каждое занятие")
        if self.teachingDays is not None and self.teachingDays > self.weeklyCount:
            raise ValueError("Число учебных дней нагрузки не может превышать число занятий")
        return self


class Assignment(StrictModel):
    eventId: Identifier
    slotId: Identifier
    roomId: Identifier


def eligible_room_ids(demand: Demand, groups: dict[str, Group], rooms: dict[str, Room],
                      room_tags: dict[str, set[str]], *, check=lambda: None) -> list[str]:
    """Static room domain, shared by the input bound and candidate builder.

    Availability can only narrow this domain. Independent result validation
    still checks capacity and tags directly, without trusting this helper.
    """
    required_size = sum(groups[group_id].size for group_id in demand.groupIds)
    required_tags = {canonical(tag) for tag in demand.requiredTags}
    eligible = []
    for room_id in demand.roomIds:
        check()
        if rooms[room_id].capacity >= required_size and required_tags <= room_tags[room_id]:
            eligible.append(room_id)
    return eligible


class EventChain(StrictModel):
    id: Identifier
    eventIds: list[Identifier] = Field(min_length=2, max_length=40)
    sameDay: bool = True
    maxGapMinutes: int | None = Field(default=None, ge=0, le=7200)
    consecutiveSlots: bool = Field(default=False, exclude_if=lambda value: value is False)
    unorderedPair: bool = Field(default=False, exclude_if=lambda value: value is False)

    @model_validator(mode="after")
    def unique_events(self):
        _unique(self.eventIds, "события упорядоченной связки")
        if self.consecutiveSlots and not self.sameDay:
            raise ValueError("Последовательные номера уроков требуют одного учебного дня")
        if self.unorderedPair and (len(self.eventIds) != 2 or not self.sameDay or not self.consecutiveSlots):
            raise ValueError("Неупорядоченная пара требует двух занятий подряд в одном дне")
        return self


class Preferences(StrictModel):
    earlyWeight: int = Field(default=1, ge=0, le=100)
    changeWeight: int = Field(default=100, ge=0, le=1000)
    roomChangeWeight: int = Field(default=10, ge=0, le=100)
    gapWeight: int = Field(default=10, ge=0, le=100)
    spanWeight: int = Field(default=1, ge=0, le=100)
    dayWeight: int = Field(default=0, ge=0, le=100)


def groups_overlap(first: Group, second: Group) -> bool:
    if canonical(first.className) != canonical(second.className):
        return False
    if first.kind == second.kind == "subgroup" and first.attendanceUnitIds and second.attendanceUnitIds:
        return bool(set(first.attendanceUnitIds) & set(second.attendanceUnitIds))
    return not (first.id != second.id and first.kind == second.kind == "subgroup"
                and first.partition and first.partition == second.partition)


class PlanningInput(StrictModel):
    periodStart: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    weekType: str = Field(default='', max_length=80)
    confirmed: bool = False
    slots: list[Slot] = Field(max_length=MAX_SLOTS)
    groups: list[Group] = Field(max_length=MAX_GROUPS)
    teachers: list[Teacher] = Field(max_length=MAX_TEACHERS)
    rooms: list[Room] = Field(max_length=MAX_ROOMS)
    demands: list[Demand] = Field(max_length=MAX_EVENTS)
    baseline: list[Assignment] = Field(default_factory=list, max_length=MAX_EVENTS)
    pins: list[Assignment] = Field(default_factory=list, max_length=MAX_EVENTS)
    eventChains: list[EventChain] = Field(default_factory=list, max_length=MAX_EVENTS)
    roomTransitions: list[RoomTransition] = Field(default_factory=list, max_length=1000)
    preferences: Preferences = Field(default_factory=Preferences)
    notes: str = Field(default="", max_length=2000)

    @field_validator("periodStart")
    @classmethod
    def monday(cls, value):
        if date.fromisoformat(value).weekday() != 0:
            raise ValueError("Начало периода должно приходиться на понедельник")
        return value

    @model_validator(mode="after")
    def references_and_bounds(self, info: ValidationInfo):
        check = (info.context or {}).get("scheduling_check", lambda: None)
        check()
        for label in ("slots", "groups", "teachers", "rooms", "demands"):
            _unique([item.id for item in getattr(self, label)], f"идентификаторы {label}")
        _unique([f"{item.day}:{item.slot}" for item in self.slots], "номера слотов внутри дня")
        for label in ("teachers", "rooms"):
            _unique([item.name for item in getattr(self, label)], f"имена {label}", normalize=True)
        _unique([canonical(item.className) + "\x00" + canonical(item.name) for item in self.groups], "имена групп")
        slots, groups = {item.id for item in self.slots}, {item.id: item for item in self.groups}
        group_classes = {item.id: canonical(item.className) for item in self.groups}
        teachers, rooms = {item.id for item in self.teachers}, {item.id for item in self.rooms}
        room_by_id = {item.id: item for item in self.rooms}
        room_tags = {item.id: {canonical(tag) for tag in item.tags} for item in self.rooms}
        for first in self.groups:
            for second in self.groups:
                check()
                if first.id >= second.id or canonical(first.className) != canonical(second.className):
                    continue
                if (first.kind == second.kind == "subgroup" and first.partition and first.partition == second.partition
                        and set(first.attendanceUnitIds) & set(second.attendanceUnitIds)):
                    raise ValueError(f"Непересекающееся разбиение содержит общую единицу состава: {first.id}, {second.id}")
            if first.kind == "whole" and first.attendanceUnitIds:
                for second in self.groups:
                    if (canonical(first.className) == canonical(second.className) and second.kind == "subgroup"
                            and not set(second.attendanceUnitIds) <= set(first.attendanceUnitIds)):
                        raise ValueError(f"Полный класс не содержит состав подгруппы: {first.id}, {second.id}")
        _unique([f"{item.fromRoomId}:{item.toRoomId}" for item in self.roomTransitions], "направленные переходы кабинетов")
        if any(item.fromRoomId not in rooms or item.toRoomId not in rooms for item in self.roomTransitions):
            raise ValueError("Переход ссылается на неизвестный кабинет")
        for item in [*self.teachers, *self.rooms, *self.groups]:
            check()
            if not set(item.unavailableSlotIds) <= slots:
                raise ValueError("Недоступность ссылается на неизвестный слот")
        expanded = sum(item.weeklyCount for item in self.demands)
        if expanded > MAX_EVENTS:
            raise ValueError("Превышен предел 2000 занятий")
        combinations = 0
        sync_counts, source_ids = {}, []
        for item in self.demands:
            check()
            if (not set(item.groupIds) <= groups.keys() or not set(item.teacherIds) <= teachers
                    or not set(item.roomIds) <= rooms or not set(item.allowedSlotIds) <= slots):
                raise ValueError("Нагрузка ссылается на неизвестную группу, преподавателя, кабинет или слот")
            selected_groups = [groups[group_id] for group_id in item.groupIds]
            for index, first in enumerate(selected_groups):
                check()
                if any(groups_overlap(first, second) for second in selected_groups[index + 1:]):
                    raise ValueError("Одно занятие содержит пересекающиеся группы учеников")
            if item.syncKey:
                previous_count = sync_counts.setdefault(item.syncKey, item.weeklyCount)
                if previous_count != item.weeklyCount:
                    raise ValueError("Синхронные нагрузки должны иметь одинаковое число занятий")
            room_count = len(eligible_room_ids(item, groups, room_by_id, room_tags, check=check))
            combinations += item.weeklyCount * len(item.allowedSlotIds or self.slots) * room_count
            source_ids.extend(item.sourceLessonIds)
        if combinations > MAX_CANDIDATES:
            raise ValueError("Превышен предел 250000 вариантов назначения; сузьте слоты или кабинеты")
        _unique(source_ids, "исходные уроки между нагрузками")
        events = {f"{item.id}_{occurrence}" for item in self.demands for occurrence in range(item.weeklyCount)}
        _unique([item.id for item in self.eventChains], "идентификаторы связок")
        if sum(len(item.eventIds) for item in self.eventChains) > MAX_EVENTS * 2:
            raise ValueError("Превышен предел 4000 ссылок в связках событий")
        for chain in self.eventChains:
            check()
            if not set(chain.eventIds) <= events:
                raise ValueError("Связка ссылается на неизвестное событие")
        for label in ("baseline", "pins"):
            assignments = getattr(self, label)
            _unique([item.eventId for item in assignments], f"события {label}")
            for item in assignments:
                check()
                if item.eventId not in events or item.slotId not in slots or item.roomId not in rooms:
                    raise ValueError(f"Неизвестная ссылка в {label}")
        return self


def planning_readiness(value: PlanningInput | dict | None, *, context: dict | None = None) -> dict:
    """Structural readiness only; feasibility is established by CP-SAT."""
    if value is None:
        return {"ready": False, "blockers": [{"code": "missing_plan", "message": "Заполните нормативный план; импортированная сетка не заменяет ограничения.", "entityIds": []}], "warnings": []}
    plan = PlanningInput.model_validate(value, context=context)
    blockers = []
    if not plan.confirmed:
        blockers.append({"code": "unconfirmed", "message": "Подтвердите группы, вместимость, квалификацию, доступность и ограничения нагрузки.", "entityIds": []})
    labels = {"slots": "учебные слоты", "groups": "группы учеников", "teachers": "преподавателей и ограничения", "rooms": "кабинеты и вместимость", "demands": "недельную учебную нагрузку"}
    for field, label in labels.items():
        if not getattr(plan, field):
            blockers.append({"code": f"missing_{field}", "message": f"Заполните {label}.", "entityIds": []})
    missing_daily = [group.id for group in plan.groups if group.maxDaily is None]
    if missing_daily:
        blockers.append({"code": "missing_group_daily_limit", "message": "Укажите подтверждённый дневной предел уроков для каждой группы учеников.", "entityIds": missing_daily})
    unresolved = [demand.id for demand in plan.demands if demand.weekTypes and not plan.weekType]
    if unresolved:
        blockers.append({"code": "unresolved_week_type", "message": "Укажите явный тип выбранной недели; чётность не определяется автоматически.", "entityIds": unresolved})
    for class_name in {canonical(group.className) for group in plan.groups}:
        check = (context or {}).get("scheduling_check", lambda: None)
        check()
        roster = [group for group in plan.groups if canonical(group.className) == class_name]
        class_ids = {group.id for group in roster}
        class_demands = [demand for demand in plan.demands if class_ids.intersection(demand.groupIds)]
        used_ids = {group_id for demand in class_demands for group_id in demand.groupIds}
        roster = [group for group in roster if group.kind == "whole" or group.id in used_ids or group.minTransitionMinutes or group.requiredBreaks]
        room_counts, room_demand = {}, {}
        for demand in class_demands:
            check()
            for room_id in demand.roomIds:
                room_counts[room_id] = room_counts.get(room_id, 0) + demand.weeklyCount
                room_demand[room_id] = demand.id if room_id not in room_demand else None
        reachable_rules = any(rule.minMinutes and room_counts.get(rule.fromRoomId, 0) and room_counts.get(rule.toRoomId, 0)
                              and (room_counts[rule.fromRoomId] > 1 or room_counts[rule.toRoomId] > 1
                                   or room_demand[rule.fromRoomId] != room_demand[rule.toRoomId]) for rule in plan.roomTransitions)
        if sum(demand.weeklyCount for demand in class_demands) < 2 or not (reachable_rules or any(group.minTransitionMinutes for group in roster)):
            continue
        subgroups = [group for group in roster if group.kind == "subgroup"]
        whole = [group for group in roster if group.kind == "whole"]
        mixed = len(subgroups) > 1 and (len({group.partition for group in subgroups}) > 1 or any(not group.partition for group in subgroups))
        missing = ([group.id for group in roster if not group.attendanceUnitIds] if mixed
                   else [group.id for group in subgroups if not group.attendanceUnitIds] if any(group.attendanceUnitIds for group in whole) else [])
        if missing:
            blockers.append({"code": "missing_attendance_units", "message": "Для переходов между пересекающимися разбиениями нужен явный обезличенный состав подгрупп и полного класса.", "entityIds": missing})
        elif whole and subgroups and not mixed and not any(group.attendanceUnitIds for group in roster) and any(group.size < sum(item.size for item in subgroups) for group in whole):
            blockers.append({"code": "group_membership_size_conflict", "message": "Сумма непересекающихся подгрупп больше полного класса; состав маршрутов не подтверждён.", "entityIds": [group.id for group in roster]})
    return {"ready": not blockers, "blockers": blockers, "warnings": []}
