"""Bounded incumbent proposals; only the caller can certify and accept them.

The model and assignments must already be validated. Occupancy and domain
filters only shortlist single-event or same-occurrence sync-bundle moves.
Every acceptance remains the responsibility of the full-model callback.
"""
from collections import defaultdict
from collections.abc import Callable
import math
import time

from .scheduling_models import PlanningInput, canonical, eligible_room_ids, groups_overlap, minutes


def improve_incumbent(plan: PlanningInput, assignments: list, *, seconds: float,
                      accept_candidate: Callable[[list, int], bool]) -> dict:
    """Propose strict generate-objective improvements within a local deadline.

    The callback must independently validate a proposal, verify its exact
    objective delta and preserve it as the caller's fallback before returning
    True. Computational moves never become school pins or source facts.
    """
    started = time.monotonic()
    stats = {"status": "completed", "reason": None, "acceptedMoves": 0,
             "attemptedCandidates": 0, "rejectedCandidates": 0,
             "enumeratedMoves": 0, "objectiveDelta": 0, "elapsedSeconds": 0.0}

    def finish(status="completed", reason=None):
        stats.update(status=status, reason=reason, elapsedSeconds=round(time.monotonic() - started, 6))
        return stats

    if (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
            or not math.isfinite(seconds) or seconds < 0):
        return finish("skipped", "invalid_budget")
    if seconds <= 1:
        return finish("skipped", "insufficient_budget")
    search_end = started + min(float(seconds), 30.0) - 1.0

    def expired():
        return time.monotonic() >= search_end

    if any(t.presenceEvents or t.requiredBreaks or t.minTransitionMinutes for t in plan.teachers) or plan.roomTransitions:
        return finish("skipped", "rich_teacher_timing")
    ordered_slots = sorted(plan.slots, key=lambda s: (s.day, s.start, s.end, s.id))
    if any(a.day == b.day and b.start < a.end for a, b in zip(ordered_slots, ordered_slots[1:])):
        return finish("skipped", "overlapping_slots")

    slots = {s.id: s for s in plan.slots}
    slot_times = {s.id: (minutes(s.start), minutes(s.end)) for s in plan.slots}
    groups = {g.id: g for g in plan.groups}
    rooms = {r.id: r for r in plan.rooms}
    teachers = {t.id: t for t in plan.teachers}
    tags = {r.id: {canonical(t) for t in r.tags} for r in plan.rooms}
    demands = {f"{d.id}_{n}": d for d in plan.demands for n in range(d.weeklyCount)}
    current = {a["eventId"]: dict(a) for a in assignments}
    sync, teacher_events = defaultdict(list), defaultdict(set)
    for event, demand in demands.items():
        if expired():
            return finish("time_limit", "deadline")
        key = (demand.syncKey, int(event.rsplit("_", 1)[1])) if demand.syncKey else (event,)
        sync[key].append(event)
        for teacher in demand.teacherIds:
            teacher_events[teacher].add(event)
    bundles = sorted(tuple(sorted(events)) for events in sync.values())
    pinned = {a.eventId for a in plan.pins}
    related = {}
    for group in plan.groups:
        if expired():
            return finish("time_limit", "deadline")
        related[group.id] = {other.id for other in plan.groups if groups_overlap(group, other)}
    student_conflict, eligible_rooms, allowed_targets = {}, {}, {}
    for event, demand in demands.items():
        if expired():
            return finish("time_limit", "deadline")
        student_conflict[event] = set().union(*(related[g] for g in demand.groupIds))
        eligible_rooms[event] = eligible_room_ids(demand, groups, rooms, tags)
        owners = [*(teachers[t] for t in demand.teacherIds), *(groups[g] for g in student_conflict[event])]
        blocked = {slot for owner in owners for slot in owner.unavailableSlotIds}
        allowed_targets[event] = {s.id for s in plan.slots
                                  if s.id not in blocked and (not demand.allowedSlotIds or s.id in demand.allowedSlotIds)
                                  and all(not teachers[t].workWindows or any(w.day == s.day and w.start <= s.start and s.end <= w.end
                                                                            for w in teachers[t].workWindows)
                                          for t in demand.teacherIds)}

    def day_cost(teacher, day, changes):
        intervals = [slot_times[slot_id] for event in teacher_events[teacher]
                     if slots[slot_id := changes.get(event, current[event]["slotId"])].day == day]
        if not intervals:
            return 0
        span = max(end for _, end in intervals) - min(start for start, _ in intervals)
        occupied = sum(end - start for start, end in intervals)
        return (plan.preferences.gapWeight * (span - occupied)
                + plan.preferences.spanWeight * span + plan.preferences.dayWeight)

    scale = len(assignments) * 19 * plan.preferences.earlyWeight + 1
    for _ in range(30):
        if expired():
            return finish("time_limit", "deadline")
        occupancy = defaultdict(set)
        for event, assignment in current.items():
            occupancy[assignment["slotId"]].add(event)
        old_cost = {(teacher.id, day): day_cost(teacher.id, day, {}) for teacher in plan.teachers for day in range(6)}
        proposals = []
        for bundle in bundles:
            if expired():
                return finish("time_limit", "deadline")
            if pinned.intersection(bundle):
                continue
            affected = {t for event in bundle for t in demands[event].teacherIds}
            old_days = {slots[current[event]["slotId"]].day for event in bundle}
            for target in plan.slots:
                stats["enumeratedMoves"] += 1
                if stats["enumeratedMoves"] % 64 == 0 and expired():
                    return finish("time_limit", "deadline")
                if (all(current[e]["slotId"] == target.id for e in bundle)
                        or any(target.id not in allowed_targets[e] for e in bundle)):
                    continue
                changes = {event: target.id for event in bundle}
                delta = sum(day_cost(t, day, changes) - old_cost[t, day]
                            for t in affected for day in old_days | {target.day}) * scale
                delta += plan.preferences.earlyWeight * sum(target.slot - slots[current[e]["slotId"]].slot for e in bundle)
                if delta >= 0:
                    continue
                occupied = occupancy[target.id] - set(bundle)
                used_rooms = {current[e]["roomId"] for e in occupied}
                choices = {}
                for event in bundle:
                    demand = demands[event]
                    if any(set(demand.teacherIds).intersection(demands[other].teacherIds)
                           or student_conflict[event].intersection(demands[other].groupIds) for other in occupied):
                        break
                    available = [r for r in eligible_rooms[event]
                                 if r not in used_rooms and target.id not in rooms[r].unavailableSlotIds]
                    if not available:
                        break
                    room = current[event]["roomId"] if current[event]["roomId"] in available else sorted(available)[0]
                    choices[event] = {"eventId": event, "slotId": target.id, "roomId": room}
                    used_rooms.add(room)
                if len(choices) == len(bundle):
                    proposals.append((delta, bundle, target.id, choices))
        adopted = False
        for delta, _bundle, _target, choices in sorted(proposals, key=lambda item: item[:3]):
            if expired():
                return finish("time_limit", "deadline")
            stats["attemptedCandidates"] += 1
            candidate = [dict(choices.get(event, assignment)) for event, assignment in current.items()]
            if accept_candidate(candidate, delta) is True:
                current = {a["eventId"]: dict(a) for a in candidate}
                stats["acceptedMoves"] += 1
                stats["objectiveDelta"] += delta
                adopted = True
                break
            stats["rejectedCandidates"] += 1
        if not adopted:
            return finish()
    return finish(reason="step_limit")
