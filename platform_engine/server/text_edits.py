"""Pure, fail-closed preparation of a text edit. No model, clock, or database IO."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
import unicodedata

from pydantic import ValidationError

from .text_edit_models import LessonSelector, TextEditPlan
from .timetable import EditableLessonInput, MAX_LESSONS, PUBLIC_LESSON_KEYS, canonical, compute_issues

EDITABLE = ("className", "day", "slot", "start", "end", "subject", "teachers", "rooms")
MAX_AFFECTED = 100
MAX_MESSAGES = 40
MAX_USER_TEXT = 32000
UNCHECKED = [
    "Квалификация преподавателей не проверена: нужен подтверждённый учебный план.",
    "Дневная и недельная нагрузка учеников и преподавателей не проверена: нужны подтверждённые лимиты.",
    "Доступность преподавателей и кабинетов не проверена: нужны подтверждённые ограничения.",
    "Вместимость и оснащение кабинетов не проверены: нужны подтверждённые составы групп и параметры кабинетов.",
]
DAY_WORDS = (
    ("понедельник", "понедельника", "понедельнику", "понедельнике", "понедельником", "пн"),
    ("вторник", "вторника", "вторнику", "вторнике", "вторником", "вт"),
    ("среда", "среду", "среды", "среде", "средой", "ср"),
    ("четверг", "четверга", "четвергу", "четверге", "четвергом", "чт"),
    ("пятница", "пятницу", "пятницы", "пятнице", "пятницей", "пт"),
)
ORDINAL_STEMS = ("перв", "втор", "трет", "четвёрт", "пят", "шест", "седьм", "восьм", "девят", "десят",
                 "одиннадцат", "двенадцат", "тринадцат", "четырнадцат", "пятнадцат", "шестнадцат",
                 "семнадцат", "восемнадцат", "девятнадцат", "двадцат")
SCOPE_RISK = re.compile(
    r"(?<!\w)(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?)(?!\w)"
    r"|(?<!\w)\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)(?!\w)"
    r"|(?<!\w)(?:сегодня|завтра|послезавтра|вчера|ежедневно|еженедельно|ежемесячно)(?!\w)"
    r"|(?<!\w)(?:следующ\w*|будущ\w*|прошл\w*)\s+недел\w*"
    r"|(?<!\w)(?:следующ\w*|ближайш\w*|прошл\w*)\s+(?:понедельник\w*|вторник\w*|сред\w*|четверг\w*|пятниц\w*)"
    r"|(?<!\w)через\s+(?:\d+\s+)?(?:день|дня|дней|недел\w*)(?!\w)"
    r"|(?<!\w)(?:кажд\w*)\s+(?:день|недел\w*|понедельник\w*|вторник\w*|сред\w*|четверг\w*|пятниц\w*)"
    r"|(?<!\w)(?:до|после)\s+каникул\w*"
)
CURRENT_GRID = re.compile(r"(?<!\w)(?:текущ\w*\s+(?:недел\w*|сетк\w*)|эт\w*\s+недел\w*|недельн\w*\s+черновик\w*)(?!\w)")
SCOPE_REFUSAL = re.compile(r"(?<!\w)(?:не|нет|ни|нельзя|отказываюсь|отменяю|неизменн\w*)(?!\w)|(?<!\w)без\s+(?:любых\s+)?изменений(?!\w)")
BULK = re.compile(r"(?<!\w)(?:все|всех|всем|всеми|каждый|каждую|каждое|целиком|полностью)(?!\w)")


def _question(path, message, candidates=()):
    return {"path": path, "message": message, "candidateIds": list(candidates)}


def _result(status, revision, message, *, questions=None, proposal=None, issues=None):
    return {"status": status, "revision": revision, "message": message,
            "questions": questions or [], "proposal": proposal, "issues": issues or [],
            "uncheckedChecks": list(UNCHECKED)}


def _quoted(value, text):
    """Literal canonical text with token boundaries, never stemming or fuzzy match."""
    needle = canonical(value)
    return bool(needle and re.search(r"(?<!\w)" + re.escape(needle) + r"(?!\w)", text))


def _slot_quoted(number, text):
    if re.search(r"(?<![\w:])" + str(number) + r"(?:-?(?:й|ый|ой|го|м|му|я|ую|е|ого|ому))?(?![\w:])", text):
        return True
    if number == 3 and any(_quoted(word, text) for word in
                           ("третий", "третья", "третье", "третью", "третьего", "третьей", "третьему", "третьим", "третьем")):
        return True
    stem = ORDINAL_STEMS[number - 1]
    stems = [stem, stem.replace("ё", "е")]
    # Bound the ending: e.g. «вторник» must not authorize «второй урок».
    return any(re.search(r"(?<!\w)" + re.escape(part) + r"(?:ый|ой|ий|ого|его|ому|ему|ым|им|ом|ем|ую|юю|ая|яя|ое|ее)(?!\w)", text)
               for part in stems)


def _day_quoted(day, text):
    return any(_quoted(word, text) for word in DAY_WORDS[day]) or bool(
        re.search(r"(?<!\w)day\s*[:=]\s*" + str(day) + r"(?!\d)", text))


def _clock_quoted(value, text):
    hour, minute = value.split(":")
    return bool(re.search(r"(?<!\d)0?" + str(int(hour)) + ":" + minute + r"(?!\d)", text))


def _grounded(fields, text, path):
    errors = []
    for key, value in fields.items():
        if value is None or key == "match":
            continue
        values = value if isinstance(value, list) else [value]
        if key in {"className", "subject", "teacher", "room", "teachers", "rooms", "lessonId"}:
            if any(not _quoted(item, text) for item in values):
                errors.append(f"{path}.{key}: значение должно быть буквально указано пользователем.")
            if isinstance(value, list) and not value:
                noun = r"(?:учител\w*|преподавател\w*)" if key == "teachers" else r"кабинет\w*"
                if not re.search(r"(?<!\w)(?:без\s+" + noun + r"|(?:убери|удали|очисти|сними)\s+" + noun + r")", text):
                    errors.append(f"{path}.{key}: очистку списка должен явно запросить пользователь.")
        elif key == "day" and not _day_quoted(value, text):
            errors.append(f"{path}.day: день недели не подтверждён текстом пользователя.")
        elif key == "slot" and not _slot_quoted(value, text):
            errors.append(f"{path}.slot: номер урока не подтверждён текстом пользователя.")
        elif key in {"start", "end"} and not _clock_quoted(value, text):
            errors.append(f"{path}.{key}: явное время не указано пользователем.")
    return errors


def _bulk_requested(evidence):
    text = canonical(evidence)
    return bool(BULK.search(text) and not re.search(
        r"(?<!\w)(?:не\s+(?:все|всех|всем|всеми|кажд\w*)|кроме|исключая|за\s+исключением)(?!\w)", text))


def _matches(selector: LessonSelector, lesson):
    for field in ("lessonId", "className", "day", "slot", "subject", "teacher", "room"):
        wanted = getattr(selector, field)
        if wanted is None:
            continue
        if field in {"teacher", "room"}:
            if canonical(wanted) not in {canonical(value) for value in lesson[field + "s"]}:
                return False
        elif field in {"day", "slot"}:
            if wanted != lesson[field]:
                return False
        elif field == "lessonId":
            if wanted != lesson["id"]:
                return False
        elif canonical(wanted) != canonical(lesson[field]):
            return False
    return True


def _public_lesson(lesson):
    result = {field: deepcopy(lesson[field]) for field in PUBLIC_LESSON_KEYS}
    for public, internal in (("planningEventId", "_planningEventId"), ("planningGroups", "_planningGroups")):
        if public in lesson or internal in lesson:
            result[public] = deepcopy(lesson[public] if public in lesson else lesson[internal])
    return result


def _linked(lesson):
    return any(key.startswith("_planning") or key.startswith("planning") for key in lesson)


def _editable(lesson):
    return {field: deepcopy(lesson[field]) for field in EDITABLE}


def _conflicts(lessons, touched):
    """At most 100 x 2000 comparisons. Old unrelated issues cannot block a patch."""
    prepared = []
    for lesson in lessons:
        prepared.append((lesson, canonical(lesson["className"]),
                         {canonical(value) for value in lesson["teachers"]},
                         {canonical(value) for value in lesson["rooms"]}))
    issues, seen = [], set()
    for first, class_one, teachers, rooms in prepared:
        if first["id"] not in touched:
            continue
        for second, class_two, other_teachers, other_rooms in prepared:
            if first["id"] == second["id"] or first["day"] != second["day"]:
                continue
            if max(first["start"], second["start"]) >= min(first["end"], second["end"]):
                continue
            pair = tuple(sorted((first["id"], second["id"])))
            for kind, overlap in (("class", class_one == class_two),
                                  ("teacher", bool(teachers & other_teachers)),
                                  ("room", bool(rooms & other_rooms))):
                if not overlap or (kind, pair) in seen:
                    continue
                seen.add((kind, pair))
                digest = hashlib.sha256((kind + "|" + "|".join(pair)).encode()).hexdigest()[:16]
                label = {"class": "класса", "teacher": "преподавателя", "room": "кабинета"}[kind]
                issues.append({"id": "text_" + digest, "kind": kind, "severity": "error",
                               "message": f"Итоговая правка создаёт или сохраняет пересечение {label}.", "lessonIds": list(pair)})
                if len(issues) >= 100:
                    return issues
    return issues


def prepare_text_edit(plan, draft, user_messages, *, answers=None):
    """Resolve selectors against the original draft and simulate all operations.

    ``answers`` maps ``operations.N.selector``/``target`` to one ID from that
    selector's current ambiguous candidate set. The caller binds replies to its
    persisted question, owner, role and turn; no proposal is applied here.
    """
    revision = draft.get("revision", 0) if isinstance(draft, dict) else 0
    if type(revision) is not int or not 0 <= revision <= 2**31 - 1:
        return _result("blocked", 0, "Некорректная ревизия черновика.")
    try:
        if isinstance(plan, TextEditPlan):
            plan = plan.model_dump(exclude_none=True)
        plan = TextEditPlan.model_validate(plan)
    except (ValidationError, TypeError, ValueError):
        return _result("blocked", revision, "План не соответствует ограниченной схеме текстовой правки.")
    if (not isinstance(user_messages, list) or not 1 <= len(user_messages) <= MAX_MESSAGES or
            any(not isinstance(value, str) for value in user_messages)):
        return _result("blocked", revision, "Требуются ограниченные исходные сообщения пользователя.")
    joined = "\n".join(user_messages)
    if not joined.strip() or len(joined) > MAX_USER_TEXT or any(
            unicodedata.category(char) == "Cs" or (unicodedata.category(char) == "Cc" and char not in "\r\n\t")
            for char in joined):
        return _result("blocked", revision, "Исходный текст превышает лимит или содержит недопустимые символы.")
    if answers is None:
        answers = {}
    if not isinstance(answers, dict) or len(answers) > 20 or any(
            not isinstance(key, str) or not isinstance(value, str) for key, value in answers.items()):
        return _result("blocked", revision, "Некорректные ответы на уточнения.")
    final_text = canonical(user_messages[-1])
    # A mention of the current week is not an affirmative scope confirmation:
    # «не на этой неделе» / «текущую неделю не изменяй» must keep the gate shut.
    explicit_current = CURRENT_GRID.search(final_text) and not SCOPE_REFUSAL.search(final_text)
    if SCOPE_RISK.search(canonical(joined)) and not (
            explicit_current and not SCOPE_RISK.search(final_text)):
        return _result("needs_clarification", revision, "Дата или повторение не привязаны к подтверждённой неделе.", questions=[
            _question("scope", "Подтвердите правку только текущей недельной сетки и укажите день недели; даты и повторения не применяются.")])
    original = draft.get("lessons")
    if not isinstance(original, list) or len(original) > MAX_LESSONS:
        return _result("blocked", revision, "Черновик не соответствует ограниченной схеме расписания.")
    try:
        if any(not isinstance(row, dict) for row in original):
            raise ValueError
        public = [_public_lesson(row) for row in original]
        if any(not isinstance(row["id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", row["id"]) for row in public):
            raise ValueError
        if len({row["id"] for row in public}) != len(public):
            raise ValueError
        for row in public:
            if type(row["edited"]) is not bool or any(not isinstance(row[field], str) for field in ("raw", "sourceCell")):
                raise ValueError
            if any(unicodedata.category(char) == "Cs" or (unicodedata.category(char) == "Cc" and char not in "\r\n\t")
                   for field in ("raw", "sourceCell") for char in row[field]):
                raise ValueError
            EditableLessonInput.model_validate(_editable(row))
    except (KeyError, TypeError, ValidationError, ValueError):
        return _result("blocked", revision, "Черновик содержит некорректные или повторяющиеся уроки.")
    text = canonical(joined)
    errors, questions, chosen, used_answers = [], [], {}, set()
    resources = {
        "className": {canonical(row["className"]): row["className"] for row in public},
        "teachers": {canonical(name): name for row in public for name in row["teachers"]},
        "rooms": {canonical(name): name for row in public for name in row["rooms"]},
    }
    for number, operation in enumerate(plan.operations):
        path = f"operations.{number}"
        if operation.evidence not in joined or not operation.evidence.strip():
            errors.append(f"{path}.evidence: отсутствует буквальная цитата сообщения пользователя.")
        for name in ("selector", "target"):
            selector = getattr(operation, name)
            if selector is None:
                continue
            selector_path = path + "." + name
            errors.extend(_grounded(selector.model_dump(exclude_none=True), text, selector_path))
            if selector.match == "all" and not _bulk_requested(operation.evidence):
                errors.append(f"{selector_path}.match: массовая правка требует явного указания «все» или «каждый» без исключений в цитате операции.")
            candidates = [row["id"] for row in public if _matches(selector, row)]
            if len(candidates) > MAX_AFFECTED:
                errors.append(f"{selector_path}: превышен лимит 100 затронутых уроков.")
            elif not candidates:
                questions.append(_question(selector_path, "По точным признакам урок не найден. Укажите класс, день, номер или ID урока."))
            elif selector.match == "one" and len(candidates) > 1:
                if selector_path in answers:
                    if answers[selector_path] not in candidates:
                        errors.append(f"{selector_path}: ответ не входит в предложенные варианты.")
                    else:
                        chosen[selector_path] = [answers[selector_path]]
                        used_answers.add(selector_path)
                else:
                    questions.append(_question(selector_path, "Найдено несколько уроков. Выберите один ID или уточните точные признаки.", candidates))
            else:
                chosen[selector_path] = candidates
        payload = operation.changes if operation.kind == "update" else operation.lesson if operation.kind == "add" else None
        if payload is not None:
            fields = payload.model_dump(exclude_none=True)
            errors.extend(_grounded(fields, text, path + (".changes" if operation.kind == "update" else ".lesson")))
            for field in ("className", "teachers", "rooms"):
                if field not in fields:
                    continue
                values = fields[field] if isinstance(fields[field], list) else [fields[field]]
                if any(canonical(value) not in resources[field] for value in values):
                    questions.append(_question(path + "." + ("changes" if operation.kind == "update" else "lesson") + "." + field,
                                               "Ресурс отсутствует в черновике. Укажите точное существующее название; новые ресурсы требуют отдельной настройки."))
    if set(answers) - used_answers:
        errors.append("Ответ не связан с текущим неоднозначным выбором урока.")
    if errors:
        return _result("blocked", revision, " ".join(errors[:10]))
    if questions:
        return _result("needs_clarification", revision, "Нужны уточнения до подготовки правки.", questions=questions)
    touched = {lesson_id for ids in chosen.values() for lesson_id in ids}
    if len(touched) + sum(op.kind == "add" for op in plan.operations) > MAX_AFFECTED:
        return _result("blocked", revision, "Превышен лимит 100 затронутых уроков.")
    original_by_id = {row["id"]: row for row in public}
    linked_ids = {row["id"] for row in original if _linked(row)}
    if touched & linked_ids:
        return _result("blocked", revision, "Выбранное занятие связано с группами или общим событием решателя. Нужен отдельный пересчёт solver; текстовая правка не разрывает эти связи.")
    working = deepcopy(original_by_id)
    additions = set()
    bell = {}
    for row in public:
        bell.setdefault((row["day"], row["slot"]), set()).add((row["start"], row["end"]))
    seed = json.dumps({"revision": revision, "plan": plan.model_dump(exclude_none=True)}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    for number, operation in enumerate(plan.operations):
        path = f"operations.{number}"
        ids = chosen.get(path + ".selector", [])
        if any(lesson_id not in working for lesson_id in ids):
            return _result("blocked", revision, "План повторно изменяет уже удалённый урок. Уточните набор операций.")
        if operation.kind == "remove":
            for lesson_id in ids:
                del working[lesson_id]
        elif operation.kind == "swap":
            other_id = chosen[path + ".target"][0]
            if other_id not in working or ids[0] == other_id:
                return _result("blocked", revision, "Обмен требует два разных существующих урока.")
            first, second = working[ids[0]], working[other_id]
            left = {field: first[field] for field in ("day", "slot", "start", "end")}
            right = {field: second[field] for field in ("day", "slot", "start", "end")}
            first.update(right)
            second.update(left)
        elif operation.kind == "update":
            fields = operation.changes.model_dump(exclude_none=True)
            for field in ("className", "teachers", "rooms"):
                if field in fields:
                    fields[field] = ([resources[field][canonical(value)] for value in fields[field]] if isinstance(fields[field], list)
                                     else resources[field][canonical(fields[field])])
            for lesson_id in ids:
                row = working[lesson_id]
                after = {**_editable(row), **deepcopy(fields)}
                if (after["day"], after["slot"]) != (row["day"], row["slot"]) and "start" not in fields:
                    intervals = bell.get((after["day"], after["slot"]), set())
                    if len(intervals) != 1:
                        questions.append(_question(path + ".changes.start", "Для нового дня и номера нет однозначной сетки звонков. Укажите начало и окончание урока."))
                        continue
                    after["start"], after["end"] = next(iter(intervals))
                try:
                    valid = EditableLessonInput.model_validate(after).model_dump()
                except (ValueError, ValidationError):
                    return _result("blocked", revision, "Итоговый урок не проходит проверку полей и интервала времени.")
                row.update(valid)
        else:
            lesson_id = "tt_text_" + hashlib.sha256((seed + "|" + str(number)).encode()).hexdigest()[:24]
            if lesson_id in working:
                return _result("blocked", revision, "Идентификатор добавляемого урока уже существует; подготовьте новый план.")
            after = operation.lesson.model_dump()
            for field in ("className", "teachers", "rooms"):
                after[field] = ([resources[field][canonical(value)] for value in after[field]] if isinstance(after[field], list)
                                else resources[field][canonical(after[field])])
            working[lesson_id] = {"id": lesson_id, **after}
            additions.add(lesson_id)
    if questions:
        return _result("needs_clarification", revision, "Нужно явно задать время нового слота.", questions=questions)
    touched |= additions
    if len(working) > MAX_LESSONS:
        return _result("blocked", revision, "Итоговый черновик превышает лимит уроков.")
    changes = []
    for lesson_id in sorted(touched):
        before, after = original_by_id.get(lesson_id), working.get(lesson_id)
        if before is not None and after is not None and _editable(before) == _editable(after):
            continue
        changes.append({"lessonId": lesson_id, "before": deepcopy(before),
                        "after": {"id": lesson_id, **_editable(after)} if after is not None else None})
    if not changes:
        return _result("blocked", revision, "План не изменяет черновик.")
    changed_ids = {change["lessonId"] for change in changes}
    issues = _conflicts(list(working.values()), changed_ids)
    if issues:
        return _result("blocked", revision, "Итоговая правка содержит пересечения класса, преподавателя или кабинета.", issues=issues)
    warnings = [issue for issue in compute_issues(list(working.values()))
                if issue["severity"] == "warning" and changed_ids.intersection(issue["lessonIds"])]
    counts = {"add": sum(change["before"] is None for change in changes),
              "remove": sum(change["after"] is None for change in changes)}
    counts["update"] = len(changes) - counts["add"] - counts["remove"]
    proposal = {"kind": "text", "expectedRevision": revision, "changes": changes,
                "summary": f"Добавлено: {counts['add']}; отменено: {counts['remove']}; изменено: {counts['update']}.",
                "uncheckedChecks": list(UNCHECKED)}
    from .timetable import text_patch_hash
    proposal["hash"] = text_patch_hash(proposal)
    return _result("awaiting_approval", revision, "Подготовлен полный diff. Для записи требуется отдельное подтверждение.", proposal=proposal, issues=warnings)
