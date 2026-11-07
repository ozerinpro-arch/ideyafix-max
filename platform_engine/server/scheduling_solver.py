"""Local CP-SAT scheduling and an independent, loop-based result validator."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
import hashlib
import json
import math
import time

from pydantic import ValidationError

from .scheduling_models import Assignment, MAX_EVENTS, SCHOOL_DAYS, PlanningInput, canonical, eligible_room_ids, minutes, planning_readiness

ISSUE_LIMIT = 256
TRANSITION_CONSTRAINT_LIMIT = 250000
MAX_SOLVE_SECONDS = 1800


class _BudgetExceeded(RuntimeError):
    def __init__(self, budget):
        self.budget = budget


class _TransitionLimitExceeded(_BudgetExceeded):
    pass


class _Deadline:
    def __init__(self, seconds, *, started=None, metrics=None, progress_callback=None):
        self.started = time.monotonic() if started is None else started
        self.end = self.started + seconds
        self.metrics = metrics
        self.ticks = 0
        self.fallback_result = None
        self.progress_callback = progress_callback

    def check(self):
        if time.monotonic() >= self.end:
            raise _BudgetExceeded(self)

    def tick(self):
        self.ticks += 1
        if self.ticks % 64 == 0:
            self.check()

    def stage(self, name):
        if self.metrics is not None:
            self.metrics["phase"] = name
        self.check()
        if self.progress_callback is not None:
            progress = {'phase': name, 'elapsedSeconds': round(time.monotonic() - self.started, 6),
                        'timeLimitSeconds': self.end - self.started}
            if self.metrics is not None:
                progress['searchWorkers'] = self.metrics.get('searchWorkers')
            try:
                self.progress_callback(progress)
            except Exception:
                # Progress persistence is advisory and cannot invalidate a
                # candidate or consume permission to relax any hard condition.
                pass

    def remaining(self):
        return max(0.0, self.end - time.monotonic())


def validate_solution(value: PlanningInput | dict, assignments: list[dict | Assignment], *, time_limit: float = 5) -> dict:
    """Independent verification has its own bounded, fail-closed wall budget."""
    if (isinstance(time_limit, bool) or not isinstance(time_limit, (int, float))
            or not 0 <= time_limit <= 30 or not math.isfinite(time_limit)):
        return {"valid": False, "issues": [{"code": "invalid_validation_limit", "message": "Некорректный предел времени проверки.", "entityIds": []}]}
    budget = _Deadline(float(time_limit))
    try:
        return _validate_solution(value, assignments, budget)
    except _BudgetExceeded:
        return {"valid": False, "issues": [{"code": "validation_time_limit", "message": "Независимая проверка не завершена в пределах времени; кандидат не подтверждён.", "entityIds": []}]}


def _validate_solution(value: PlanningInput | dict, assignments: list[dict | Assignment], budget: _Deadline) -> dict:
    """Check every hard constraint without using CP-SAT or its constraints.

    Consecutive means adjacent ordinal lesson numbers within the same day.
    Missing ordinal numbers break a run; real interval overlap is checked
    independently. Availability IDs denote the full interval of that slot.
    """
    issues = []
    omitted = 0

    def add(code, message, ids=()):
        nonlocal omitted
        if len(issues) < ISSUE_LIMIT:
            issues.append({"code": code, "message": message, "entityIds": sorted(set(ids))[:MAX_EVENTS]})
        else:
            omitted += 1

    try:
        budget.stage("validation_input")
        plan = PlanningInput.model_validate(value, context={"scheduling_check": budget.check})
    except (ValidationError, TypeError, ValueError):
        return {"valid": False, "issues": [{"code": "invalid_input", "message": "Некорректная модель планирования.", "entityIds": []}]}
    if not plan.confirmed:
        add("unconfirmed", "Исходные ограничения не подтверждены оператором.")
    for blocker in planning_readiness(plan, context={"scheduling_check": budget.check})["blockers"]:
        if blocker["code"] in {"missing_attendance_units", "group_membership_size_conflict"}:
            add(blocker["code"], blocker["message"], blocker["entityIds"])
    for group in plan.groups:
        if group.maxDaily is None:
            add("missing_group_daily_limit", "Не подтверждён дневной предел учебной нагрузки группы.", [group.id])
    if not isinstance(assignments, list) or len(assignments) > MAX_EVENTS * 2:
        add("invalid_assignments", "Некорректный или слишком большой список назначений.")
        return {"valid": False, "issues": issues}
    slots = {item.id: item for item in plan.slots}
    groups = {item.id: item for item in plan.groups}
    teachers = {item.id: item for item in plan.teachers}
    rooms = {item.id: item for item in plan.rooms}
    budget.stage("validation_indexes")
    group_classes = {item.id: canonical(item.className) for item in plan.groups}
    related_groups = {}
    for first in plan.groups:
        budget.check()
        # Independent student relation; do not call solver/model predicates.
        related_groups[first.id] = {second.id for second in plan.groups if group_classes[first.id] == group_classes[second.id]
                                   and not (first.id != second.id and first.kind == second.kind == "subgroup"
                                            and ((first.attendanceUnitIds and second.attendanceUnitIds and set(first.attendanceUnitIds).isdisjoint(second.attendanceUnitIds))
                                                 or (not (first.attendanceUnitIds and second.attendanceUnitIds) and bool(first.partition) and first.partition == second.partition)))}
    slot_overlaps = {}
    for blocked in plan.slots:
        budget.check()
        slot_overlaps[blocked.id] = {current.id for current in plan.slots if current.day == blocked.day and current.start < blocked.end and blocked.start < current.end}
    availability_cache = {}

    def unavailable(entity):
        key = tuple(sorted(entity.unavailableSlotIds))
        if key not in availability_cache:
            blocked_slots = set()
            for blocked_id in key:
                budget.tick()
                blocked_slots.update(slot_overlaps[blocked_id])
            availability_cache[key] = blocked_slots
        return availability_cache[key]

    group_blocked, teacher_blocked, room_blocked = {}, {}, {}
    for entities, destination in ((plan.groups, group_blocked), (plan.teachers, teacher_blocked), (plan.rooms, room_blocked)):
        for entity in entities:
            budget.check()
            destination[entity.id] = unavailable(entity)
    qualifications = {item.id: {canonical(subject) for subject in item.subjects} for item in plan.teachers}
    room_tags = {item.id: {canonical(tag) for tag in item.tags} for item in plan.rooms}
    demand_data = {}
    for demand in plan.demands:
        budget.check()
        affected = set()
        for group_id in demand.groupIds:
            budget.tick()
            affected.update(related_groups[group_id])
        demand_data[demand.id] = {"groups": set(demand.groupIds), "teachers": set(demand.teacherIds), "affected": affected,
                                  "blockedGroups": [(group_id, group_blocked[group_id]) for group_id in affected if group_blocked[group_id]],
                                  "size": sum(groups[group_id].size for group_id in demand.groupIds),
                                  "subject": canonical(demand.subject), "tags": {canonical(tag) for tag in demand.requiredTags}}
    # Expand separately from solver construction; validate exact coverage.
    expected = {}
    for demand in plan.demands:
        budget.check()
        for occurrence in range(demand.weeklyCount):
            expected[f"{demand.id}_{occurrence}"] = (demand, occurrence)
    if not expected:
        add("empty_plan", "План не содержит учебной нагрузки.")
    seen, accepted = set(), {}
    for index, supplied in enumerate(assignments):
        budget.check()
        try:
            assignment = Assignment.model_validate(supplied)
        except (ValidationError, ValueError, TypeError):
            add("invalid_assignment", "Назначение содержит некорректные поля.", [str(index)])
            continue
        event_id = assignment.eventId
        if event_id in seen:
            add("duplicate_event", "Занятие назначено несколько раз.", [event_id])
            continue
        seen.add(event_id)
        if event_id not in expected:
            add("unknown_event", "Назначено неизвестное занятие.", [event_id])
            continue
        demand, occurrence = expected[event_id]
        if assignment.slotId not in slots or assignment.roomId not in rooms:
            add("unknown_resource", "Неизвестный слот или кабинет назначения.", [event_id])
            continue
        slot, room = slots[assignment.slotId], rooms[assignment.roomId]
        data = demand_data[demand.id]
        accepted[event_id] = (assignment, demand, slot, occurrence)
        if demand.allowedSlotIds and slot.id not in demand.allowedSlotIds:
            add("slot_domain", "Занятие назначено вне разрешённых слотов.", [event_id, slot.id])
        if demand.weekTypes and canonical(plan.weekType) not in {canonical(kind) for kind in demand.weekTypes}:
            add("demand_week_type", "Тип выбранной недели не соответствует нагрузке.", [event_id])
        if demand.activeFrom or demand.activeUntil:
            lesson_date = (date.fromisoformat(plan.periodStart) + timedelta(days=slot.day)).isoformat()
            if (demand.activeFrom and lesson_date < demand.activeFrom) or (demand.activeUntil and lesson_date > demand.activeUntil):
                add("demand_active_dates", "Занятие назначено вне дат действия нагрузки.", [event_id, slot.id])
        if room.id not in demand.roomIds:
            add("room_domain", "Кабинет не входит в альтернативы занятия.", [event_id, room.id])
        if room.capacity < data["size"]:
            add("capacity", "Вместимость кабинета меньше числа учеников.", [event_id, room.id])
        if not data["tags"] <= room_tags[room.id]:
            add("room_tags", "В кабинете отсутствует необходимое оснащение.", [event_id, room.id])
        for group_id, blocked_slots in data["blockedGroups"]:
            budget.tick()
            if slot.id in blocked_slots:
                add("group_unavailable", "Занятие пересекает недоступность учеников группы.", [event_id, group_id])
        for group_id in data["affected"]:
            if any(rest.day == slot.day and rest.start < slot.end and slot.start < rest.end for rest in groups[group_id].requiredBreaks):
                add("group_required_break", "Урок пересекается с обязательным перерывом учеников.", [event_id, group_id])
        for teacher_id in demand.teacherIds:
            teacher = teachers[teacher_id]
            if data["subject"] not in qualifications[teacher_id]:
                add("qualification", "Не подтверждена квалификация участвующего преподавателя.", [event_id, teacher_id])
            if slot.id in teacher_blocked[teacher_id]:
                add("teacher_unavailable", "Назначение пересекает недоступность преподавателя.", [event_id, teacher_id])
            if teacher.workWindows and not any(window.day == slot.day and window.start <= slot.start
                                               and slot.end <= window.end for window in teacher.workWindows):
                add("teacher_work_window", "Занятие не помещается целиком в подтверждённое рабочее окно.", [event_id, teacher_id])
            if any(event.day == slot.day and event.start < slot.end and slot.start < event.end
                   for event in teacher.presenceEvents):
                add("teacher_presence_overlap", "Урок пересекается с обязательным очным событием преподавателя.", [event_id, teacher_id])
            if any(rest.day == slot.day and rest.start < slot.end and slot.start < rest.end for rest in teacher.requiredBreaks):
                add("teacher_required_break", "Урок пересекается с обязательным отдыхом преподавателя.", [event_id, teacher_id])
        if slot.id in room_blocked[room.id]:
            add("room_unavailable", "Назначение пересекает недоступность кабинета.", [event_id, room.id])
    for event_id in expected.keys() - seen:
        budget.tick()
        add("missing_event", "Занятие не назначено.", [event_id])
    for pin in plan.pins:
        budget.tick()
        current = accepted.get(pin.eventId)
        if current is None or current[0].slotId != pin.slotId or current[0].roomId != pin.roomId:
            add("pin", "Зафиксированное назначение изменено или отсутствует.", [pin.eventId])
    daily_events, pupil_days = defaultdict(list), defaultdict(set)
    teacher_events, demand_days, synced = defaultdict(list), defaultdict(list), defaultdict(list)
    for event_id, (assignment, demand, slot, occurrence) in accepted.items():
        budget.check()
        daily_events[slot.day].append((event_id, assignment, demand, slot))
        demand_days[(demand.id, slot.day)].append(event_id)
        for teacher_id in demand.teacherIds:
            teacher_events[teacher_id].append((event_id, slot))
        if demand.syncKey:
            synced[(demand.syncKey, occurrence)].append((event_id, slot.id))
        for group_id in demand_data[demand.id]["affected"]:
            budget.tick()
            pupil_days[(group_id, slot.day)].add(event_id)
    for day, events in daily_events.items():
        budget.check()
        events.sort(key=lambda item: (item[3].start, item[3].end, item[0]))
        for index, (event_id, assignment, demand, slot) in enumerate(events):
            for other_id, other_assignment, other_demand, other_slot in events[index + 1:]:
                budget.tick()
                if other_slot.start >= slot.end:
                    break
                if slot.start >= other_slot.end:
                    continue
                ids = [event_id, other_id]
                if not demand_data[demand.id]["teachers"].isdisjoint(demand_data[other_demand.id]["teachers"]):
                    add("teacher_overlap", "Преподаватель занят в пересекающихся интервалах.", ids)
                if assignment.roomId == other_assignment.roomId:
                    add("room_overlap", "Кабинет занят в пересекающихся интервалах.", ids)
                if not demand_data[demand.id]["groups"].isdisjoint(demand_data[other_demand.id]["affected"]):
                    add("group_overlap", "Пересекаются занятия одной группы учеников.", ids)
    for teacher_id, teacher in teachers.items():
        budget.check()
        events = teacher_events[teacher_id]
        if any(event.day == rest.day and event.start < rest.end and rest.start < event.end
               for event in teacher.presenceEvents for rest in teacher.requiredBreaks):
            add("presence_required_break", "Очная обязанность пересекается с обязательным отдыхом.", [teacher_id])
        if len(events) > teacher.maxWeekly:
            add("weekly_load", "Превышена недельная нагрузка преподавателя.", [teacher_id])
        for day in range(SCHOOL_DAYS):
            assigned = [(event_id, slot) for event_id, slot in events if slot.day == day]
            intervals = sorted([(minutes(slot.start), minutes(slot.end)) for _, slot in assigned]
                               + [(minutes(event.start), minutes(event.end)) for event in teacher.presenceEvents if event.day == day])
            if intervals:
                span = max(end for _, end in intervals) - intervals[0][0]
                # Compute union independently, including malformed overlapping candidates.
                occupied, until = 0, -1
                for start, end in intervals:
                    occupied += max(0, end - max(start, until))
                    until = max(until, end)
                if teacher.maxDailySpanMinutes is not None and span > teacher.maxDailySpanMinutes:
                    add("teacher_daily_span", "Превышен подтверждённый дневной размах занятости преподавателя.", [teacher_id])
                if teacher.maxDailyGapMinutes is not None and span - occupied > teacher.maxDailyGapMinutes:
                    add("teacher_daily_gaps", "Превышена подтверждённая сумма промежутков между занятиями преподавателя.", [teacher_id])
            if len(assigned) > teacher.maxDaily:
                add("daily_load", "Превышена дневная нагрузка преподавателя.", [teacher_id, *[event_id for event_id, _ in assigned]])
            run, previous = 0, -2
            for ordinal in sorted({slot.slot for _, slot in assigned}):
                run = run + 1 if ordinal == previous + 1 else 1
                previous = ordinal
                if run > teacher.maxConsecutive:
                    add("consecutive_load", "Превышено число последовательных уроков преподавателя.", [teacher_id, *[event_id for event_id, _ in assigned]])
                    break
    demands = {item.id: item for item in plan.demands}
    observed_demand_days = defaultdict(set)
    observed_demand_events = defaultdict(list)
    for (demand_id, day), event_ids in demand_days.items():
        budget.tick()
        observed_demand_days[demand_id].add(day)
        observed_demand_events[demand_id].extend(event_ids)
        if len(event_ids) > demands[demand_id].maxPerDay:
            add("demand_daily", "Превышено дневное число занятий одной нагрузки.", [demand_id, *event_ids])
    for demand in plan.demands:
        budget.tick()
        if demand.teachingDays is not None and len(observed_demand_days[demand.id]) != demand.teachingDays:
            add("demand_teaching_days", "Не соблюдено точное число учебных дней нагрузки.",
                [demand.id, *observed_demand_events[demand.id]])
    for entries in synced.values():
        budget.tick()
        if len({slot_id for _, slot_id in entries}) > 1:
            add("sync", "Синхронные занятия назначены в разные слоты.", [event_id for event_id, _ in entries])
    for chain in plan.eventChains:
        budget.check()
        for first_id, second_id in zip(chain.eventIds, chain.eventIds[1:]):
            if first_id not in accepted or second_id not in accepted:
                continue  # Coverage already reports missing events.
            first, second = accepted[first_id][2], accepted[second_id][2]
            if chain.unorderedPair and (second.day, second.start) < (first.day, first.start):
                first, second = second, first
            gap = second.day * 1440 + minutes(second.start) - (first.day * 1440 + minutes(first.end))
            if (gap < 0 or (chain.sameDay and first.day != second.day)
                    or (chain.maxGapMinutes is not None and gap > chain.maxGapMinutes)
                    or (chain.consecutiveSlots and second.slot != first.slot + 1)):
                add("event_chain", "Нарушены порядок, день или промежуток явно связанной пары занятий.", [chain.id, first_id, second_id])
    classes = defaultdict(list)
    for group in plan.groups:
        classes[canonical(group.className)].append(group)
    validation_indexes = _validation_route_indexes(accepted, budget) if any(group.attendanceUnitIds for group in plan.groups) else None
    for class_groups in classes.values():
        budget.check()
        explicit_parts = [group for group in class_groups if group.kind == "subgroup"]
        if explicit_parts and all(group.attendanceUnitIds for group in explicit_parts):
            rows, _teacher_index, group_index = validation_indexes
            unit_members = defaultdict(list)
            whole = [group for group in class_groups if group.kind == "whole"]
            for group in explicit_parts:
                for unit in group.attendanceUnitIds:
                    budget.tick()
                    unit_members[unit].append(group)
            seen_patterns = set()
            for members in unit_members.values():
                budget.check()
                pattern = tuple(sorted(group.id for group in members))
                if pattern in seen_patterns:
                    continue
                seen_patterns.add(pattern)
                owners = whole + members
                owner_ids = {group.id for group in owners}
                limit = min((group.maxDaily for group in owners if group.maxDaily is not None), default=20)
                selected, by_day = set(), defaultdict(list)
                for owner in owners:
                    for event_id in group_index[owner.id]:
                        budget.tick()
                        selected.add(event_id)
                for event_id in selected:
                    budget.tick()
                    by_day[rows[event_id][1]].append(event_id)
                for day in range(SCHOOL_DAYS):
                    counted = by_day[day]
                    if len(counted) > limit:
                        add("group_daily_load", "Превышен дневной предел для подтверждённой единицы состава.", [*owner_ids, *counted])
            del unit_members, seen_patterns
            continue
        representatives = [group for group in class_groups if group.kind == "subgroup"] or class_groups
        whole_limits = [group.maxDaily for group in class_groups if group.kind == "whole" and group.maxDaily is not None]
        for representative in representatives:
            budget.check()
            if representative.maxDaily is None:
                continue
            limit = min([representative.maxDaily, *whole_limits])
            for day in range(SCHOOL_DAYS):
                counted = pupil_days[(representative.id, day)]
                if len(counted) > limit:
                    add("group_daily_load", "Превышен подтверждённый дневной предел уроков учеников группы.", [representative.id, *counted])
    _independent_transition_issues(plan, accepted, budget, add, indexes=validation_indexes)
    if omitted:
        issues.append({"code": "issues_truncated", "message": f"Дополнительно обнаружено нарушений: {omitted}.", "entityIds": []})
    budget.check()
    return {"valid": not issues, "issues": issues}


def _validation_route_indexes(accepted, budget):
    """One independently built index reused by daily limits and route checks."""
    rows, teacher_index, group_index = {}, defaultdict(list), defaultdict(list)
    for event_id, (assignment, demand, slot, _occurrence) in accepted.items():
        budget.check()
        rows[event_id] = (event_id, slot.day, minutes(slot.start), minutes(slot.end), assignment.roomId)
        for teacher_id in demand.teacherIds:
            budget.tick()
            teacher_index[teacher_id].append(event_id)
        for group_id in demand.groupIds:
            budget.tick()
            group_index[group_id].append(event_id)
    return rows, teacher_index, group_index


def _independent_transition_issues(plan, accepted, budget, add, *, indexes=None):
    """Stream actual routes from bounded indexes, without CP/timing helpers.

    Each accepted event is stored once. Group membership indexes hold at most
    MAX_EVENTS * MAX_GROUPS references; only the current route is materialized.
    Identical anonymous-unit owner sets describe the same physical constraints
    and are verified once, without retaining their duplicate event lists.
    """
    rules = {(rule.fromRoomId, rule.toRoomId): rule.minMinutes for rule in plan.roomTransitions}
    has_room_rules = any(rules.values())
    if not has_room_rules and not any(owner.minTransitionMinutes for owner in [*plan.teachers, *plan.groups]):
        return
    rows, teacher_index, group_index = indexes if indexes is not None else _validation_route_indexes(accepted, budget)

    def group_route_events(owners):
        selected = set()
        for owner in owners:
            budget.check()
            for event_id in group_index[owner.id]:
                budget.tick()
                selected.add(event_id)
        for event_id in selected:
            budget.tick()
            yield rows[event_id]

    def actual_routes():
        for teacher in plan.teachers:
            budget.check()
            if not teacher.minTransitionMinutes and not has_room_rules:
                continue
            lessons = []
            for event_id in teacher_index[teacher.id]:
                budget.tick()
                lessons.append(rows[event_id])
            for day in range(SCHOOL_DAYS):
                budget.check()
                merged = []
                for event in sorted((event for event in teacher.presenceEvents if event.day == day), key=lambda item: item.start):
                    budget.tick()
                    start, end = minutes(event.start), minutes(event.end)
                    if merged and start <= merged[-1][1]:
                        merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
                    else:
                        merged.append((start, end))
                for index, (start, end) in enumerate(merged):
                    budget.tick()
                    lessons.append((f"presence_{teacher.id}_{day}_{index}", day, start, end, None))
            yield "teacher_transition", [teacher], lessons
        by_class = defaultdict(list)
        for group in plan.groups:
            budget.tick()
            by_class[canonical(group.className)].append(group)
        used_groups = frozenset(group_index)
        for original_roster in by_class.values():
            budget.check()
            if not any(group.minTransitionMinutes for group in original_roster) and not has_room_rules:
                continue
            roster = [group for group in original_roster if group.kind == "whole" or group.id in used_groups or group.minTransitionMinutes or group.requiredBreaks]
            whole = [group for group in roster if group.kind == "whole"]
            parts = [group for group in roster if group.kind == "subgroup"]
            membership = defaultdict(list)
            for group in parts:
                budget.check()
                for unit in ([("explicit", unit) for unit in group.attendanceUnitIds] or [("partition", group.id)]):
                    budget.tick()
                    membership[unit].append(group)
            seen_patterns = set()
            for members in membership.values():
                budget.check()
                pattern = tuple(sorted(group.id for group in members))
                if pattern in seen_patterns:
                    continue
                seen_patterns.add(pattern)
                owners = whole + members
                yield "group_transition", owners, group_route_events(owners)
            all_whole_units = set()
            for group in whole:
                for unit in group.attendanceUnitIds:
                    budget.tick()
                    all_whole_units.add(("explicit", unit))
            part_size = sum(part.size for part in parts)
            remaining = (bool(all_whole_units - membership.keys()) if all_whole_units else
                         any(group.size > part_size for group in whole))
            if whole and (not parts or remaining):
                yield "group_transition", whole, group_route_events(whole)

    for code, owners, events in actual_routes():
        budget.check()
        minimum = max(owner.minTransitionMinutes for owner in owners)
        events_by_day, rests_by_day = defaultdict(list), defaultdict(list)
        for event in events:
            budget.tick()
            events_by_day[event[1]].append(event)
        for owner in owners:
            budget.check()
            for rest in owner.requiredBreaks:
                budget.tick()
                rests_by_day[rest.day].append((minutes(rest.start), minutes(rest.end)))
        for day in range(SCHOOL_DAYS):
            budget.check()
            ordered = sorted(events_by_day[day], key=lambda item: (item[2], item[3], item[0]))
            rests = sorted(rests_by_day[day])
            for first, second in zip(ordered, ordered[1:]):
                budget.check()
                required = max(minimum, rules.get((first[4], second[4]), 0))
                if not required:
                    continue
                cursor, capacity = first[3], 0
                for start, end in rests:
                    budget.tick()
                    if end <= cursor or start >= second[2]:
                        continue
                    capacity = max(capacity, max(0, min(start, second[2]) - cursor))
                    cursor = max(cursor, min(end, second[2]))
                capacity = max(capacity, second[2] - cursor)
                if capacity < required:
                    add(code, "Между соседними событиями нет непрерывного времени для перехода вне обязательного отдыха.",
                        [owner.id for owner in owners] + [first[0], second[0]])


def _merged_intervals(intervals):
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _gap_segments(start, end, breaks):
    """Return disjoint rest and remaining intervals inside one known-event gap."""
    rest = _merged_intervals((max(start, first), min(end, last)) for first, last in breaks
                             if first < end and start < last)
    free, cursor = [], start
    for first, last in rest:
        if cursor < first:
            free.append((cursor, first))
        cursor = last
    if cursor < end:
        free.append((cursor, end))
    return rest, free


def _group_route_specs(plan):
    """One route per explicit atomic attendance unit, or disjoint partition part.

    A whole-class-only residual route prevents omission of pupils outside named
    subgroups. Mixed unresolved partitions are blocked by planning readiness.
    """
    classes = defaultdict(list)
    for group in plan.groups:
        classes[canonical(group.className)].append(group)
    routes = []
    for class_name, roster in classes.items():
        used_groups = {group_id for demand in plan.demands for group_id in demand.groupIds}
        roster = [group for group in roster if group.kind == "whole" or group.id in used_groups or group.minTransitionMinutes or group.requiredBreaks]
        whole = [group for group in roster if group.kind == "whole"]
        subgroups = [group for group in roster if group.kind == "subgroup"]
        units = defaultdict(list)
        for group in subgroups:
            for unit in ([("explicit", unit) for unit in group.attendanceUnitIds] or [("partition", group.id)]):
                units[unit].append(group)
        for unit, members in units.items():
            owners = whole + members
            routes.append((f"{class_name}:{unit}", owners, {item.id for item in owners}))
        declared_whole = {("explicit", unit) for group in whole for unit in group.attendanceUnitIds}
        residual = (bool(declared_whole - set(units)) if declared_whole else
                    any(group.size > sum(member.size for member in subgroups) for group in whole))
        if whole and (not subgroups or residual):
            routes.append((f"{class_name}:whole_residual", whole, {item.id for item in whole}))
    return routes


def _assignment_metrics(plan, assignments, budget):
    """Exact known-event timing, never an assertion about avoidable waiting."""
    baseline = {item.eventId: item for item in plan.baseline}
    slots = {item.id: item for item in plan.slots}
    demands = {f"{demand.id}_{occurrence}": demand for demand in plan.demands for occurrence in range(demand.weeklyCount)}
    teacher_days = defaultdict(list)
    changed_slots = changed_rooms = changed_events = early = 0
    for item in assignments:
        budget.tick()
        event_id, slot = item["eventId"], slots[item["slotId"]]
        early += slot.slot - 1
        if event_id in baseline:
            slot_changed = item["slotId"] != baseline[event_id].slotId
            room_changed = item["roomId"] != baseline[event_id].roomId
            changed_slots += slot_changed
            changed_rooms += room_changed
            changed_events += slot_changed or room_changed
        for teacher_id in demands[event_id].teacherIds:
            teacher_days[(teacher_id, slot.day)].append({"eventId": event_id, "startMinute": minutes(slot.start), "endMinute": minutes(slot.end), "roomId": item["roomId"]})
    room_rules = {(rule.fromRoomId, rule.toRoomId): rule.minMinutes for rule in plan.roomTransitions}
    timing, summaries, free_days = [], [], []
    for teacher in sorted(plan.teachers, key=lambda item: item.id):
        budget.check()
        scope = "lessons_and_presence_events" if teacher.presenceEvents else "lessons_only"
        summary = {"teacherId": teacher.id, "lessonCount": 0, "lessonMinutes": 0,
                   "attendanceDays": 0, "spanMinutes": 0, "gapMinutes": 0,
                   "requiredBreakMinutes": 0, "transitionMinutes": 0, "unallocatedGapMinutes": 0,
                   "maxGapMinutes": 0, "maxDailySpanMinutes": 0, "timingScope": scope}
        for day in range(SCHOOL_DAYS):
            lessons = sorted(teacher_days[(teacher.id, day)], key=lambda item: item["startMinute"])
            presence = [(minutes(event.start), minutes(event.end)) for event in teacher.presenceEvents if event.day == day]
            intervals = _merged_intervals([(item["startMinute"], item["endMinute"]) for item in lessons] + presence)
            rests = _merged_intervals((minutes(rest.start), minutes(rest.end)) for rest in teacher.requiredBreaks if rest.day == day)
            day_slots = [slot for slot in plan.slots if slot.day == day]
            if not intervals:
                if day_slots:
                    range_start = min(minutes(slot.start) for slot in day_slots)
                    range_end = max(minutes(slot.end) for slot in day_slots)
                    range_rests, range_free = _gap_segments(range_start, range_end, rests)
                    free_days.append({"teacherId": teacher.id, "day": day, "startMinute": range_start,
                                      "endMinute": range_end, "minutes": range_end - range_start,
                                      "noKnownEvents": True, "canLeaveSchool": None, "timingScope": scope,
                                      "requiredBreakMinutes": sum(end - start for start, end in range_rests),
                                      "freeBlocks": [{"startMinute": start, "endMinute": end, "minutes": end - start, "canLeaveSchool": None} for start, end in range_free]})
                continue
            first, last = intervals[0][0], intervals[-1][1]
            occupied = sum(end - start for start, end in intervals)
            gaps = [{"startMinute": first_end, "endMinute": second_start, "minutes": second_start - first_end}
                    for (_, first_end), (second_start, _) in zip(intervals, intervals[1:])]
            break_intervals, transition_intervals = [], []
            room_at_end = {item["endMinute"]: item["roomId"] for item in lessons}
            room_at_start = {item["startMinute"]: item["roomId"] for item in lessons}
            for gap in gaps:
                gap_rests, available = _gap_segments(gap["startMinute"], gap["endMinute"], rests)
                from_room, to_room = room_at_end.get(gap["startMinute"]), room_at_start.get(gap["endMinute"])
                required = max(teacher.minTransitionMinutes, room_rules.get((from_room, to_room), 0))
                chosen = next(((start, start + required) for start, end in available if end - start >= required), None) if required else None
                allocated = required if chosen else 0
                if chosen:
                    transition_intervals.append({"startMinute": chosen[0], "endMinute": chosen[1], "minutes": allocated,
                                                 "fromRoomId": from_room, "toRoomId": to_room})
                break_intervals.extend({"startMinute": start, "endMinute": end, "minutes": end - start} for start, end in gap_rests)
                gap.update(requiredBreakMinutes=sum(end - start for start, end in gap_rests), transitionMinutes=allocated)
                gap["unallocatedGapMinutes"] = gap["minutes"] - gap["requiredBreakMinutes"] - allocated
            free_blocks = []
            if day_slots:
                range_start, range_end = min(minutes(slot.start) for slot in day_slots), max(minutes(slot.end) for slot in day_slots)
                cursor = range_start
                unavailable = _merged_intervals(intervals + rests + [(item["startMinute"], item["endMinute"]) for item in transition_intervals])
                for start, end in unavailable:
                    if start > cursor:
                        free_blocks.append({"startMinute": cursor, "endMinute": min(start, range_end), "minutes": min(start, range_end) - cursor, "canLeaveSchool": None})
                    cursor = max(cursor, end)
                    if cursor >= range_end:
                        break
                if cursor < range_end:
                    free_blocks.append({"startMinute": cursor, "endMinute": range_end, "minutes": range_end - cursor, "canLeaveSchool": None})
            free_blocks = [block for block in free_blocks if block["minutes"] > 0]
            timing.append({"teacherId": teacher.id, "day": day, "lessonCount": len(lessons),
                           "presenceEventCount": len(presence), "startMinute": first, "endMinute": last,
                           "occupiedMinutes": occupied, "spanMinutes": last - first, "gapMinutes": last - first - occupied,
                           "lessonIntervals": lessons, "gapIntervals": gaps, "freeBlocks": free_blocks,
                           "requiredBreakIntervals": break_intervals, "transitionIntervals": transition_intervals,
                           **{field: sum(gap[field] for gap in gaps) for field in ("requiredBreakMinutes", "transitionMinutes", "unallocatedGapMinutes")},
                           "timingScope": scope, "rangeSource": "school_slots"})
            summary["lessonCount"] += len(lessons)
            summary["lessonMinutes"] += sum(item["endMinute"] - item["startMinute"] for item in lessons)
            summary["attendanceDays"] += 1
            summary["spanMinutes"] += last - first
            summary["gapMinutes"] += last - first - occupied
            for field in ("requiredBreakMinutes", "transitionMinutes", "unallocatedGapMinutes"):
                summary[field] += sum(gap[field] for gap in gaps)
            summary["maxGapMinutes"] = max(summary["maxGapMinutes"], max((gap["minutes"] for gap in gaps), default=0))
            summary["maxDailySpanMinutes"] = max(summary["maxDailySpanMinutes"], last - first)
        summaries.append(summary)
    budget.check()
    return {"baselineChanges": changed_events, "baselineComparedEvents": len(baseline),
            "changedSlots": changed_slots, "changedRooms": changed_rooms, "earlySlotPenalty": early,
            "teacherDailyTiming": timing, "teacherSummary": summaries, "teacherFreeDays": free_days,
            "timingDefinition": "real_minutes; gaps_include_all_breaks_between_known_events",
            "timingLimitations": ["Учитываются только явно заданные перерывы и минимальные переходы; остаточные разрывы не считаются доказанно избежимыми.",
                                  "Учитываются только уроки и явно заданные очные события; разрешение покинуть школу не подтверждено."]}


def _time_cost(plan, metrics):
    return sum(plan.preferences.gapWeight * teacher["unallocatedGapMinutes"]
               + plan.preferences.spanWeight * teacher["spanMinutes"]
               + plan.preferences.dayWeight * teacher["attendanceDays"] for teacher in metrics["teacherSummary"])


def _comparison(control, candidate, control_objective, candidate_objective):
    previous = {item["teacherId"]: item for item in control["teacherSummary"]}
    fields = ("gapMinutes", "requiredBreakMinutes", "transitionMinutes", "unallocatedGapMinutes", "spanMinutes", "attendanceDays", "maxGapMinutes", "maxDailySpanMinutes")
    return {"scope": "same_input", "controlObjective": control_objective, "candidateObjective": candidate_objective,
            "improved": candidate_objective < control_objective,
            "teacherDeltas": [{"teacherId": item["teacherId"], **{field: item[field] - previous[item["teacherId"]][field] for field in fields}}
                              for item in candidate["teacherSummary"]], "actualSchoolSavingsMeasured": False}


def solution_metrics(value: PlanningInput | dict, assignments: list[dict | Assignment]) -> dict:
    """Describe assignments; callers must independently validate them first."""
    plan = PlanningInput.model_validate(value)
    supplied = [Assignment.model_validate(item).model_dump() for item in assignments]
    return _assignment_metrics(plan, supplied, _Deadline(5))


def _generate_objective(plan, metrics):
    early_upper = sum(item.weeklyCount for item in plan.demands) * 19 * plan.preferences.earlyWeight
    return _time_cost(plan, metrics) * (early_upper + 1) + plan.preferences.earlyWeight * metrics["earlySlotPenalty"]


def compare_assignments(value: PlanningInput | dict, control_assignments: list, candidate_assignments: list) -> dict:
    """Compare the same explicit model; this helper never certifies feasibility."""
    plan = PlanningInput.model_validate(value)
    control, candidate = solution_metrics(plan, control_assignments), solution_metrics(plan, candidate_assignments)
    return _same_input_comparison(plan, control, candidate)


def _same_input_comparison(plan, control, candidate):
    result = _comparison(control, candidate, _generate_objective(plan, control), _generate_objective(plan, candidate))
    result["inputHash"] = hashlib.sha256(json.dumps(plan.model_dump(), sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    return result


def _solve_plan(value: PlanningInput | dict, mode: str = "generate", time_limit: float = 10, *,
                search_workers: int = 1, progress_callback=None, candidate_validator=None) -> dict:
    """Build CP-SAT, use explicit workers, independently verify all candidates.

    Generate minimizes known-event gaps/span/days, then ordinal lateness.
    Repair minimizes baseline changes before weighted changes and lateness.
    Preferences are costs, never permission to relax a hard constraint.
    """
    started = time.monotonic()
    valid_mode = isinstance(mode, str) and mode in {"generate", "repair"}
    metrics = {"engine": "ortools_cp_sat", "mode": mode if valid_mode else "invalid",
               "eventCount": 0, "candidateCount": 0, "objective": None, "bestBound": None,
               "solverStatus": "NOT_RUN", "timeLimitSeconds": None, "wallTimeSeconds": 0.0, "optimalityProven": False,
               "searchWorkers": search_workers if type(search_workers) is int and 1 <= search_workers <= 8 else None,
               "candidateCountMeaning": "admissible_event_slot_room_positions", "solutionCount": 0,
               "incumbentImprovements": 0}
    control = None
    incumbent = None
    comparison = None

    def result(status, diagnostics=(), assignments=(), validation=None):
        metrics["wallTimeSeconds"] = round(time.monotonic() - started, 6)
        return {"status": status, "assignments": list(assignments), "diagnostics": list(diagnostics)[:ISSUE_LIMIT],
                "metrics": dict(metrics), "validation": validation or {"valid": False, "issues": []},
                "control": control, "comparison": comparison}

    def diagnostic(code, message, ids=()):
        return {"code": code, "message": message, "entityIds": sorted(set(ids))[:MAX_EVENTS]}

    if (not valid_mode or isinstance(time_limit, bool) or not isinstance(time_limit, (int, float))
            or not 0 <= time_limit <= MAX_SOLVE_SECONDS or not math.isfinite(time_limit)
            or type(search_workers) is not int or not 1 <= search_workers <= 8
            or progress_callback is not None and not callable(progress_callback)
            or candidate_validator is not None and not callable(candidate_validator)):
        return result("invalid", [diagnostic("invalid_options", "Некорректный режим, параметры поиска или предел времени расчёта (0–1800 секунд).")])
    metrics["timeLimitSeconds"] = float(time_limit)
    reserve = min(10.0, max(0.1, float(time_limit) * 0.1))
    metrics["finalizationReserveSeconds"] = reserve
    budget = _Deadline(float(time_limit), started=started, metrics=metrics, progress_callback=progress_callback)
    budget.stage("input_validation")
    try:
        plan = PlanningInput.model_validate(value, context={"scheduling_check": budget.check})
    except (ValidationError, TypeError, ValueError):
        return result("invalid", [diagnostic("invalid_input", "Некорректная или слишком большая модель планирования.")])
    readiness = planning_readiness(plan, context={"scheduling_check": budget.check})
    if not readiness["ready"]:
        return result("invalid", readiness["blockers"], validation={"valid": False, "issues": readiness["blockers"]})
    events = [(f"{demand.id}_{occurrence}", demand, occurrence) for demand in sorted(plan.demands, key=lambda item: item.id) for occurrence in range(demand.weeklyCount)]
    metrics["eventCount"] = len(events)

    def checked_candidate(assignments):
        checked = validate_solution(plan, assignments, time_limit=min(5.0, budget.remaining()))
        if checked["valid"] and candidate_validator is not None:
            try:
                accepted = candidate_validator([dict(item) for item in assignments]) is True
            except Exception:
                accepted = False
            if not accepted:
                return {"valid": False, "issues": [{"code": "candidate_validation_hook",
                    "message": "Дополнительные ограничения не прошли независимую проверку.", "entityIds": []}]}
        return checked
    warnings = []
    if mode == "repair" and not plan.baseline:
        warnings.append(diagnostic("no_baseline", "Базовых назначений нет; близость к предыдущему расписанию не оценивается."))
    if time_limit == 0:
        metrics["solverStatus"] = "UNKNOWN"
        return result("unknown", [*warnings, diagnostic("time_limit", "Предел времени равен нулю; поиск не запускался. Неразрешимость не установлена.")])
    if plan.baseline and len(plan.baseline) == len(events):
        budget.stage("baseline_validation")
        preserved = [item.model_dump() for item in plan.baseline]
        checked = checked_candidate(preserved)
        metrics["baselineValidated"] = checked["valid"]
        if checked["valid"]:
            metrics.update(_assignment_metrics(plan, preserved, budget))
            objective = (_generate_objective(plan, metrics) if mode == "generate"
                         else plan.preferences.earlyWeight * metrics["earlySlotPenalty"])
            metrics.update(solverStatus="BASELINE_VALIDATED", objective=objective, bestBound=objective,
                           baselinePreserved=mode == "repair",
                           objectiveDefinition=("lexicographic(weightedKnownEventTiming, weightedOrdinalLateness)" if mode == "generate"
                                                else "lexicographic(baselineChanges, weightedSlotRoomChanges, weightedOrdinalLateness)"),
                           consecutiveDefinition="adjacent_slot_numbers_within_day", optimalityProven=mode == "repair")
            control = {"assignments": preserved, "validation": checked, "metrics": _assignment_metrics(plan, preserved, budget),
                       "method": "independently_validated_supplied_candidate" if mode == "generate" else "validated_complete_repair_baseline", "objective": objective}
            incumbent = control
            comparison = _same_input_comparison(plan, control["metrics"], control["metrics"])
            if mode == "repair":
                return result("optimal", warnings, preserved, checked)
            metrics["bestBound"] = None
            budget.fallback_result = result("feasible", warnings, preserved, checked)
            if len(events) >= 100 and time_limit > 30 and budget.remaining() > reserve + 1:
                from .scheduling_local_search import improve_incumbent

                def accept_local(assignments, expected_delta):
                    nonlocal incumbent, comparison
                    checked = checked_candidate(assignments)
                    if not checked["valid"]:
                        return False
                    described = _assignment_metrics(plan, assignments, budget)
                    objective = _generate_objective(plan, described)
                    if objective >= incumbent["objective"] or objective - incumbent["objective"] != expected_delta:
                        return False
                    incumbent = {"assignments": assignments, "validation": checked,
                                 "metrics": described, "objective": objective}
                    metrics.update(described, objective=objective, bestBound=None, optimalityProven=False)
                    metrics["incumbentImprovements"] += 1
                    comparison = _same_input_comparison(plan, control["metrics"], described)
                    # Promotion happens immediately after full validation/scoring,
                    # before any later deadline, model construction or CP search.
                    budget.fallback_result = result("feasible", warnings, assignments, checked)
                    return True

                budget.stage("local_improvement")
                metrics["localSearch"] = improve_incumbent(plan, incumbent["assignments"],
                    seconds=min(30.0, float(time_limit) * 0.15, budget.remaining() - reserve),
                    accept_candidate=accept_local)
        if any(issue["code"] == "validation_time_limit" for issue in checked["issues"]):
            return result("unknown", [diagnostic("validation_time_limit", "Базовое расписание не проверено в пределах времени.")], validation=checked)
    budget.stage("proof_prechecks")
    slots = sorted(plan.slots, key=lambda item: (item.day, item.start, item.end, item.slot, item.id))
    rooms = sorted(plan.rooms, key=lambda item: item.id)
    slot_index, room_index = {item.id: index for index, item in enumerate(slots)}, {item.id: index for index, item in enumerate(rooms)}
    group_by_id, teacher_by_id = {item.id: item for item in plan.groups}, {item.id: item for item in plan.teachers}
    room_by_id = {item.id: item for item in rooms}
    teacher_events = defaultdict(list)
    for event_number, (_event_id, demand, _occurrence) in enumerate(events):
        budget.tick()
        for teacher_id in demand.teacherIds:
            teacher_events[teacher_id].append(event_number)
    for teacher_id, event_numbers in teacher_events.items():
        if len(event_numbers) > teacher_by_id[teacher_id].maxWeekly:
            metrics["solverStatus"] = "INFEASIBLE_PRECHECK"
            return result("infeasible", [*warnings, diagnostic("weekly_load", "Число требуемых занятий превышает недельный предел преподавателя.", [teacher_id])])
        budget.check()
    for teacher in plan.teachers:
        budget.check()
        if any(event.day == rest.day and event.start < rest.end and rest.start < event.end
               for event in teacher.presenceEvents for rest in teacher.requiredBreaks):
            metrics["solverStatus"] = "INFEASIBLE_PRECHECK"
            return result("infeasible", [diagnostic("presence_required_break", "Очная обязанность пересекается с обязательным отдыхом.", [teacher.id])])
    qualifications = {teacher.id: {canonical(subject) for subject in teacher.subjects} for teacher in plan.teachers}
    for demand in plan.demands:
        if demand.weekTypes and canonical(plan.weekType) not in {canonical(kind) for kind in demand.weekTypes}:
            metrics["solverStatus"] = "INFEASIBLE_PRECHECK"
            return result("infeasible", [*warnings, diagnostic("demand_week_type", "Тип выбранной недели не соответствует нагрузке; сначала примените календарный снимок.", [demand.id])])
        unqualified = [teacher_id for teacher_id in demand.teacherIds if canonical(demand.subject) not in qualifications[teacher_id]]
        if unqualified or demand.weeklyCount > len({slot.day for slot in slots if not demand.allowedSlotIds or slot.id in demand.allowedSlotIds}) * demand.maxPerDay:
            metrics["solverStatus"] = "INFEASIBLE_PRECHECK"
            proof = diagnostic("qualification", "Не подтверждена квалификация всех участвующих преподавателей.", [demand.id, *unqualified]) if unqualified else diagnostic("demand_daily", "Недельное число занятий больше суммы дневных пределов доступных дней.", [demand.id])
            return result("infeasible", [*warnings, proof])
        budget.check()
    budget.stage("availability_indexes")
    # Compile real interval overlap once (<=100x100), not for each event.
    slot_overlaps = {}
    slot_times = []
    for slot in slots:
        budget.check()
        start, end = slot.day * 1440 + minutes(slot.start), slot.day * 1440 + minutes(slot.end)
        slot_times.append((start, end, end - start))
        slot_overlaps[slot.id] = {index for index, other in enumerate(slots) if slot.day == other.day and slot.start < other.end and other.start < slot.end}
    pattern_cache = {}

    def unavailable_indices(entity):
        pattern = tuple(sorted(entity.unavailableSlotIds))
        if pattern not in pattern_cache:
            blocked = set()
            for slot_id in pattern:
                budget.tick()
                blocked.update(slot_overlaps[slot_id])
            pattern_cache[pattern] = blocked
        return pattern_cache[pattern]

    group_blocked, teacher_blocked, room_blocked = {}, {}, {}
    for roster, index in ((plan.groups, group_blocked), (plan.teachers, teacher_blocked), (plan.rooms, room_blocked)):
        for entity in roster:
            budget.check()
            index[entity.id] = unavailable_indices(entity)
            if hasattr(entity, "requiredBreaks") and entity.requiredBreaks:
                index[entity.id] = set(index[entity.id])
                for slot_number, slot in enumerate(slots):
                    budget.tick()
                    if any(rest.day == slot.day and rest.start < slot.end and slot.start < rest.end for rest in entity.requiredBreaks):
                        index[entity.id].add(slot_number)
    for teacher in plan.teachers:
        if teacher.workWindows:
            blocked = set(teacher_blocked[teacher.id])
            for index, slot in enumerate(slots):
                budget.tick()
                if not any(window.day == slot.day and window.start <= slot.start and slot.end <= window.end
                           for window in teacher.workWindows):
                    blocked.add(index)
            teacher_blocked[teacher.id] = blocked
        if teacher.presenceEvents:
            blocked = set(teacher_blocked[teacher.id])
            for index, slot in enumerate(slots):
                budget.tick()
                if any(event.day == slot.day and event.start < slot.end and slot.start < event.end
                       for event in teacher.presenceEvents):
                    blocked.add(index)
            teacher_blocked[teacher.id] = blocked
    group_classes = {group.id: canonical(group.className) for group in plan.groups}
    related_groups = {}
    for first in plan.groups:
        budget.check()
        related_groups[first.id] = {second.id for second in plan.groups if group_classes[first.id] == group_classes[second.id]
                                   and not (first.id != second.id and first.kind == second.kind == "subgroup"
                                            and ((first.attendanceUnitIds and second.attendanceUnitIds and set(first.attendanceUnitIds).isdisjoint(second.attendanceUnitIds))
                                                 or (not (first.attendanceUnitIds and second.attendanceUnitIds) and first.partition and first.partition == second.partition)))}
    room_tags = {room.id: {canonical(tag) for tag in room.tags} for room in rooms}
    student_patterns = {}
    demand_domains, demand_student_groups = {}, {}
    budget.stage("candidate_domains")
    for demand in plan.demands:
        budget.check()
        pattern = tuple(sorted(demand.groupIds))
        if pattern not in student_patterns:
            affected, blocked = set(), set()
            for group_id in pattern:
                budget.tick()
                affected.update(related_groups[group_id])
            for group_id in affected:
                budget.tick()
                blocked.update(group_blocked[group_id])
            student_patterns[pattern] = affected, blocked
        affected, student_blocked = student_patterns[pattern]
        demand_student_groups[demand.id] = affected
        eligible_rooms = [room_index[room_id] for room_id in
                          eligible_room_ids(demand, group_by_id, room_by_id, room_tags, check=budget.check)]
        blocked = set(student_blocked)
        for teacher_id in demand.teacherIds:
            blocked.update(teacher_blocked[teacher_id])
        candidates = []
        allowed_slots = sorted(slot_index[slot_id] for slot_id in demand.allowedSlotIds) if demand.allowedSlotIds else range(len(slots))
        for slot_number in allowed_slots:
            budget.check()
            if slot_number in blocked:
                continue
            if demand.activeFrom or demand.activeUntil:
                lesson_date = (date.fromisoformat(plan.periodStart) + timedelta(days=slots[slot_number].day)).isoformat()
                if (demand.activeFrom and lesson_date < demand.activeFrom) or (demand.activeUntil and lesson_date > demand.activeUntil):
                    continue
            for room_number in eligible_rooms:
                budget.tick()
                if slot_number not in room_blocked[rooms[room_number].id]:
                    candidates.append((slot_number, room_number, *slot_times[slot_number]))
        demand_domains[demand.id] = candidates
        metrics["candidateCount"] += len(candidates) * demand.weeklyCount
        if not candidates:
            metrics["solverStatus"] = "INFEASIBLE_PRECHECK"
            return result("infeasible", [*warnings, diagnostic("empty_domain", "Нет допустимой пары слот–кабинет с учётом квалификации, доступности, вместимости и оснащения.", [demand.id])])
    budget.stage("engine_initialization")
    try:
        from ortools.sat.python import cp_model
    except ImportError:
        return result("invalid", [diagnostic("solver_unavailable", "Локальная библиотека OR-Tools недоступна.")])
    budget.stage("model_construction")
    model = cp_model.CpModel()
    event_vars, event_slot_domains, event_starts, event_ends, event_durations, event_days = [], [], [], [], [], []
    room_intervals, group_intervals = defaultdict(list), defaultdict(list)
    demand_events = defaultdict(list)
    objective_terms = []
    teacher_time_terms = []
    baseline_change_terms, weighted_change_terms = [], []
    baseline = {item.eventId: item for item in plan.baseline} if mode == "repair" else {}
    event_index = {event_id: index for index, (event_id, _demand, _occurrence) in enumerate(events)}
    pinned = {item.eventId for item in plan.pins}
    linked = {event_id for chain in plan.eventChains for event_id in chain.eventIds}
    day_booleans = []
    for index, (event_id, demand, occurrence) in enumerate(events):
        budget.check()
        candidates = demand_domains[demand.id]
        selected_slot = model.NewIntVar(0, len(slots) - 1, f"slot_{index}")
        selected_room = model.NewIntVar(0, len(rooms) - 1, f"room_{index}")
        start = model.NewIntVar(min(item[2] for item in candidates), max(item[2] for item in candidates), f"start_{index}")
        end = model.NewIntVar(min(item[3] for item in candidates), max(item[3] for item in candidates), f"end_{index}")
        duration = model.NewIntVar(min(item[4] for item in candidates), max(item[4] for item in candidates), f"duration_{index}")
        model.AddAllowedAssignments([selected_slot, selected_room, start, end, duration], candidates)
        interval = model.NewIntervalVar(start, duration, end, f"event_{index}")
        event_vars.append((selected_slot, selected_room, interval))
        event_starts.append(start)
        event_ends.append(end)
        event_durations.append(duration)
        event_slot_domains.append({item[0] for item in candidates})
        demand_events[demand.id].append(index)
        for group_id in demand.groupIds:
            group_intervals[group_id].append(index)
        options = []
        for room_number in sorted({item[1] for item in candidates}):
            budget.tick()
            selected = model.NewBoolVar(f"room_choice_{index}_{room_number}")
            model.Add(selected_room == room_number).OnlyEnforceIf(selected)
            model.Add(selected_room != room_number).OnlyEnforceIf(selected.Not())
            room_intervals[room_number].append(model.NewOptionalIntervalVar(start, duration, end, selected, f"room_event_{index}_{room_number}"))
            options.append(selected)
        model.AddExactlyOne(options)
        day = model.NewIntVar(0, SCHOOL_DAYS - 1, f"day_{index}")
        model.AddElement(selected_slot, [slot.day for slot in slots], day)
        event_days.append(day)
        day_choices = []
        for day_number in range(SCHOOL_DAYS):
            selected = model.NewBoolVar(f"day_choice_{index}_{day_number}")
            model.Add(day == day_number).OnlyEnforceIf(selected)
            model.Add(day != day_number).OnlyEnforceIf(selected.Not())
            day_choices.append(selected)
        day_booleans.append(day_choices)
        if plan.preferences.earlyWeight:
            lateness = model.NewIntVar(0, 19, f"lateness_{index}")
            model.AddElement(selected_slot, [slot.slot - 1 for slot in slots], lateness)
            objective_terms.append(plan.preferences.earlyWeight * lateness)
        if event_id in baseline:
            previous = baseline[event_id]
            changes = []
            for selected, previous_index, weight, label in (
                (selected_slot, slot_index[previous.slotId], plan.preferences.changeWeight, "slot"),
                (selected_room, room_index[previous.roomId], plan.preferences.roomChangeWeight, "room"),
            ):
                changed = model.NewBoolVar(f"changed_{label}_{index}")
                model.Add(selected != previous_index).OnlyEnforceIf(changed)
                model.Add(selected == previous_index).OnlyEnforceIf(changed.Not())
                changes.append(changed)
                if weight:
                    weighted_change_terms.append(weight * changed)
                model.AddHint(selected, previous_index)
            any_change = model.NewBoolVar(f"changed_event_{index}")
            model.AddMaxEquality(any_change, changes)
            baseline_change_terms.append(any_change)
    for intervals in room_intervals.values():
        budget.check()
        if len(intervals) > 1:
            model.AddNoOverlap(intervals)
    for event_numbers in group_intervals.values():
        budget.check()
        if len(event_numbers) > 1:
            model.AddNoOverlap([event_vars[index][2] for index in event_numbers])
    used_groups = sorted(group_intervals)
    for position, first_id in enumerate(used_groups):
        budget.check()
        for second_id in used_groups[position + 1:]:
            budget.tick()
            if second_id in related_groups[first_id]:
                indexes = sorted(set(group_intervals[first_id]) | set(group_intervals[second_id]))
                if len(indexes) > 1:
                    model.AddNoOverlap([event_vars[index][2] for index in indexes])
    slot_bools = {}

    def at_slot(event_number, slot_number):
        budget.tick()
        key = (event_number, slot_number)
        if key not in slot_bools:
            selected = model.NewBoolVar(f"at_slot_{event_number}_{slot_number}")
            model.Add(event_vars[event_number][0] == slot_number).OnlyEnforceIf(selected)
            model.Add(event_vars[event_number][0] != slot_number).OnlyEnforceIf(selected.Not())
            slot_bools[key] = selected
        return slot_bools[key]

    teacher_transition_vars = {}
    for teacher_id, teacher in teacher_by_id.items():
        budget.check()
        event_numbers = teacher_events[teacher_id]
        if len(event_numbers) > 1:
            model.AddNoOverlap([event_vars[index][2] for index in event_numbers])
        for day_number in range(SCHOOL_DAYS):
            model.Add(sum(day_booleans[index][day_number] for index in event_numbers) <= teacher.maxDaily)
            if (teacher.maxDailySpanMinutes is not None or teacher.maxDailyGapMinutes is not None
                    or (mode == "generate" and (plan.preferences.gapWeight or plan.preferences.spanWeight or plan.preferences.dayWeight))):
                starts, ends, durations = [], [], []
                day_start, day_end = day_number * 1440, (day_number + 1) * 1440
                presence = _merged_intervals([(day_start + minutes(event.start), day_start + minutes(event.end))
                                              for event in teacher.presenceEvents if event.day == day_number])
                starts.extend(start for start, _end in presence)
                ends.extend(end for _start, end in presence)
                durations.extend(end - start for start, end in presence)
                for index in event_numbers:
                    budget.tick()
                    present = day_booleans[index][day_number]
                    local_start = model.NewIntVar(day_start, day_end, f"teacher_start_{teacher_id}_{day_number}_{index}")
                    local_end = model.NewIntVar(day_start, day_end, f"teacher_end_{teacher_id}_{day_number}_{index}")
                    model.Add(local_start == event_starts[index]).OnlyEnforceIf(present)
                    model.Add(local_start == day_end).OnlyEnforceIf(present.Not())
                    model.Add(local_end == event_ends[index]).OnlyEnforceIf(present)
                    model.Add(local_end == day_start).OnlyEnforceIf(present.Not())
                    starts.append(local_start)
                    ends.append(local_end)
                    if teacher.maxDailyGapMinutes is not None or (mode == "generate" and plan.preferences.gapWeight):
                        duration_on_day = model.NewIntVar(0, 1440, f"teacher_duration_{teacher_id}_{day_number}_{index}")
                        model.Add(duration_on_day == event_durations[index]).OnlyEnforceIf(present)
                        model.Add(duration_on_day == 0).OnlyEnforceIf(present.Not())
                        durations.append(duration_on_day)
                if not starts:
                    continue
                first = model.NewIntVar(day_start, day_end, f"teacher_first_{teacher_id}_{day_number}")
                last = model.NewIntVar(day_start, day_end, f"teacher_last_{teacher_id}_{day_number}")
                span = model.NewIntVar(0, 1440, f"teacher_span_{teacher_id}_{day_number}")
                model.AddMinEquality(first, starts)
                model.AddMaxEquality(last, ends)
                model.AddMaxEquality(span, [last - first, 0])
                if teacher.maxDailySpanMinutes is not None:
                    model.Add(span <= teacher.maxDailySpanMinutes)
                if teacher.maxDailyGapMinutes is not None:
                    model.Add(span - sum(durations) <= teacher.maxDailyGapMinutes)
                if mode == "generate":
                    if plan.preferences.gapWeight:
                        credited_breaks = []
                        for rest_number, (rest_start, rest_end) in enumerate(_merged_intervals(
                                (day_start + minutes(rest.start), day_start + minutes(rest.end)) for rest in teacher.requiredBreaks if rest.day == day_number)):
                            budget.check()
                            before = model.NewBoolVar(f"before_rest_{teacher_id}_{day_number}_{rest_number}")
                            after = model.NewBoolVar(f"after_rest_{teacher_id}_{day_number}_{rest_number}")
                            used = model.NewBoolVar(f"used_rest_{teacher_id}_{day_number}_{rest_number}")
                            model.Add(first <= rest_start).OnlyEnforceIf(before)
                            model.Add(first > rest_start).OnlyEnforceIf(before.Not())
                            model.Add(last >= rest_end).OnlyEnforceIf(after)
                            model.Add(last < rest_end).OnlyEnforceIf(after.Not())
                            model.AddBoolAnd([before, after]).OnlyEnforceIf(used)
                            model.AddBoolOr([before.Not(), after.Not(), used])
                            credited_breaks.append((rest_end - rest_start) * used)
                        transition = model.NewIntVar(0, 1440, f"transition_total_{teacher_id}_{day_number}")
                        teacher_transition_vars[(teacher_id, day_number)] = transition
                        remainder = model.NewIntVar(0, 1440, f"unallocated_gap_{teacher_id}_{day_number}")
                        model.Add(remainder == span - sum(durations) - sum(credited_breaks) - transition)
                        teacher_time_terms.append(plan.preferences.gapWeight * remainder)
                    if plan.preferences.spanWeight:
                        teacher_time_terms.append(plan.preferences.spanWeight * span)
                    if plan.preferences.dayWeight:
                        present = model.NewBoolVar(f"teacher_present_{teacher_id}_{day_number}")
                        if presence:
                            model.Add(present == 1)
                        else:
                            model.AddMaxEquality(present, [day_booleans[index][day_number] for index in event_numbers])
                        teacher_time_terms.append(plan.preferences.dayWeight * present)
            if teacher.maxConsecutive >= teacher.maxDaily:
                continue
            occupancy = {}
            for slot_number, slot in enumerate(slots):
                budget.tick()
                if slot.day != day_number:
                    continue
                candidates = [at_slot(index, slot_number) for index in event_numbers if slot_number in event_slot_domains[index]]
                if candidates:
                    occupied = model.NewBoolVar(f"occupied_{teacher_id}_{day_number}_{slot.slot}")
                    model.Add(sum(candidates) == occupied)
                    occupancy[slot.slot] = occupied
            for first in range(1, 21 - teacher.maxConsecutive):
                model.Add(sum(occupancy.get(number, 0) for number in range(first, first + teacher.maxConsecutive + 1)) <= teacher.maxConsecutive)
    for demand in plan.demands:
        budget.check()
        used_days = []
        for day_number in range(SCHOOL_DAYS):
            budget.tick()
            model.Add(sum(day_booleans[index][day_number] for index in demand_events[demand.id]) <= demand.maxPerDay)
            if demand.teachingDays is not None:
                used = model.NewBoolVar(f"demand_day_{demand.id}_{day_number}")
                model.AddMaxEquality(used, [day_booleans[index][day_number] for index in demand_events[demand.id]])
                used_days.append(used)
        if demand.teachingDays is not None:
            model.Add(sum(used_days) == demand.teachingDays)
        # Occurrences have identical domains. Order only interchangeable ones:
        # exact pins, synchronized/linked occurrence identities and baselines
        # prohibit this symmetry reduction and are always retained verbatim.
        indexes = demand_events[demand.id]
        interchangeable = (not demand.syncKey and not any(events[index][0] in pinned for index in indexes)
                           and not any(events[index][0] in linked or events[index][0] in baseline for index in indexes))
        if interchangeable:
            for first, second in zip(indexes, indexes[1:]):
                model.Add(event_starts[first] < event_starts[second])
    class_groups = defaultdict(list)
    for group in plan.groups:
        class_groups[canonical(group.className)].append(group)
    for class_roster in class_groups.values():
        budget.check()
        explicit_parts = [group for group in class_roster if group.kind == "subgroup"]
        if explicit_parts and all(group.attendanceUnitIds for group in explicit_parts):
            for unit in {unit for group in explicit_parts for unit in group.attendanceUnitIds}:
                budget.check()
                owners = [group for group in class_roster if group.kind == "whole" or unit in group.attendanceUnitIds]
                limit = min(group.maxDaily for group in owners)
                affected_events = set(index for group in owners for index in group_intervals.get(group.id, []))
                for day_number in range(SCHOOL_DAYS):
                    model.Add(sum(day_booleans[index][day_number] for index in affected_events) <= limit)
            continue
        representatives = [group for group in class_roster if group.kind == "subgroup"] or class_roster
        whole_limits = [group.maxDaily for group in class_roster if group.kind == "whole"]
        for representative in representatives:
            budget.check()
            limit = min([representative.maxDaily, *whole_limits])
            affected_events = [index for index, (_event_id, demand, _occurrence) in enumerate(events) if representative.id in demand_student_groups[demand.id]]
            for day_number in range(SCHOOL_DAYS):
                model.Add(sum(day_booleans[index][day_number] for index in affected_events) <= limit)
    sync_groups = defaultdict(list)
    for index, (_event_id, demand, occurrence) in enumerate(events):
        budget.tick()
        if demand.syncKey:
            sync_groups[(demand.syncKey, occurrence)].append(index)
    for indexes in sync_groups.values():
        budget.tick()
        for index in indexes[1:]:
            model.Add(event_vars[index][0] == event_vars[indexes[0]][0])
    for pin in plan.pins:
        budget.tick()
        index = event_index[pin.eventId]
        model.Add(event_vars[index][0] == slot_index[pin.slotId])
        model.Add(event_vars[index][1] == room_index[pin.roomId])
    event_ordinals = {}
    slot_ordinals = [slot.slot for slot in slots]
    for chain in plan.eventChains:
        budget.check()
        for first_id, second_id in zip(chain.eventIds, chain.eventIds[1:]):
            first, second = event_index[first_id], event_index[second_id]
            if chain.sameDay:
                model.Add(event_days[first] == event_days[second])
            if chain.consecutiveSlots:
                for index in (first, second):
                    budget.tick()
                    if index not in event_ordinals:
                        ordinal = model.NewIntVar(1, 20, f"chain_ordinal_{index}")
                        model.AddElement(event_vars[index][0], slot_ordinals, ordinal)
                        event_ordinals[index] = ordinal
            if chain.unorderedPair:
                forward = model.NewBoolVar(f"chain_pair_forward_{chain.id}")
                for earlier, later, direction in ((first, second, forward), (second, first, forward.Not())):
                    model.Add(event_starts[later] >= event_ends[earlier]).OnlyEnforceIf(direction)
                    model.Add(event_ordinals[later] == event_ordinals[earlier] + 1).OnlyEnforceIf(direction)
                    if chain.maxGapMinutes is not None:
                        model.Add(event_starts[later] - event_ends[earlier] <= chain.maxGapMinutes).OnlyEnforceIf(direction)
                continue
            model.Add(event_starts[second] >= event_ends[first])
            if chain.maxGapMinutes is not None:
                model.Add(event_starts[second] - event_ends[first] <= chain.maxGapMinutes)
            if chain.consecutiveSlots:
                model.Add(event_ordinals[second] == event_ordinals[first] + 1)
    budget.stage("transition_model_construction")
    directed_rules = [(room_index[rule.fromRoomId], room_index[rule.toRoomId], rule.minMinutes)
                      for rule in plan.roomTransitions if rule.minMinutes]
    demand_room_slots = {}
    for demand_id, candidates in demand_domains.items():
        mapping = defaultdict(set)
        for slot_number, room_number, *_ in candidates:
            budget.tick()
            mapping[slot_number].add(room_number)
        demand_room_slots[demand_id] = mapping
    transition_checks = 0

    def charge_transition():
        nonlocal transition_checks
        transition_checks += 1
        budget.check()
        if transition_checks > TRANSITION_CONSTRAINT_LIMIT:
            raise _TransitionLimitExceeded(budget)

    def route_nodes(label, event_numbers, day_number, presence=()):
        nodes = []
        for slot_number, slot in enumerate(slots):
            budget.check()
            if slot.day != day_number:
                continue
            possible = [index for index in event_numbers if slot_number in event_slot_domains[index]]
            if not possible:
                continue
            occupied = model.NewBoolVar(f"route_occupied_{label}_{day_number}_{slot_number}")
            model.Add(sum(at_slot(index, slot_number) for index in possible) == occupied)
            room_var, domain = None, set()
            if directed_rules:
                room_var = model.NewIntVar(-1, len(rooms) - 1, f"route_room_{label}_{day_number}_{slot_number}")
                model.Add(room_var == -1).OnlyEnforceIf(occupied.Not())
                for index in possible:
                    budget.check()
                    model.Add(room_var == event_vars[index][1]).OnlyEnforceIf(at_slot(index, slot_number))
                    domain.update(demand_room_slots[events[index][1].id][slot_number])
            nodes.append({"start": minutes(slot.start), "end": minutes(slot.end), "occupied": occupied,
                          "room": room_var, "domain": domain, "roomBools": {}})
        for index, (start, end) in enumerate(presence):
            occupied = model.NewBoolVar(f"route_presence_{label}_{day_number}_{index}")
            model.Add(occupied == 1)
            nodes.append({"start": start, "end": end, "occupied": occupied, "room": None, "domain": set(), "roomBools": {}})
        return sorted(nodes, key=lambda node: (node["start"], node["end"]))

    def room_is(node, room_number):
        if room_number not in node["roomBools"]:
            charge_transition()
            selected = model.NewBoolVar(f"route_room_choice_{transition_checks}")
            model.Add(node["room"] == room_number).OnlyEnforceIf(selected)
            model.Add(node["room"] != room_number).OnlyEnforceIf(selected.Not())
            node["roomBools"][room_number] = selected
        return node["roomBools"][room_number]

    def constrain_route(label, owners, event_numbers, day_number, presence=(), target=None):
        minimum = max(owner.minTransitionMinutes for owner in owners)
        if not minimum and not directed_rules:
            if target is not None:
                model.Add(target == 0)
            return
        nodes = route_nodes(label, event_numbers, day_number, presence)
        prefix = [0]
        for index, node in enumerate(nodes):
            budget.check()
            count = model.NewIntVar(0, len(nodes), f"route_prefix_{label}_{day_number}_{index}")
            model.Add(count == prefix[-1] + node["occupied"])
            prefix.append(count)
        rests = _merged_intervals((minutes(rest.start), minutes(rest.end)) for owner in owners for rest in owner.requiredBreaks if rest.day == day_number)
        allocated = []
        for i, first in enumerate(nodes):
            for j in range(i + 1, len(nodes)):
                charge_transition()
                second = nodes[j]
                if first["end"] > second["start"]:
                    continue
                relevant = [(from_room, to_room, duration) for from_room, to_room, duration in directed_rules
                            if from_room in first["domain"] and to_room in second["domain"] and duration > minimum]
                if not minimum and not relevant:
                    continue
                _, free = _gap_segments(first["end"], second["start"], rests)
                capacity = max((end - start for start, end in free), default=0)
                between = prefix[j] - prefix[i + 1]
                adjacent = None
                if target is not None:
                    adjacent = model.NewBoolVar(f"route_adjacent_{label}_{day_number}_{i}_{j}")
                    model.Add(adjacent <= first["occupied"])
                    model.Add(adjacent <= second["occupied"])
                    model.Add(between == 0).OnlyEnforceIf(adjacent)
                    model.Add(between >= 1).OnlyEnforceIf([first["occupied"], second["occupied"], adjacent.Not()])
                    if minimum:
                        allocated.append(minimum * adjacent)
                if capacity < minimum:
                    model.Add(between >= 1).OnlyEnforceIf([first["occupied"], second["occupied"]])
                for from_room, to_room, duration in relevant:
                    charge_transition()
                    selected_from, selected_to = room_is(first, from_room), room_is(second, to_room)
                    if duration > capacity:
                        model.Add(between >= 1).OnlyEnforceIf([selected_from, selected_to])
                    if target is not None:
                        used = model.NewBoolVar(f"directed_transition_{label}_{day_number}_{i}_{j}_{from_room}_{to_room}")
                        model.AddBoolAnd([adjacent, selected_from, selected_to]).OnlyEnforceIf(used)
                        model.AddBoolOr([adjacent.Not(), selected_from.Not(), selected_to.Not(), used])
                        allocated.append((duration - minimum) * used)
        if target is not None:
            model.Add(target == sum(allocated))

    for teacher_id, teacher in teacher_by_id.items():
        for day_number in range(SCHOOL_DAYS):
            budget.check()
            presence = _merged_intervals((minutes(event.start), minutes(event.end)) for event in teacher.presenceEvents if event.day == day_number)
            constrain_route(f"teacher_{teacher_id}", [teacher], teacher_events[teacher_id], day_number, presence,
                            teacher_transition_vars.get((teacher_id, day_number)))
    if directed_rules or any(group.minTransitionMinutes for group in plan.groups):
        for route_number, (_route_id, owners, group_ids) in enumerate(_group_route_specs(plan)):
            budget.check()
            event_numbers = [index for index, (_event_id, demand, _occurrence) in enumerate(events) if group_ids.intersection(demand.groupIds)]
            for day_number in range(SCHOOL_DAYS):
                constrain_route(f"group_{route_number}", owners, event_numbers, day_number)
    metrics["transitionChecks"] = transition_checks
    # Generate has no baseline costs/hints. Early time breaks timing ties.
    # Repair retains its existing bounded integer lexicographic encoding.
    early_upper = len(events) * 19 * plan.preferences.earlyWeight
    secondary_upper = len(baseline) * (plan.preferences.changeWeight + plan.preferences.roomChangeWeight)
    secondary_scale = early_upper + 1
    primary_scale = (secondary_upper + 1) * secondary_scale
    objective = (secondary_scale * sum(teacher_time_terms) + sum(objective_terms) if mode == "generate" else
                 primary_scale * sum(baseline_change_terms) + secondary_scale * sum(weighted_change_terms) + sum(objective_terms))
    definition = ("lexicographic(weightedKnownEventTiming, weightedOrdinalLateness)" if mode == "generate" else
                  "lexicographic(baselineChanges, weightedSlotRoomChanges, weightedOrdinalLateness)")
    metrics["objectiveDefinition"] = definition
    metrics["objectiveScales"] = ({"teacherTime": secondary_scale} if mode == "generate" else
                                  {"baselineChanges": primary_scale, "weightedSlotRoomChanges": secondary_scale})

    def score(described):
        if mode == "generate":
            return _generate_objective(plan, described)
        return (primary_scale * described["baselineChanges"]
                + secondary_scale * (plan.preferences.changeWeight * described["changedSlots"]
                                     + plan.preferences.roomChangeWeight * described["changedRooms"])
                + plan.preferences.earlyWeight * described["earlySlotPenalty"])

    def extract(solver):
        assignments = []
        for index, (event_id, _demand, _occurrence) in enumerate(events):
            budget.tick()
            assignments.append({"eventId": event_id, "slotId": slots[solver.Value(event_vars[index][0])].id,
                                "roomId": rooms[solver.Value(event_vars[index][1])].id})
        return assignments

    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = search_workers
    solver.parameters.random_seed = 0
    solver.parameters.log_search_progress = False

    class Solutions(cp_model.CpSolverSolutionCallback):
        def on_solution_callback(self):
            metrics["solutionCount"] += 1

    if control is None:
        budget.stage("cp_sat_control")
        solver.parameters.max_time_in_seconds = max(0.000001, budget.remaining() - reserve)
        solver.parameters.stop_after_first_solution = True
        status = solver.Solve(model, Solutions())
        metrics.update(solverStatus=solver.StatusName(status), solverWallTimeSeconds=round(solver.WallTime(), 6),
                       branches=solver.NumBranches(), conflicts=solver.NumConflicts(), searchSolverStatus=solver.StatusName(status))
        if status == cp_model.INFEASIBLE:
            return result("infeasible", [*warnings, diagnostic("no_feasible_model", "CP-SAT доказал отсутствие расписания при заданных жёстких ограничениях. Точный набор причин не вычислялся.")])
        if status not in {cp_model.OPTIMAL, cp_model.FEASIBLE}:
            return result("invalid" if status == cp_model.MODEL_INVALID else "unknown",
                          [*warnings, diagnostic("invalid_solver_model" if status == cp_model.MODEL_INVALID else "search_unfinished",
                                                "Модель расчёта недопустима." if status == cp_model.MODEL_INVALID else "Поиск завершён без проверенного решения; неразрешимость не установлена.")])
        assignments = extract(solver)
        budget.stage("control_validation")
        checked = checked_candidate(assignments)
        if not checked["valid"]:
            timed_out = any(issue["code"] == "validation_time_limit" for issue in checked["issues"])
            return result("unknown" if timed_out else "invalid", [diagnostic("validation_time_limit" if timed_out else "validator_rejected", "Контрольный кандидат не прошёл независимую проверку.")], validation=checked)
        described = _assignment_metrics(plan, assignments, budget)
        control = {"assignments": assignments, "validation": checked, "metrics": described,
                   "method": "first_feasible_cp_sat_without_objective", "objective": score(described)}
        incumbent = control
        metrics.update(described, objective=control["objective"], bestBound=None, optimalityProven=False, baselinePreserved=False)
        comparison = _same_input_comparison(plan, described, described)
        budget.fallback_result = result("feasible", warnings, assignments, checked)
    model.Minimize(objective)
    # The accepted control is only a search incumbent, never a preservation cost.
    model.ClearHints()
    control_by_event = {item["eventId"]: item for item in incumbent["assignments"]}
    for index, (_event_id, _demand, _occurrence) in enumerate(events):
        previous = control_by_event[events[index][0]]
        model.AddHint(event_vars[index][0], slot_index[previous["slotId"]])
        model.AddHint(event_vars[index][1], room_index[previous["roomId"]])
    budget.stage("cp_sat_search")
    solver.parameters.stop_after_first_solution = False
    solver.parameters.max_time_in_seconds = max(0.000001, budget.remaining() - reserve)
    status = solver.Solve(model, Solutions())
    metrics.update(solverStatus=solver.StatusName(status), solverWallTimeSeconds=round(solver.WallTime(), 6),
                   branches=solver.NumBranches(), conflicts=solver.NumConflicts(), searchSolverStatus=solver.StatusName(status))
    if status in {cp_model.OPTIMAL, cp_model.FEASIBLE}:
        metrics.update(searchObjective=solver.ObjectiveValue(), searchBestBound=solver.BestObjectiveBound())
    if status not in {cp_model.OPTIMAL, cp_model.FEASIBLE}:
        if status in {cp_model.MODEL_INVALID, cp_model.INFEASIBLE}:
            return result("invalid", [diagnostic("inconsistent_optimization_model", "Оптимизационная модель отклонила ранее проверенный контрольный вариант.")])
        raise _BudgetExceeded(budget)
    assignments = extract(solver)
    budget.stage("independent_validation")
    checked = checked_candidate(assignments)
    if not checked["valid"]:
        if any(issue["code"] == "validation_time_limit" for issue in checked["issues"]):
            raise _BudgetExceeded(budget)
        return result("invalid", [diagnostic("validator_rejected", "Независимая проверка отклонила результат решателя.")], validation=checked)
    described = _assignment_metrics(plan, assignments, budget)
    # A time-limited search may find a worse incumbent; keep the verified control.
    if score(described) < incumbent["objective"]:
        metrics["incumbentImprovements"] += 1
    if score(described) > incumbent["objective"]:
        assignments, checked, described = incumbent["assignments"], incumbent["validation"], incumbent["metrics"]
        status = cp_model.FEASIBLE
    metrics.update(described, objective=score(described), bestBound=solver.BestObjectiveBound(),
                   consecutiveDefinition="adjacent_slot_numbers_within_day", baselinePreserved=False,
                   optimalityProven=status == cp_model.OPTIMAL)
    comparison = _same_input_comparison(plan, control["metrics"], described)
    completed = result("optimal" if status == cp_model.OPTIMAL else "feasible", warnings, assignments, checked)
    budget.fallback_result = completed
    budget.check()
    return completed


def solve_plan(value: PlanningInput | dict, mode: str = "generate", time_limit: float = 10, *,
               search_workers: int = 1, progress_callback=None, candidate_validator=None) -> dict:
    """One walltime budget covers validation, indexing, construction and search.

    Python preprocessing checks the deadline cooperatively at bounded intervals.
    CP-SAT receives only the remaining budget and the explicit worker count. A deadline
    without a previously established proof is always unknown, never infeasible.
    """
    try:
        return _solve_plan(value, mode, time_limit, search_workers=search_workers,
                           progress_callback=progress_callback, candidate_validator=candidate_validator)
    except _BudgetExceeded as error:
        budget = error.budget
        complexity_limit = isinstance(error, _TransitionLimitExceeded)
        metrics = dict(budget.metrics or {})
        metrics.update(solverStatus="UNKNOWN", optimalityProven=False, wallTimeSeconds=round(time.monotonic() - budget.started, 6))
        if budget.fallback_result is not None:
            fallback = dict(budget.fallback_result)
            fallback["status"] = "feasible"
            # Candidate quality belongs to its validated assignments. Search
            # observations are separate and must not replace that quality.
            observed = {key: metrics[key] for key in (
                "candidateCount", "candidateCountMeaning", "solutionCount", "incumbentImprovements",
                "searchSolverStatus", "searchObjective", "searchBestBound", "solverWallTimeSeconds",
                "branches", "conflicts", "transitionChecks", "objectiveScales",
                "finalizationReserveSeconds", "localSearch") if key in metrics}
            fallback["metrics"] = {**fallback["metrics"], **observed, "solverStatus": "UNKNOWN", "optimalityProven": False,
                                   "phase": metrics.get("phase"), "wallTimeSeconds": metrics["wallTimeSeconds"]}
            fallback["diagnostics"] = [*fallback["diagnostics"], {"code": "transition_model_limit" if complexity_limit else "optimization_time_limit", "message": "Достигнут предел сложности переходов; сохранён проверенный вариант. Сузьте допустимые интервалы или кабинеты." if complexity_limit else "Поиск улучшений не завершён; сохранён независимо проверенный допустимый вариант.", "entityIds": []}]
            return fallback
        return {"status": "unknown", "assignments": [],
                "diagnostics": [{"code": "transition_model_limit" if complexity_limit else "time_limit", "message": "Достигнут предел сложности переходов; сузьте интервалы или кабинеты. Неразрешимость не установлена." if complexity_limit else "Предел времени исчерпан; неразрешимость не установлена.", "entityIds": []}],
                "metrics": metrics, "validation": {"valid": False, "issues": []}, "control": None, "comparison": None}
