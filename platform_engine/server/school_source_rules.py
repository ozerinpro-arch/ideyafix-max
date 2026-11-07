"""Curated, hash-bound HARD fragments of the supplied school conditions.

Pure: no persistence, text execution, name guessing, invented load or rooms.
A partial fragment never certifies the rest of its source condition.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
from hashlib import sha256

from .scheduling_models import PlanningInput, canonical
from .subject_names import class_grade
from .school_rule_updates import SourceRuleUpdate

# Exact UTF-8 text hashes from the reviewed 2026-10-24 source package. A changed
# text (even with the same numerical condition ID) needs a new review.
REVIEWED_TEXT_HASHES = {}  # No original organisation documents are authority here.

SPEC_INFORMATICS = {'спец.информатика', 'спец. информатика', 'специальная информатика', 'специальный курс по информатике'}
SPEC_RUSSIAN = {'спец.русский', 'спец. русский', 'специальный курс по русскому языку'}
UNITY = {'unity', 'программирование unity'}


def _observed_weekly_counts(demands):
    """Positive applicability slices only; no school-year limits are inferred.

    Sweep explicit inclusive date bounds as ordinal boundaries, and explicit
    week types plus the default category for untyped demands. This avoids
    adding two disjoint two-hour modules into a false four-hour load.
    """
    kinds = {None, *(canonical(kind) for demand in demands for kind in demand['weekTypes'])}
    counts = set()
    for kind in kinds:
        # Bound the model's representable ISO domain, never the school year.
        # The default category must not create a slice before year 1 or after
        # year 9999 when an explicit bound is at that exact legal endpoint.
        current, changes = 0, {date.min.toordinal(): 0}
        for demand in demands:
            if demand['weekTypes'] and kind not in {canonical(value) for value in demand['weekTypes']}:
                continue
            count = demand['weeklyCount']
            if demand['activeFrom']:
                begin = date.fromisoformat(demand['activeFrom']).toordinal()
                changes[begin] = changes.get(begin, 0) + count
            else:
                current += count
            if demand['activeUntil']:
                finish = date.fromisoformat(demand['activeUntil']).toordinal() + 1
                changes[finish] = changes.get(finish, 0) - count
        for boundary in sorted(changes):
            if boundary > date.max.toordinal():
                break
            current += changes[boundary]
            if current:
                counts.add(current)
    return counts


def _text_hash(text):
    return sha256(text.encode('utf-8')).hexdigest()


class _Context:
    def __init__(self, condition, resolutions, plan, updates=()):
        self.condition, self.resolutions, self.plan = condition, resolutions, plan
        self.updates = updates
        self.teacher_ids, self.demand_ids, self.steps, self.issues = set(), set(), [], []
        self.slots = {s['id']: s for s in plan['slots']}
        self.teachers = {t['id']: t for t in plan['teachers']}
        self.groups = {g['id']: g for g in plan['groups']}

    def issue(self, code, message, **details):
        self.issues.append({'conditionId': self.condition['id'], 'textHash': _text_hash(self.condition['text']),
                            'code': code, 'message': message, 'sourceCells': [], **details})

    def teacher(self, name):
        mapping = self.resolutions.get('teacherMap', {})
        if name in mapping:
            teacher_id = mapping[name]
            if teacher_id not in self.teachers:
                self.issue('source_rule_reference_required', f'Сопоставление {name} не ссылается на существующего педагога.')
                return None
        else:
            exact = [t['id'] for t in self.teachers.values() if canonical(t['name']) == canonical(name)]
            if len(exact) != 1:
                self.issue('source_rule_reference_required', f'Нужно точное сопоставление педагога {name}; похожие имена не используются.')
                return None
            teacher_id = exact[0]
        self.teacher_ids.add(teacher_id)
        return teacher_id

    def class_names(self, *, label=None, grades=None):
        mapping = self.resolutions.get('classMap', {})
        names = {target for raw, target in mapping.items()
                 if (label is not None and canonical(raw) == canonical(label))
                 or (grades is not None and class_grade(raw) in grades)}
        if not names:
            self.issue('source_rule_reference_required', 'Нужна явная карта классов для условия: ' + (label or ', '.join(map(str, sorted(grades)))))
        return names

    def demands(self, teacher_id, *, subjects=None, classes=None):
        selected = [d for d in self.plan['demands'] if teacher_id in d['teacherIds']
                    and (subjects is None or canonical(d['subject']) in {canonical(s) for s in subjects})
                    and (classes is None or any(self.groups[g]['className'] in classes for g in d['groupIds']))]
        if not selected:
            self.issue('source_rule_reference_required', 'Не найдена явно заданная нагрузка нужного предмета и классов.', teacherIds=[teacher_id])
        return selected

    def restrict(self, demand, predicate, message):
        if not self.slots:
            self.issue('source_rule_reference_required', 'Сначала задайте подтверждённые учебные слоты.')
            return
        original = demand['allowedSlotIds']
        eligible = original or list(self.slots)
        remaining = [key for key in eligible if predicate(self.slots[key])]
        if not remaining:
            # [] means ANY in PlanningInput, so never store an empty intersection.
            self.issue('source_rule_empty_domain', 'Условие несовместимо с разрешёнными слотами нагрузки; прежний список сохранён.', demandIds=[demand['id']])
            return
        if len(remaining) != len(eligible):
            demand['allowedSlotIds'] = remaining
        self.demand_ids.add(demand['id'])
        self.steps.append(message)

    def availability(self, teacher_id, predicate, message):
        if teacher_id is None:
            return
        if not self.slots:
            self.issue('source_rule_reference_required', 'Сначала задайте подтверждённые учебные слоты.')
            return
        teacher = self.teachers[teacher_id]
        blocked = {key for key, slot in self.slots.items() if not predicate(slot)}
        previous = teacher['unavailableSlotIds']
        proposed = [*previous, *sorted(blocked - set(previous))]
        overlap = lambda a, b: a['day'] == b['day'] and a['start'] < b['end'] and b['start'] < a['end']
        def available(slot_id, unavailable):
            slot = self.slots[slot_id]
            windows = teacher['workWindows']
            return (not any(overlap(slot, self.slots[other]) for other in unavailable)
                    and (not windows or any(w['day'] == slot['day'] and w['start'] <= slot['start'] and slot['end'] <= w['end'] for w in windows)))
        for demand in self.plan['demands']:
            if teacher_id not in demand['teacherIds']:
                continue
            eligible = demand['allowedSlotIds'] or list(self.slots)
            if any(available(s, previous) for s in eligible) and not any(available(s, proposed) for s in eligible):
                self.issue('source_rule_empty_domain', 'Недоступность педагога исключает все разрешённые слоты нагрузки; прежние запреты сохранены.', teacherIds=[teacher_id], demandIds=[demand['id']])
                return
        teacher['unavailableSlotIds'] = proposed
        self.steps.append(message)

    def teacher_rule(self, name, predicate, message):
        update = next((item for item in self.updates
                       if item.conditionId == self.condition['id'] and item.teacher == name), None)
        if update:
            if update.textHash != _text_hash(self.condition['text']):
                self.issue('source_rule_update_stale', 'Уточнение относится к другой версии исходного условия.')
                return
            teacher_id = self.teacher(name)
            if teacher_id is None:
                return
            if update.effect == 'personal_exemption':
                self.steps.append(f'{name}: персональное исключение из этого условия. {update.reason}')
                # Unavailability has no source ownership. Never remove a saved
                # manual/other-source ban merely because this overlay changed.
                if any(not predicate(self.slots[key]) for key in self.teachers[teacher_id]['unavailableSlotIds']):
                    self.issue('source_rule_existing_unavailability',
                               'Ранее сохранённая недоступность сохранена: её происхождение нужно проверить отдельно.',
                               teacherIds=[teacher_id])
                return
            if update.effectiveFrom is None:
                self.issue('source_rule_effective_date_required',
                           'Условие действует со второй четверти; точная дата начала не подтверждена. Запрет не добавлен.',
                           teacherIds=[teacher_id])
                return
            first = date.fromisoformat(self.plan['periodStart'])
            original = predicate
            predicate = lambda slot: ((first + timedelta(days=slot['day'])).isoformat() < update.effectiveFrom
                                      or original(slot))
            message += f' Начало действия: {update.effectiveFrom}.'
        self.availability(self.teacher(name), predicate, message)

    def demand_rule(self, name, predicate, message, *, subjects=None, classes=None):
        teacher_id = self.teacher(name)
        if teacher_id is None:
            return []
        demands = self.demands(teacher_id, subjects=subjects, classes=classes)
        for demand in demands:
            self.restrict(demand, predicate, message)
        return demands

    def required_course(self, subjects, *, teacher_id=None, classes=None, weekly_count=None):
        """Presence/count checks never create load, membership or calendar dates."""
        selected = [demand for demand in self.plan['demands']
                    if canonical(demand['subject']) in {canonical(subject) for subject in subjects}
                    and (teacher_id is None or teacher_id in demand['teacherIds'])]
        if not selected:
            self.issue('source_rule_required_load', 'В нормативной модели отсутствует явно названный обязательный курс: ' + ', '.join(sorted(subjects)) + '.')
            return []
        if classes is None:
            self.demand_ids.update(demand['id'] for demand in selected)
            self.steps.append('Наличие отдельной нагрузки названного курса проверено; часы и участники не добавлялись.')
            return selected
        if not classes:
            self.issue('source_rule_required_load', 'Для проверки обязательного курса нужны явные сопоставления его классов.')
            return []
        relevant = []
        for class_name in sorted(classes):
            group_demands = {}
            for demand in selected:
                for group_id in demand['groupIds']:
                    if self.groups[group_id]['className'] == class_name:
                        group_demands.setdefault(group_id, []).append(demand)
            if not group_demands:
                self.issue('source_rule_required_load', f'В классе {class_name} отсутствует обязательный курс с указанным педагогом.', teacherIds=[teacher_id])
                continue
            for group_id, demands in group_demands.items():
                observed = _observed_weekly_counts(demands)
                if weekly_count is not None and observed != {weekly_count}:
                    self.issue('source_rule_required_load', f'Группа {group_id}: в непустых срезах действия курса требуется {weekly_count} занятия в неделю; найдено {", ".join(map(str, sorted(observed))) or "0"}. Даты, типы недель и часы не менялись.', groupIds=[group_id], demandIds=[d['id'] for d in demands])
                else:
                    self.steps.append(f'Обязательная нагрузка группы {group_id}: {weekly_count} занятия в каждом непустом срезе действия.')
                    self.demand_ids.update(d['id'] for d in demands)
                relevant.extend(demands)
        return relevant


def _apply(context):
    # Unknown source conditions retain the existing explicit-resolution gate.
    # Historical named-person rules are not part of this independent platform.
    return 'Требуется явное подтверждение условия собственного источника.'


def apply_source_rules(package, resolutions, plan, provenance):
    """Apply only reviewed hard fragments; return a detached validated input.

    ``source_rule_empty_domain`` and normative ``source_rule_required_load``
    are never resolved by a manual attestation. Other residuals retain their exact conditionId for
    the compiler's existing planHash+packageHash manual-review lifecycle.
    ``provenance`` is accepted for the compiler contract, never used as authority
    to infer teachers, subjects, classes, dates or rooms.
    """
    value = PlanningInput.model_validate(plan).model_dump(mode='json')
    before = deepcopy(value)
    if not isinstance(resolutions, dict):
        raise ValueError('Требуется объект сопоставлений источников')
    applied, partial, unresolved = [], [], []
    updates = [SourceRuleUpdate.model_validate(item) for item in package.get('ruleUpdates', [])]
    for condition in package.get('conditions', []):
        if not isinstance(condition, dict) or not isinstance(condition.get('id'), str) or not isinstance(condition.get('text'), str):
            raise ValueError('Условие должно содержать id и точный текст')
        context = _Context(condition, resolutions, value, updates)
        key, actual_hash = condition['id'], _text_hash(condition['text'])
        if key not in REVIEWED_TEXT_HASHES:
            context.issue('source_rule_manual_required', 'Условие не автоматизировано; мягкое пожелание или неоднозначные связи не повышаются до запрета.')
            unresolved.extend(context.issues)
            continue
        if REVIEWED_TEXT_HASHES[key] != actual_hash:
            context.issue('source_rule_text_changed', 'Текст условия изменился после проверки; прежняя автоматизация не применялась.')
            unresolved.extend(context.issues)
            continue
        remaining = _apply(context)
        report = {'conditionId': key, 'textHash': actual_hash, 'teacherIds': sorted(context.teacher_ids),
                  'demandIds': sorted(context.demand_ids), 'message': ' '.join(dict.fromkeys(context.steps))}
        if remaining:
            context.issue('source_rule_partial', remaining)
        if context.issues:
            if context.steps:
                partial.append({**report, 'remaining': remaining or 'Исправьте перечисленные привязки или противоречия.'})
            unresolved.extend(context.issues)
        else:
            applied.append(report)
    if value != before:
        value['confirmed'] = False
    return {'input': PlanningInput.model_validate(value).model_dump(mode='json'), 'appliedConditions': applied,
            'partialConditions': partial, 'unresolved': unresolved}
