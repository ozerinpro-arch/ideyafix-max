"""Bounded XLSX timetable import and a separate, revisioned local draft.

No workbook code, formulas, links or model calls are executed. Public domain
services are shared with the local schedule agent; HTTP handlers only enforce
session access and decode the request. Imported data never enters demo_state.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import csv
import hmac
from io import BytesIO, StringIO
import json
import posixpath
import re
import sqlite3
import stat
from typing import Annotated
import unicodedata
from urllib.parse import unquote, urlsplit
from uuid import uuid4
import xml.etree.ElementTree as ET
import zipfile
from xml.sax.saxutils import escape
import zlib

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .auth import require_roles
from .db import database_path

MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 12 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 200
MAX_ROWS = 1000
MAX_COLUMNS = 64
MAX_CELL_TEXT = 2000
MAX_SHARED_STRINGS = 20000
MAX_LESSONS = 2000
MAX_XML_NODES = 150000
MAX_XML_DEPTH = 64
HISTORY_LIMIT = 10
RECEIPT_LIMIT = 128
IMPORT_LIMIT = 128
MAX_REVISION = 2**31 - 1

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
DOC_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS = {"m": MAIN, "r": REL}
SLOT_PATTERN = re.compile(r"(\d{1,2})\.\s*(\d{1,2}):(\d{2})\s*[-–—]\s*(\d{1,2}):(\d{2})")
COORD_PATTERN = re.compile(r"([A-Z]{1,3})([1-9][0-9]{0,6})")
PUBLIC_LESSON_KEYS = ("id", "className", "day", "slot", "start", "end", "subject", "teachers", "rooms", "sourceCell", "raw", "edited")
DAY_NAMES = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота")


def canonical(value: str) -> str:
    """Exact Unicode/case/whitespace equivalence; never infer identities."""
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _single_line(value: str) -> str:
    if any(unicodedata.category(char) in {"Cc", "Cs"} for char in value):
        raise ValueError("Текст должен быть одной строкой без управляющих символов")
    return value


class EditableLessonInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)
    className: str = Field(min_length=1, max_length=80)
    day: int = Field(ge=0, le=5)
    slot: int = Field(ge=1, le=20)
    start: str = Field(pattern=r"^[0-9]{2}:[0-9]{2}$")
    end: str = Field(pattern=r"^[0-9]{2}:[0-9]{2}$")
    subject: str = Field(min_length=1, max_length=160)
    teachers: list[Annotated[str, Field(min_length=1, max_length=120)]] = Field(max_length=8)
    rooms: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(max_length=8)

    @field_validator("className", "subject")
    @classmethod
    def single_line(cls, value):
        return _single_line(value)

    @field_validator("teachers", "rooms")
    @classmethod
    def unique_names(cls, values):
        for value in values:
            _single_line(value)
        if len({canonical(value) for value in values}) != len(values):
            raise ValueError("Повторяющиеся имена или кабинеты требуют уточнения")
        return values

    @model_validator(mode="after")
    def valid_interval(self):
        if _minutes(self.start) >= _minutes(self.end):
            raise ValueError("Окончание урока должно быть позже начала")
        return self


class LessonEditInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expectedRevision: int = Field(ge=0, le=MAX_REVISION)
    lesson: EditableLessonInput


def _minutes(value: str) -> int:
    try:
        hour, minute = (int(part) for part in value.split(":"))
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError
        return hour * 60 + minute
    except (ValueError, TypeError):
        raise ValueError("Некорректное время урока") from None


def _invalid(message: str):
    raise HTTPException(422, message)


class _BoundedTreeBuilder(ET.TreeBuilder):
    def __init__(self):
        super().__init__()
        self.nodes = 0
        self.depth = 0

    def start(self, tag, attrs):
        self.nodes += 1
        self.depth += 1
        if self.nodes > MAX_XML_NODES or self.depth > MAX_XML_DEPTH:
            _invalid("Слишком сложная XML-структура XLSX")
        return super().start(tag, attrs)

    def end(self, tag):
        element = super().end(tag)
        self.depth -= 1
        return element


def _xml(payload: bytes) -> ET.Element:
    try:
        text = payload.decode("utf-8-sig")
        if "\x00" in text or re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.IGNORECASE):
            _invalid("DTD, сущности и нестандартная кодировка XML запрещены")
        return ET.fromstring(text, parser=ET.XMLParser(target=_BoundedTreeBuilder()))
    except (ET.ParseError, UnicodeError, ValueError):
        _invalid("Повреждённый или неподдерживаемый XML в XLSX")


def _relationship_target(base: str, target: str) -> str:
    target = unquote(target)
    try:
        parsed = urlsplit(target)
    except ValueError:
        _invalid("Повреждённая связь XLSX")
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or "\\" in target or any(unicodedata.category(char) in {"Cc", "Cs"} for char in target):
        _invalid("Внешние или неоднозначные связи XLSX запрещены")
    resolved = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join(posixpath.dirname(base), target))
    if resolved in {"", ".", ".."} or resolved.startswith("../"):
        _invalid("Недопустимая связь внутри XLSX")
    return resolved


def _archive(payload: bytes) -> dict[str, ET.Element]:
    if not payload or len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(413 if payload else 422, "Пустой файл или файл больше 2 МиБ")
    try:
        with zipfile.ZipFile(BytesIO(payload)) as archive:
            members = archive.infolist()
            if len(members) > MAX_ARCHIVE_MEMBERS or sum(item.file_size for item in members) > MAX_UNCOMPRESSED_BYTES:
                _invalid("Архив XLSX превышает допустимый размер или число файлов")
            names = set()
            roots = {}
            actual_size = 0
            xml_nodes = 0
            for member in members:
                name = member.filename
                parts = name.rstrip("/").split("/")
                if (name.startswith("/") or "\\" in name or "\x00" in name or any(part in {"", ".", ".."} for part in parts)
                        or name in names or member.flag_bits & 1
                        or stat.S_ISLNK(member.external_attr >> 16)
                        or member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}):
                    _invalid("Недопустимая структура ZIP в XLSX")
                names.add(name)
                lower_name = name.casefold()
                if "vbaproject" in lower_name or "externallink" in lower_name or lower_name.endswith((".bin", ".vba")):
                    _invalid("Макросы и внешние связи XLSX запрещены")
                if member.is_dir():
                    continue
                # read() is bounded even for a dishonest central directory.
                with archive.open(member) as stream:
                    content = stream.read(min(member.file_size + 1, MAX_UNCOMPRESSED_BYTES + 1))
                actual_size += len(content)
                if len(content) != member.file_size or actual_size > MAX_UNCOMPRESSED_BYTES:
                    _invalid("Некорректный распакованный размер XLSX")
                if lower_name.endswith((".xml", ".rels")):
                    root = _xml(content)
                    if any(True for _ in root.iter(f"{{{MAIN}}}f")):
                        _invalid("Формулы в книге XLSX запрещены, включая другие листы")
                    xml_nodes += sum(1 for _ in root.iter())
                    if xml_nodes > MAX_XML_NODES:
                        _invalid("Превышен общий лимит XML-структуры XLSX")
                    roots[name] = root
                    if lower_name.endswith(".rels"):
                        if root.tag != f"{{{REL}}}Relationships":
                            _invalid("Некорректные связи XLSX")
                        ids = set()
                        for item in root:
                            relation_id = item.get("Id", "")
                            target = item.get("Target", "")
                            if (item.tag != f"{{{REL}}}Relationship" or not relation_id or relation_id in ids or not target
                                    or item.get("TargetMode", "Internal").casefold() != "internal"):
                                _invalid("Внешние или повреждённые связи XLSX запрещены")
                            ids.add(relation_id)
                            _relationship_target("xl/workbook.xml", target)
                    if name == "[Content_Types].xml":
                        for item in root:
                            content_type = item.get("ContentType", "").casefold()
                            if any(word in content_type for word in ("macroenabled", "vbaproject", "activex")):
                                _invalid("Макросы и активное содержимое XLSX запрещены")
            return roots
    except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, OSError, EOFError, NotImplementedError, zlib.error):
        _invalid("Файл не является исправной книгой XLSX")


def _cell_text(cell: ET.Element, shared: list[str]) -> str:
    if cell.find("m:f", NS) is not None:
        _invalid("Формулы на листе расписания запрещены")
    kind = cell.get("t", "n")
    if kind == "inlineStr":
        value = "".join(item.text or "" for item in cell.findall("m:is//m:t", NS))
    else:
        values = cell.findall("m:v", NS)
        if len(values) > 1:
            _invalid("Неоднозначное значение ячейки XLSX")
        value = values[0].text or "" if values else ""
        if kind == "s":
            if not re.fullmatch(r"[0-9]+", value) or len(value) > 8 or int(value) >= len(shared):
                _invalid("Некорректная ссылка на строку XLSX")
            value = shared[int(value)]
        elif kind not in {"n", "str", "b", "d"}:
            _invalid("Ошибка или неподдерживаемый тип ячейки XLSX")
    if len(value) > MAX_CELL_TEXT or any(unicodedata.category(char) == "Cs" for char in value):
        _invalid("Слишком длинный или некорректный текст ячейки XLSX")
    return value


def _worksheet_rows(root: ET.Element, shared: list[str]) -> list[tuple[int, dict[int, tuple[str, str]]]]:
    if root.tag != f"{{{MAIN}}}worksheet":
        _invalid("Неподдерживаемый формат листа XLSX")
    sheet_data = root.findall("m:sheetData", NS)
    if len(sheet_data) != 1:
        _invalid("На листе XLSX отсутствуют однозначные данные")
    rows = []
    seen_rows, seen_cells = set(), set()
    for row in sheet_data[0]:
        row_number = row.get("r", "")
        if row.tag != f"{{{MAIN}}}row" or not re.fullmatch(r"[1-9][0-9]{0,6}", row_number):
            _invalid("Некорректная строка листа XLSX")
        number = int(row_number)
        if number > MAX_ROWS or number in seen_rows:
            _invalid("Повторяющаяся строка или превышен лимит строк XLSX")
        seen_rows.add(number)
        values = {}
        for cell in row:
            coordinate = cell.get("r", "")
            match = COORD_PATTERN.fullmatch(coordinate)
            if cell.tag != f"{{{MAIN}}}c" or not match or int(match[2]) != number or coordinate in seen_cells:
                _invalid("Некорректная или повторяющаяся координата ячейки XLSX")
            seen_cells.add(coordinate)
            column = 0
            for char in match[1]:
                column = column * 26 + ord(char) - 64
            if column > MAX_COLUMNS:
                _invalid("Превышен лимит столбцов XLSX")
            values[column] = (coordinate, _cell_text(cell, shared))
        rows.append((number, values))
    return sorted(rows)


def _parse_slot(value: str) -> tuple[int, str, str]:
    match = SLOT_PATTERN.fullmatch(value.strip())
    if not match:
        _invalid("В столбце «Уроки» требуется номер и интервал, например 1. 8:00 - 8:40")
    slot, start_hour, start_minute, end_hour, end_minute = (int(part) for part in match.groups())
    start, end = f"{start_hour:02d}:{start_minute:02d}", f"{end_hour:02d}:{end_minute:02d}"
    try:
        if not 1 <= slot <= 20 or _minutes(start) >= _minutes(end):
            raise ValueError
    except ValueError:
        _invalid("Неверный номер или интервал урока")
    return slot, start, end


def _parse_lesson(raw: str) -> tuple[str, list[str], list[str], list[str]]:
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if not lines:
        _invalid("Пустая ячейка урока")
    subject = lines[0]
    rooms, warnings = [], []
    room_match = re.fullmatch(r"(.+?)\s*\(([^()]*)\)\s*", subject)
    if room_match and canonical(room_match[2]) == "extra":
        warnings.append("Пометка «extra» в названии предмета: кабинет не указан")
    elif room_match:
        subject = room_match[1].strip()
        rooms = [part.strip() for part in room_match[2].split(",") if part.strip()]
        if any(not part.strip() for part in room_match[2].split(",")):
            warnings.append("Неполный список кабинетов в исходной ячейке")
    elif "(" in subject or ")" in subject:
        warnings.append("Скобки в предмете требуют ручной проверки кабинета")
    teachers = [part.strip() for line in lines[1:] for part in line.split(",") if part.strip()]
    if len(lines) > 2:
        warnings.append("Несколько строк преподавателей требуют проверки структуры урока")
    if any(not part.strip() for line in lines[1:] for part in line.split(",")):
        warnings.append("Неполный список преподавателей в исходной ячейке")
    if (not subject or len(subject) > 160 or len(rooms) > 8 or len(teachers) > 8
            or any(len(value) > 80 for value in rooms) or any(len(value) > 120 for value in teachers)):
        _invalid("Слишком длинный предмет или список преподавателей/кабинетов")
    try:
        for value in (subject, *teachers, *rooms):
            _single_line(value)
    except ValueError:
        _invalid("Управляющие символы в описании урока")
    return subject, teachers, rooms, warnings


def _source_name(filename: str) -> str:
    name = filename.strip()
    if not name or len(name) > 180 or "/" in name or "\\" in name or any(unicodedata.category(char) in {"Cc", "Cs"} for char in name):
        _invalid("Некорректное имя файла")
    if not name.casefold().endswith(".xlsx"):
        raise HTTPException(415, "Поддерживается только XLSX без макросов")
    return name


def parse_xlsx(payload: bytes, filename: str) -> dict:
    """Pure preview service. No database access and no source file writes."""
    source_name = _source_name(filename)
    roots = _archive(payload)
    workbook = roots.get("xl/workbook.xml")
    relationships = roots.get("xl/_rels/workbook.xml.rels")
    if workbook is None or workbook.tag != f"{{{MAIN}}}workbook" or relationships is None or "[Content_Types].xml" not in roots:
        _invalid("Отсутствует структура книги XLSX")
    sheets = workbook.findall("m:sheets/m:sheet", NS)
    if not sheets or len(sheets) > 32:
        _invalid("Некорректное число листов XLSX")
    names, sheet_ids = set(), set()
    for sheet in sheets:
        name, sheet_id = sheet.get("name", ""), sheet.get(f"{{{DOC_REL}}}id", "")
        if not name or len(name) > 100 or canonical(name) in names or not sheet_id or sheet_id in sheet_ids:
            _invalid("Повторяющиеся или некорректные листы XLSX")
        names.add(canonical(name))
        sheet_ids.add(sheet_id)
    selected = [sheet for sheet in sheets if canonical(sheet.get("name", "")) == canonical("Расписание")]
    if not selected:
        if len(sheets) != 1:
            _invalid("Выберите книгу с листом «Расписание» или единственным совместимым листом")
        selected = sheets
    links = {item.get("Id"): item for item in relationships}
    link = links.get(selected[0].get(f"{{{DOC_REL}}}id"))
    if link is None or link.get("Type") != f"{DOC_REL}/worksheet":
        _invalid("Неверная связь листа расписания")
    sheet_path = _relationship_target("xl/workbook.xml", link.get("Target", ""))
    root = roots.get(sheet_path)
    if root is None:
        _invalid("Лист расписания отсутствует в архиве")
    shared = []
    string_links = [item for item in relationships if item.get("Type") == f"{DOC_REL}/sharedStrings"]
    if len(string_links) > 1:
        _invalid("Неоднозначный словарь строк XLSX")
    if string_links:
        string_root = roots.get(_relationship_target("xl/workbook.xml", string_links[0].get("Target", "")))
        if string_root is None or string_root.tag != f"{{{MAIN}}}sst":
            _invalid("Отсутствует словарь строк XLSX")
        for item in string_root:
            if item.tag != f"{{{MAIN}}}si" or len(shared) >= MAX_SHARED_STRINGS:
                _invalid("Превышен лимит или повреждён словарь строк XLSX")
            text = "".join(node.text or "" for node in item.findall(".//m:t", NS))
            if len(text) > MAX_CELL_TEXT:
                _invalid("Слишком длинная общая строка XLSX")
            shared.append(text)
    rows = _worksheet_rows(root, shared)
    classes, lessons = None, []
    day = None
    days = {canonical(name): index for index, name in enumerate(DAY_NAMES)}
    days.update({name: index for index, name in enumerate(("пн", "вт", "ср", "чт", "пт", "сб"))})
    for number, cells in rows:
        nonempty = {column: value for column, (_, value) in cells.items() if value.strip()}
        if not nonempty:
            continue
        a, b = nonempty.get(1, "").strip(), nonempty.get(2, "").strip()
        if canonical(a) == "дни" or canonical(b) == "уроки":
            if canonical(a) != "дни" or canonical(b) != "уроки" or max(nonempty) < 3:
                _invalid("Повреждена строка заголовков расписания")
            header = [nonempty.get(column, "").strip() for column in range(3, max(nonempty) + 1)]
            if any(not value or len(value) > 80 for value in header) or len({canonical(value) for value in header}) != len(header):
                _invalid("Пустые, повторяющиеся или слишком длинные классы в заголовке")
            try:
                for value in header:
                    _single_line(value)
            except ValueError:
                _invalid("Некорректное имя класса")
            if classes is not None and header != classes:
                _invalid("Повторный заголовок должен содержать те же классы в том же порядке")
            classes = header
            continue
        if classes is None:
            if number > 10 or any(column != 1 for column in nonempty):
                _invalid("Неподдерживаемая структура: ожидаются «Дни», «Уроки» и классы начиная с C")
            continue
        if max(nonempty) > len(classes) + 2:
            _invalid("Данные выходят за столбцы классов")
        if a:
            if canonical(a) not in days:
                _invalid("Неизвестный день недели в расписании")
            day = days[canonical(a)]
        if not b:
            if any(column >= 3 for column in nonempty):
                _invalid("Урок указан без номера и времени")
            continue
        slot, start, end = _parse_slot(b)
        if day is None:
            _invalid("До первого урока требуется день недели")
        for column, class_name in enumerate(classes, 3):
            coordinate, raw = cells.get(column, ("", ""))
            if not raw.strip():
                continue
            subject, teachers, rooms, warnings = _parse_lesson(raw)
            if len(lessons) >= MAX_LESSONS:
                _invalid("Превышен лимит уроков")
            lessons.append({"id": f"tt_{uuid4().hex[:12]}", "className": class_name, "day": day, "slot": slot,
                            "start": start, "end": end, "subject": subject, "teachers": teachers, "rooms": rooms,
                            "sourceCell": f"{selected[0].get('name')}!{coordinate}", "raw": raw, "edited": False,
                            "_parseWarnings": warnings})
    if classes is None or not lessons:
        _invalid("Совместимое расписание с уроками не найдено")
    return {"revision": 0, "sourceName": source_name, "lessons": lessons, "issues": compute_issues(lessons), "updatedAt": None}


def _same_planning_event(first: dict, second: dict) -> bool:
    return bool(first.get("_planningEventId") and first.get("_planningEventId") == second.get("_planningEventId")
                and canonical(first["className"]) != canonical(second["className"])
                and _event_signature(first) == _event_signature(second))


def _event_signature(lesson):
    return (lesson["day"], lesson["slot"], lesson["start"], lesson["end"], canonical(lesson["subject"]),
            tuple(sorted(canonical(value) for value in lesson["teachers"])),
            tuple(sorted(canonical(value) for value in lesson["rooms"])))


def _class_participants_overlap(first: dict, second: dict, class_name: str) -> bool:
    one = [g for g in first.get("_planningGroups", []) if canonical(g["className"]) == canonical(class_name)]
    two = [g for g in second.get("_planningGroups", []) if canonical(g["className"]) == canonical(class_name)]
    if not one or not two:
        return True
    for a in one:
        for b in two:
            disjoint = (a["kind"] == b["kind"] == "subgroup" and a["partition"]
                        and a["partition"] == b["partition"] and a["id"] != b["id"])
            if not disjoint:
                return True
    return False


def compute_issues(lessons: list[dict]) -> list[dict]:
    """Report overlapping interval components, without assigning subgroups."""
    issues = []

    def add(kind, severity, message, ids):
        ids = sorted(set(ids))
        digest = hashlib.sha256((kind + "|" + message + "|" + "|".join(ids)).encode()).hexdigest()[:16]
        issues.append({"id": f"i_{digest}", "kind": kind, "severity": severity, "message": message, "lessonIds": ids})

    groups = defaultdict(list)
    for lesson in lessons:
        lesson_id = lesson["id"]
        if not lesson["rooms"]:
            add("missing_room", "warning", "Не указан кабинет", [lesson_id])
        warnings = list(lesson.get("_parseWarnings", []))
        if not lesson["teachers"]:
            warnings.append("Не указан преподаватель")
        if len(lesson["teachers"]) > 1 or len(lesson["rooms"]) > 1:
            warnings.append("Несколько преподавателей или кабинетов: связь с подгруппами не установлена")
        if any(len({canonical(value) for value in lesson[field]}) != len(lesson[field]) for field in ("teachers", "rooms")):
            warnings.append("Повторяющиеся имена или кабинеты требуют уточнения")
        if warnings:
            add("ambiguous", "warning", "; ".join(warnings), [lesson_id])
        for kind, values in (("class", [lesson["className"]]), ("teacher", lesson["teachers"]), ("room", lesson["rooms"])):
            for value in {canonical(value): value for value in values}.values():
                groups[(kind, lesson["day"], canonical(value))].append((lesson, value))
    labels = {"class": "Класс", "teacher": "Преподаватель", "room": "Кабинет"}
    for (kind, day, _), items in sorted(groups.items()):
        items.sort(key=lambda item: (item[0]["start"], item[0]["end"], item[0]["id"]))
        if not any(lesson.get("_planningEventId") for lesson, _ in items):
            # Imported grids have no subgroup proof. Preserve the linear scan
            # instead of comparing every pair of untrusted source rows.
            cluster, cluster_end = [], -1
            def emit_source_cluster():
                if len(cluster) > 1:
                    add(kind, "error" if kind == "class" else "warning",
                        f"{labels[kind]} «{items[0][1]}»: пересечение времени ({DAY_NAMES[day]})" + ("; проверьте совместные занятия и подгруппы" if kind != "class" else ""),
                        [lesson["id"] for lesson in cluster])
            for lesson, _label in items:
                start, end = _minutes(lesson["start"]), _minutes(lesson["end"])
                if cluster and start >= cluster_end:
                    emit_source_cluster()
                    cluster = []
                cluster.append(lesson)
                cluster_end = max(cluster_end if len(cluster) > 1 else -1, end)
            emit_source_cluster()
            continue
        # Connected components of true interval conflicts. Explicit solver group
        # provenance is allowed only through the validated internal batch path.
        parent = list(range(len(items)))
        def find(index):
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index
        for right, (lesson, _) in enumerate(items):
            for left in range(right - 1, -1, -1):
                other = items[left][0]
                if _minutes(other["end"]) <= _minutes(lesson["start"]):
                    continue
                if _same_planning_event(lesson, other):
                    continue
                if kind == "class" and not _class_participants_overlap(lesson, other, items[right][1]):
                    continue
                parent[find(right)] = find(left)
        components = defaultdict(list)
        for index, (lesson, _) in enumerate(items):
            components[find(index)].append(lesson)
        for component in components.values():
            if len(component) > 1:
                add(kind, "error" if kind == "class" else "warning",
                    f"{labels[kind]} «{items[0][1]}»: пересечение времени ({DAY_NAMES[day]})" + ("; проверьте совместные занятия и подгруппы" if kind != "class" else ""),
                    [lesson["id"] for lesson in component])
    return issues


def _empty() -> dict:
    return {"revision": 0, "sourceName": "", "lessons": [], "issues": [], "updatedAt": None}


def _public(draft: dict) -> dict:
    return {"revision": draft["revision"], "sourceName": draft["sourceName"],
            "lessons": [{**{key: deepcopy(lesson[key]) for key in PUBLIC_LESSON_KEYS},
                         **({"planningEventId": lesson["_planningEventId"], "planningGroups": deepcopy(lesson["_planningGroups"])}
                            if lesson.get("_planningEventId") else {})} for lesson in draft["lessons"]],
            "issues": deepcopy(draft["issues"]), "updatedAt": draft["updatedAt"]}


def _connect() -> sqlite3.Connection:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=15, isolation_level=None)
    connection.execute("PRAGMA busy_timeout=15000")
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS timetable_draft (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS timetable_history (
          id INTEGER PRIMARY KEY AUTOINCREMENT, revision INTEGER NOT NULL, payload TEXT NOT NULL,
          actor_id TEXT NOT NULL, actor_role TEXT NOT NULL, action TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS timetable_sources (hash TEXT PRIMARY KEY, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS timetable_command_receipts (
          id INTEGER PRIMARY KEY AUTOINCREMENT, command_key TEXT NOT NULL UNIQUE, command_hash TEXT NOT NULL,
          actor_id TEXT NOT NULL, actor_role TEXT NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL);
    """)
    return connection


def _load(connection: sqlite3.Connection) -> dict:
    row = connection.execute("SELECT payload FROM timetable_draft WHERE id=1").fetchone()
    return json.loads(row[0]) if row else _empty()


def read_draft(connection=None) -> dict:
    """Read the public draft only. The caller is responsible for authorization."""
    if connection is not None:
        return _public(_load(connection))
    with closing(_connect()) as connection:
        return _public(_load(connection))


def _actor(profile: dict) -> tuple[str, str]:
    if not isinstance(profile, dict) or profile.get("role") not in {"admin", "director"}:
        raise HTTPException(403, "Для этой роли действие недоступно")
    actor_id = profile.get("userId")
    if not isinstance(actor_id, str) or not 1 <= len(actor_id) <= 80:
        raise HTTPException(403, "Требуется действующий профиль пользователя")
    return actor_id, profile["role"]


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _cas(draft: dict, expected_revision: int):
    if type(expected_revision) is not int or not 0 <= expected_revision <= MAX_REVISION:
        _invalid("Некорректная ожидаемая ревизия")
    if draft["revision"] != expected_revision:
        raise HTTPException(409, "Расписание изменено: обновите черновик и проверьте правку")
    if draft["revision"] >= MAX_REVISION:
        raise HTTPException(409, "Достигнут лимит ревизий черновика")


def _save(connection, previous: dict, draft: dict, actor: tuple[str, str], action: str):
    if previous["revision"]:
        connection.execute("INSERT INTO timetable_history(revision,payload,actor_id,actor_role,action,created_at) VALUES(?,?,?,?,?,?)",
                           (previous["revision"], json.dumps(previous, ensure_ascii=False), *actor, action, draft["updatedAt"]))
        connection.execute("DELETE FROM timetable_history WHERE id NOT IN (SELECT id FROM timetable_history ORDER BY id DESC LIMIT ?)", (HISTORY_LIMIT,))
    connection.execute("INSERT INTO timetable_draft(id,payload) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                       (json.dumps(draft, ensure_ascii=False),))


def import_draft(payload: bytes, filename: str, expected_revision: int, profile: dict) -> dict:
    actor = _actor(profile)
    draft = parse_xlsx(payload, filename)
    source_hash = hashlib.sha256(payload).hexdigest()
    with closing(_connect()) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            previous = _load(connection)
            _cas(previous, expected_revision)
            if connection.execute("SELECT 1 FROM timetable_sources WHERE hash=?", (source_hash,)).fetchone():
                raise HTTPException(409, "Этот файл уже импортирован; повторный импорт не заменяет ручные правки")
            if connection.execute("SELECT COUNT(*) FROM timetable_sources").fetchone()[0] >= IMPORT_LIMIT:
                raise HTTPException(409, "Достигнут лимит импортов локального черновика")
            draft.update(revision=previous["revision"] + 1, updatedAt=_utc(), _sourceHash=source_hash)
            _save(connection, previous, draft, actor, "import")
            connection.execute("INSERT INTO timetable_sources(hash,created_at) VALUES(?,?)", (source_hash, draft["updatedAt"]))
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    return _public(draft)


def _edit_body(body: LessonEditInput | dict) -> LessonEditInput:
    try:
        return body if isinstance(body, LessonEditInput) else LessonEditInput.model_validate(body)
    except ValidationError:
        _invalid("Некорректные поля правки урока")


def mutation_hash(lesson_id: str, body: LessonEditInput | dict) -> str:
    if not isinstance(lesson_id, str) or not 1 <= len(lesson_id) <= 80:
        _invalid("Некорректный идентификатор урока")
    body = _edit_body(body)
    serialized = json.dumps({"lessonId": lesson_id, **body.model_dump()}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def update_lesson(lesson_id: str, body: LessonEditInput | dict, profile: dict, *, idempotency_key: str | None = None, expected_hash: str | None = None, guard=None) -> dict:
    """CAS update; optional agent receipt atomically records the exact result.

    Receipt keys retain the last 128 commands. Replaying a retained key requires
    the same actor, role and normalized payload hash and returns the original
    result even after subsequent edits. Evicted keys still fail stale CAS.
    """
    actor = _actor(profile)
    body = _edit_body(body)
    command_hash = mutation_hash(lesson_id, body)
    if expected_hash is not None and (not isinstance(expected_hash, str) or not hmac.compare_digest(command_hash, expected_hash)):
        raise HTTPException(409, "Содержание правки отличается от подтверждённого предложения")
    if idempotency_key is not None:
        if (not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128
                or expected_hash is None or any(unicodedata.category(char) in {"Cc", "Cs"} for char in idempotency_key)):
            _invalid("Некорректная квитанция применения правки")
    with closing(_connect()) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            if idempotency_key is not None:
                receipt = connection.execute("SELECT command_hash,actor_id,actor_role,result FROM timetable_command_receipts WHERE command_key=?", (idempotency_key,)).fetchone()
                if receipt:
                    if receipt[:3] != (command_hash, *actor):
                        raise HTTPException(409, "Квитанция уже использована для другого действия или пользователя")
                    connection.commit()
                    return json.loads(receipt[3])
            if guard is not None:
                guard(connection)
            else:
                assert_manual_draft_context(connection)
            previous = _load(connection)
            _cas(previous, body.expectedRevision)
            draft = deepcopy(previous)
            lesson = next((item for item in draft["lessons"] if item["id"] == lesson_id), None)
            if lesson is None:
                raise HTTPException(404, "Урок черновика не найден")
            stamp = _utc()
            lesson.update(body.lesson.model_dump())
            lesson.update(edited=True, _editedAt=stamp, _parseWarnings=[])
            # A free manual edit no longer proves the solver's group assignment.
            lesson.pop("_planningEventId", None)
            lesson.pop("_planningGroups", None)
            draft.update(revision=previous["revision"] + 1, updatedAt=stamp, issues=compute_issues(draft["lessons"]))
            _save(connection, previous, draft, actor, "edit")
            result = _public(draft)
            if idempotency_key is not None:
                connection.execute("INSERT INTO timetable_command_receipts(command_key,command_hash,actor_id,actor_role,result,created_at) VALUES(?,?,?,?,?,?)",
                                   (idempotency_key, command_hash, *actor, json.dumps(result, ensure_ascii=False), stamp))
                connection.execute("DELETE FROM timetable_command_receipts WHERE id NOT IN (SELECT id FROM timetable_command_receipts ORDER BY id DESC LIMIT ?)", (RECEIPT_LIMIT,))
            connection.commit()
            return result
        except BaseException:
            connection.rollback()
            raise


router = APIRouter(prefix="/api/timetable", tags=["timetable draft"])
_authorized = require_roles("admin", "director")


async def _upload(request: Request) -> bytes:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().casefold() != "application/octet-stream":
        raise HTTPException(415, "Загрузите XLSX как application/octet-stream")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "Файл превышает 2 МиБ")
        chunks.append(chunk)
    return b"".join(chunks)


@router.get("")
def get_timetable(profile: dict = Depends(_authorized)):
    return read_draft()


@router.post("/preview")
async def preview_timetable(request: Request, filename: str = Query(min_length=1, max_length=180), profile: dict = Depends(_authorized)):
    return _public(parse_xlsx(await _upload(request), filename))


@router.post("/import")
async def import_timetable(request: Request, filename: str = Query(min_length=1, max_length=180), expectedRevision: int = Query(ge=0, le=MAX_REVISION), profile: dict = Depends(_authorized)):
    return import_draft(await _upload(request), filename, expectedRevision, profile)


@router.post("/lessons/{lesson_id}")
def edit_timetable_lesson(lesson_id: str, body: LessonEditInput, profile: dict = Depends(_authorized)):
    return update_lesson(lesson_id, body, profile)


def _batch_payload(lessons, expected_revision, source_name):
    if type(expected_revision) is not int or not 0 <= expected_revision <= MAX_REVISION:
        _invalid("Некорректная ожидаемая версия")
    if not isinstance(source_name, str) or not 1 <= len(source_name) <= 180:
        _invalid("Некорректный источник расчёта")
    try:
        _single_line(source_name)
    except ValueError:
        _invalid("Некорректный источник расчёта")
    if not isinstance(lessons, list) or not 1 <= len(lessons) <= MAX_LESSONS:
        _invalid("Расчёт должен содержать от 1 до 2000 занятий")
    normalized, ids, event_signatures, event_classes = [], set(), {}, set()
    allowed = set(EditableLessonInput.model_fields) | {"id", "sourceCell", "raw", "edited", "_planningEventId", "_planningGroups"}
    for item in lessons:
        if not isinstance(item, dict) or set(item) - allowed:
            _invalid("Неизвестные поля занятия расчёта")
        lesson_id = item.get("id")
        if not isinstance(lesson_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", lesson_id) or lesson_id in ids:
            _invalid("Некорректный или повторный идентификатор занятия")
        ids.add(lesson_id)
        try:
            fields = EditableLessonInput.model_validate({key: item[key] for key in EditableLessonInput.model_fields}).model_dump()
        except (ValidationError, KeyError):
            _invalid("Некорректные поля занятия расчёта")
        entry = {"id": lesson_id, **fields}
        for key in ("raw", "sourceCell"):
            value = item.get(key, "")
            if not isinstance(value, str) or len(value) > MAX_CELL_TEXT or any(unicodedata.category(c) == "Cs" for c in value):
                _invalid("Некорректный источник занятия")
            entry[key] = value
        event_id = item.get("_planningEventId")
        groups = item.get("_planningGroups")
        if (event_id is None) != (groups is None):
            _invalid("Неполные сведения о группах расчёта")
        if event_id is not None:
            if not isinstance(event_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", event_id) or not isinstance(groups, list) or not 1 <= len(groups) <= 200:
                _invalid("Некорректные сведения о событии расчёта")
            clean, group_ids = [], set()
            for group in groups:
                if not isinstance(group, dict) or not {"id", "className", "kind", "partition"} <= set(group) or set(group) - {"id", "className", "kind", "partition", "name", "size"}:
                    _invalid("Некорректная группа расчёта")
                if not isinstance(group["id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", group["id"]) or group["id"] in group_ids:
                    _invalid("Повторная или некорректная группа расчёта")
                group_ids.add(group["id"])
                if group["kind"] not in {"whole", "subgroup"}:
                    _invalid("Неизвестный тип группы расчёта")
                for key in ("className", "partition"):
                    if not isinstance(group[key], str) or len(group[key]) > 80 or (key == "className" and not group[key].strip()):
                        _invalid("Некорректные поля группы расчёта")
                    try:
                        _single_line(group[key])
                    except ValueError:
                        _invalid("Некорректные поля группы расчёта")
                clean.append({key: group[key] for key in ("id", "className", "kind", "partition")})
            if not any(canonical(g["className"]) == canonical(fields["className"]) for g in clean):
                _invalid("Занятие не относится к указанной группе")
            entry.update(_planningEventId=event_id, _planningGroups=clean)
            projection = event_id, canonical(fields["className"])
            signature = _event_signature(entry)
            if projection in event_classes or (event_id in event_signatures and event_signatures[event_id] != signature):
                _invalid("Повторная или несовместимая проекция общего занятия")
            event_classes.add(projection)
            event_signatures[event_id] = signature
        normalized.append(entry)
    return {"lessons": normalized, "expectedRevision": expected_revision, "sourceName": source_name}


def batch_hash(lessons, expected_revision, source_name="Автоматический расчёт") -> str:
    content = _batch_payload(lessons, expected_revision, source_name)
    return hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def apply_batch(lessons, expected_revision, profile, *, idempotency_key, expected_hash, source_name="Автоматический расчёт", guard=None) -> dict:
    """Internal validated solver boundary; not exposed as a writable HTTP API.

    guard participates in this SAME transaction, so config/run metadata and
    draft/receipt transition atomically. A retained receipt replays before guard.
    """
    actor = _actor(profile)
    payload = _batch_payload(lessons, expected_revision, source_name)
    digest = batch_hash(lessons, expected_revision, source_name)
    if not isinstance(expected_hash, str) or not hmac.compare_digest(digest, expected_hash):
        raise HTTPException(409, "Расчёт отличается от подтверждённого предложения")
    if not isinstance(idempotency_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", idempotency_key):
        _invalid("Некорректный идентификатор команды расчёта")
    with closing(_connect()) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            receipt = connection.execute("SELECT command_hash,actor_id,actor_role,result FROM timetable_command_receipts WHERE command_key=?", (idempotency_key,)).fetchone()
            if receipt:
                if receipt[:3] != (digest, *actor):
                    raise HTTPException(409, "Команда уже использована для другого расчёта или пользователя")
                connection.commit()
                return json.loads(receipt[3])
            if guard is not None:
                guard(connection)
            previous = _load(connection)
            _cas(previous, expected_revision)
            existing = {item["id"]: item for item in previous["lessons"]}
            stamp = _utc()
            materialized = []
            for item in payload["lessons"]:
                old = existing.get(item["id"])
                entry = {**item, "edited": True, "_editedAt": stamp, "_parseWarnings": []}
                if old is not None:
                    entry.update(raw=old["raw"], sourceCell=old["sourceCell"])
                elif not entry["sourceCell"]:
                    entry["sourceCell"] = "План!" + item["id"]
                materialized.append(entry)
            draft = {"revision": previous["revision"] + 1, "sourceName": source_name,
                     "lessons": materialized, "updatedAt": stamp, "issues": compute_issues(materialized)}
            if any(issue["severity"] == "error" for issue in draft["issues"]):
                raise HTTPException(422, "Расчёт содержит конфликт участников класса")
            _save(connection, previous, draft, actor, "planning")
            result = _public(draft)
            connection.execute("INSERT INTO timetable_command_receipts(command_key,command_hash,actor_id,actor_role,result,created_at) VALUES(?,?,?,?,?,?)",
                               (idempotency_key, digest, *actor, json.dumps(result, ensure_ascii=False), stamp))
            connection.execute("DELETE FROM timetable_command_receipts WHERE id NOT IN (SELECT id FROM timetable_command_receipts ORDER BY id DESC LIMIT ?)", (RECEIPT_LIMIT,))
            connection.commit()
            return result
        except BaseException:
            connection.rollback()
            raise


def _text_patch_payload(proposal: dict) -> dict:
    """Validate the internal text proposal, without granting model write access."""
    required = {"kind", "expectedRevision", "changes", "summary"}
    allowed = required | {"hash", "uncheckedChecks"}
    if not isinstance(proposal, dict) or not required <= proposal.keys() or proposal.keys() - allowed:
        _invalid("Некорректное предложение текстовой правки")
    if proposal["kind"] != "text" or type(proposal["expectedRevision"]) is not int or not 0 <= proposal["expectedRevision"] <= MAX_REVISION:
        _invalid("Некорректная версия текстовой правки")
    summary = proposal["summary"]
    checks = proposal.get("uncheckedChecks", [])
    if not isinstance(summary, str) or not 1 <= len(summary) <= 2000:
        _invalid("Текстовая правка требует ограниченное описание")
    if not isinstance(checks, list) or len(checks) > 16 or any(not isinstance(item, str) or not 1 <= len(item) <= 300 for item in checks):
        _invalid("Некорректный список непроверенных ограничений")
    if any(unicodedata.category(char) == "Cs" or (unicodedata.category(char) == "Cc" and char not in "\n\r\t")
           for value in [summary, *checks] for char in value):
        _invalid("Некорректный текст предложения")
    changes = proposal["changes"]
    if not isinstance(changes, list) or not 1 <= len(changes) <= 100:
        _invalid("За одно согласование допустимо от 1 до 100 изменений")
    normalized, seen = [], set()
    for change in changes:
        if not isinstance(change, dict) or set(change) != {"lessonId", "before", "after"}:
            _invalid("Изменение должно содержать полный before и after")
        lesson_id = change["lessonId"]
        if not isinstance(lesson_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", lesson_id) or lesson_id in seen:
            _invalid("Повторный или некорректный идентификатор текстовой правки")
        seen.add(lesson_id)
        before, after = change["before"], change["after"]
        if before is None and after is None:
            _invalid("Пустая текстовая правка")
        if before is not None:
            if not isinstance(before, dict) or set(before) != set(PUBLIC_LESSON_KEYS) or before.get("id") != lesson_id:
                _invalid("Нужен полный исходный урок без скрытых метаданных")
            if type(before.get("edited")) is not bool or any(not isinstance(before.get(key), str) for key in ("raw", "sourceCell")):
                _invalid("Некорректное происхождение исходного урока")
            if any(unicodedata.category(char) == "Cs" for key in ("raw", "sourceCell") for char in before[key]):
                _invalid("Некорректный Unicode исходного урока")
            try:
                EditableLessonInput.model_validate({key: before[key] for key in EditableLessonInput.model_fields})
            except ValidationError:
                _invalid("Некорректный исходный урок")
        if after is not None:
            if not isinstance(after, dict) or set(after) != set(EditableLessonInput.model_fields) | {"id"} or after.get("id") != lesson_id:
                _invalid("Нужны только редактируемые поля конечного урока")
            try:
                fields = EditableLessonInput.model_validate({key: value for key, value in after.items() if key != "id"}).model_dump()
            except ValidationError:
                _invalid("Некорректный конечный урок")
            after = {"id": lesson_id, **fields}
            if before is not None and all(before[key] == value for key, value in after.items()):
                _invalid("Предложение не меняет урок")
        normalized.append({"lessonId": lesson_id, "before": deepcopy(before), "after": after})
    return {"kind": "text", "expectedRevision": proposal["expectedRevision"], "changes": normalized,
            "summary": summary, "uncheckedChecks": checks}


def text_patch_hash(proposal: dict) -> str:
    payload = _text_patch_payload(proposal)
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_text_patch_context(proposal: dict, connection=None):
    """Do not bypass a confirmed normative plan with a free grid mutation.

    Imported rows lack the solver's event/group proof. Until a complete mapping
    exists they may be edited only as an explicitly unverified local draft.
    Confirmed plans must use the existing validated planning/substitution path.
    """
    _text_patch_payload(proposal)
    if connection is None:
        with closing(_connect()) as owned:
            return validate_text_patch_context(proposal, owned)
    assert_manual_draft_context(connection)


def assert_manual_draft_context(connection=None):
    """Confirmed plans are changed by a validated candidate, never a free card.

    Legacy unconfigured manual drafts retain their explicit warning semantics;
    trusted substitution guards keep their existing constrained route.
    """
    if connection is None:
        with closing(_connect()) as owned:
            return assert_manual_draft_context(owned)
    table = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='planning_config'").fetchone()
    if table:
        row = connection.execute("SELECT input_json FROM planning_config WHERE id=1").fetchone()
        if row:
            try:
                confirmed = json.loads(row[0]).get("confirmed") is True
            except (ValueError, TypeError, AttributeError):
                raise HTTPException(409, "Настройки планирования требуют проверки") from None
            if confirmed:
                raise HTTPException(422, "Подтверждён нормативный план. Переставьте занятия в проверенном кандидате расчёта или используйте подбор замены: отдельная свободная правка не доказывает соблюдение всех ограничений.")


def apply_text_patch(proposal: dict, profile: dict, *, idempotency_key: str, guard=None) -> dict:
    """Apply a confirmed partial diff in one transaction, preserving other rows.

    Only the agent's saved approval workflow calls this function. Receipt replay
    precedes guards/CAS to recover a committed mutation after checkpoint failure.
    """
    actor = _actor(profile)
    payload = _text_patch_payload(proposal)
    digest = text_patch_hash(proposal)
    if not isinstance(proposal.get("hash"), str) or not re.fullmatch(r"[a-f0-9]{64}", proposal["hash"]) or not hmac.compare_digest(digest, proposal["hash"]):
        raise HTTPException(409, "Текстовая правка отличается от согласованного предпросмотра")
    if not isinstance(idempotency_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", idempotency_key):
        _invalid("Некорректная квитанция текстовой правки")
    with closing(_connect()) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            receipt = connection.execute("SELECT command_hash,actor_id,actor_role,result FROM timetable_command_receipts WHERE command_key=?", (idempotency_key,)).fetchone()
            if receipt:
                if receipt[:3] != (digest, *actor):
                    raise HTTPException(409, "Квитанция принадлежит другой правке или пользователю")
                connection.commit()
                return json.loads(receipt[3])
            if guard is not None:
                guard(connection)
            validate_text_patch_context(proposal, connection)
            previous = _load(connection)
            _cas(previous, payload["expectedRevision"])
            public = {item["id"]: item for item in _public(previous)["lessons"]}
            existing = {item["id"]: item for item in previous["lessons"]}
            changed, stamp = {}, _utc()
            for change in payload["changes"]:
                lesson_id, before, after = change["lessonId"], change["before"], change["after"]
                old = existing.get(lesson_id)
                if before != public.get(lesson_id) or (before is None and old is not None):
                    raise HTTPException(409, "Исходный урок отличается от показанного в предложении")
                if old and (old.get("_planningEventId") or old.get("_planningGroups")):
                    raise HTTPException(422, "Связанное занятие требует отдельного пересчёта расписания")
                if after is None:
                    changed[lesson_id] = None
                else:
                    entry = deepcopy(old) if old else {"raw": "", "sourceCell": "Агент!" + lesson_id}
                    entry.update(deepcopy(after))
                    entry.update(edited=True, _editedAt=stamp, _parseWarnings=[])
                    changed[lesson_id] = entry
            lessons = [changed.get(item["id"], deepcopy(item)) for item in previous["lessons"] if changed.get(item["id"], True) is not None]
            lessons.extend(item for lesson_id, item in changed.items() if lesson_id not in existing and item is not None)
            if len(lessons) > MAX_LESSONS:
                raise HTTPException(422, "Превышен лимит занятий черновика")
            issues = compute_issues(lessons)
            if any(issue["kind"] in {"class", "teacher", "room"} and set(issue["lessonIds"]) & changed.keys() for issue in issues):
                raise HTTPException(422, "Текстовая правка создаёт или сохраняет пересечение затронутого класса, учителя или кабинета")
            draft = deepcopy(previous)
            draft.update(revision=previous["revision"] + 1, lessons=lessons, issues=issues, updatedAt=stamp)
            _save(connection, previous, draft, actor, "text_edit")
            result = _public(draft)
            connection.execute("INSERT INTO timetable_command_receipts(command_key,command_hash,actor_id,actor_role,result,created_at) VALUES(?,?,?,?,?,?)",
                               (idempotency_key, digest, *actor, json.dumps(result, ensure_ascii=False), stamp))
            connection.execute("DELETE FROM timetable_command_receipts WHERE id NOT IN (SELECT id FROM timetable_command_receipts ORDER BY id DESC LIMIT ?)", (RECEIPT_LIMIT,))
            connection.commit()
            return result
        except BaseException:
            connection.rollback()
            raise


def list_history(profile: dict) -> list[dict]:
    _actor(profile)
    with closing(_connect()) as connection:
        current = _load(connection)
        rows = connection.execute("SELECT revision,payload,action,created_at FROM timetable_history ORDER BY id DESC LIMIT ?", (HISTORY_LIMIT - 1,)).fetchall()
    result = []
    if current["revision"]:
        result.append({"revision": current["revision"], "action": "current", "createdAt": current["updatedAt"], "lessonCount": len(current["lessons"])})
    for revision, payload, action, stamp in rows:
        snapshot = json.loads(payload)
        result.append({"revision": revision, "action": "before_" + action, "createdAt": snapshot["updatedAt"] or stamp, "lessonCount": len(snapshot["lessons"])})
    return result


def _historical(connection, revision: int) -> dict:
    if type(revision) is not int or not 1 <= revision <= MAX_REVISION:
        _invalid("Некорректная версия истории")
    current = _load(connection)
    if current["revision"] == revision and revision:
        return current
    row = connection.execute("SELECT payload FROM timetable_history WHERE revision=? ORDER BY id DESC LIMIT 1", (revision,)).fetchone()
    if not row:
        raise HTTPException(404, "Версия не найдена в локальной истории")
    return json.loads(row[0])


def restore_revision(expected_revision: int, target_revision: int, profile: dict) -> dict:
    actor = _actor(profile)
    with closing(_connect()) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            previous = _load(connection)
            _cas(previous, expected_revision)
            if target_revision == previous["revision"]:
                raise HTTPException(409, "Эта версия уже является текущей")
            draft = deepcopy(_historical(connection, target_revision))
            draft.update(revision=previous["revision"] + 1, updatedAt=_utc(), issues=compute_issues(draft["lessons"]))
            _save(connection, previous, draft, actor, "restore")
            connection.commit()
            return _public(draft)
        except BaseException:
            connection.rollback()
            raise


class RestoreInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expectedRevision: int = Field(ge=0, le=MAX_REVISION)
    targetRevision: int = Field(ge=1, le=MAX_REVISION)


class ExportInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expectedRevision: int = Field(ge=0, le=MAX_REVISION)
    format: str = Field(pattern=r"^(xlsx|csv)$")


def _export_rows(draft):
    rows = [["Класс", "День", "Урок", "Начало", "Окончание", "Предмет", "Учителя", "Кабинеты", "Источник", "Идентификатор"]]
    for lesson in sorted(draft["lessons"], key=lambda l: (l["day"], l["start"], l["className"], l["id"])):
        rows.append([lesson["className"], DAY_NAMES[lesson["day"]], str(lesson["slot"]), lesson["start"], lesson["end"], lesson["subject"], ", ".join(lesson["teachers"]), ", ".join(lesson["rooms"]), lesson["sourceCell"], lesson["id"]])
    return rows


def _spreadsheet_text(value: str) -> str:
    # Prevent CSV/Excel formula injection, including leading whitespace/control.
    if value.lstrip(" \t\r\n\ufeff").startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def export_draft(expected_revision: int, format: str, profile: dict) -> tuple[bytes, str]:
    _actor(profile)
    draft = read_draft()
    if type(expected_revision) is not int or draft["revision"] != expected_revision:
        raise HTTPException(409, "Версия изменилась: обновите расписание перед выгрузкой")
    if not draft["lessons"]:
        raise HTTPException(422, "Нет занятий для выгрузки")
    rows = [[_spreadsheet_text(value) for value in row] for row in _export_rows(draft)]
    if format == "csv":
        output = StringIO(newline="")
        writer = csv.writer(output, delimiter=";", lineterminator="\r\n")
        writer.writerows(rows)
        return output.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8"
    if format != "xlsx":
        _invalid("Неизвестный формат выгрузки")
    def column(index):
        name = ""
        while index:
            index, rem = divmod(index - 1, 26)
            name = chr(65 + rem) + name
        return name
    xml_rows = []
    for index, row in enumerate(rows, 1):
        cells = "".join(f'<c r="{column(col)}{index}" t="inlineStr"><is><t xml:space="preserve">{escape(text)}</t></is></c>' for col, text in enumerate(row, 1))
        xml_rows.append(f'<row r="{index}">{cells}</row>')
    parts = {
        "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
        "_rels/.rels": f'<Relationships xmlns="{REL}"><Relationship Id="r1" Type="{DOC_REL}/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml": f'<workbook xmlns="{MAIN}" xmlns:r="{DOC_REL}"><sheets><sheet name="Занятия" sheetId="1" r:id="r1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": f'<Relationships xmlns="{REL}"><Relationship Id="r1" Type="{DOC_REL}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{MAIN}"><sheetData>{"".join(xml_rows)}</sheetData></worksheet>',
    }
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, xml in parts.items():
            archive.writestr(name, '<?xml version="1.0" encoding="UTF-8"?>' + xml)
    return output.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.get("/history")
def history_timetable(profile: dict = Depends(_authorized)):
    return list_history(profile)


@router.get("/history/{revision}")
def historical_timetable(revision: int, profile: dict = Depends(_authorized)):
    if not 1 <= revision <= MAX_REVISION:
        _invalid("Некорректная версия")
    with closing(_connect()) as connection:
        return _public(_historical(connection, revision))


@router.post("/restore")
def restore_timetable(body: RestoreInput, profile: dict = Depends(_authorized)):
    return restore_revision(body.expectedRevision, body.targetRevision, profile)


@router.post("/export")
def download_timetable(body: ExportInput, profile: dict = Depends(_authorized)):
    content, media_type = export_draft(body.expectedRevision, body.format, profile)
    return Response(content, media_type=media_type, headers={"Content-Disposition": f'attachment; filename="timetable-r{body.expectedRevision}.{body.format}"'})


def apply_approved_request_patch(connection, proposal: dict, profile: dict, *, idempotency_key: str, guard) -> dict:
    """Exact request approval inside the caller's existing domain transaction.

    The caller atomically stores its durable request state and receipt on this
    connection. No nested connection/commit, and none of the text-edit guards
    (confirmed normative input, linked events, before/CAS, conflicts) are waived.
    Current membership is required even for receipt replay.
    """
    if not connection.in_transaction or not callable(guard):
        raise ValueError('An active domain transaction and authorization guard are required')
    guard(connection)
    actor = _actor(profile)
    payload = _text_patch_payload(proposal)
    digest = text_patch_hash(proposal)
    if not isinstance(proposal.get('hash'), str) or not hmac.compare_digest(digest, proposal['hash']):
        raise HTTPException(409, 'Предложение отличается от точного согласованного изменения')
    if not isinstance(idempotency_key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', idempotency_key):
        _invalid('Некорректная квитанция заявки')
    # Explicit mutation only; transactional DDL, no executescript implicit COMMIT.
    connection.execute('CREATE TABLE IF NOT EXISTS timetable_draft (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')
    connection.execute('CREATE TABLE IF NOT EXISTS timetable_history (id INTEGER PRIMARY KEY AUTOINCREMENT, revision INTEGER NOT NULL, payload TEXT NOT NULL, actor_id TEXT NOT NULL, actor_role TEXT NOT NULL, action TEXT NOT NULL, created_at TEXT NOT NULL)')
    connection.execute('CREATE TABLE IF NOT EXISTS timetable_command_receipts (id INTEGER PRIMARY KEY AUTOINCREMENT, command_key TEXT NOT NULL UNIQUE, command_hash TEXT NOT NULL, actor_id TEXT NOT NULL, actor_role TEXT NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL)')
    receipt = connection.execute('SELECT command_hash,actor_id,actor_role,result FROM timetable_command_receipts WHERE command_key=?', (idempotency_key,)).fetchone()
    if receipt:
        if receipt[:3] != (digest, *actor): raise HTTPException(409, 'Квитанция принадлежит другой правке или пользователю')
        return json.loads(receipt[3])
    validate_text_patch_context(proposal, connection)
    previous = _load(connection); _cas(previous, payload['expectedRevision'])
    public = {r['id']: r for r in _public(previous)['lessons']}
    existing = {r['id']: r for r in previous['lessons']}; changed = {}; stamp = _utc()
    for change in payload['changes']:
        ident, before, after = change['lessonId'], change['before'], change['after']; old = existing.get(ident)
        if before != public.get(ident) or (before is None and old is not None): raise HTTPException(409, 'Исходный урок изменился после предложения')
        if old and (old.get('_planningEventId') or old.get('_planningGroups')): raise HTTPException(422, 'Связанное занятие требует проверенного пересчёта')
        if after is None: raise HTTPException(422, 'Заявка не удаляет уроки')
        entry = deepcopy(old) if old else {'raw': '', 'sourceCell': 'Заявка!' + ident}
        entry.update(deepcopy(after)); entry.update(edited=True, _editedAt=stamp, _parseWarnings=[])
        changed[ident] = entry
    lessons = [changed.get(r['id'], deepcopy(r)) for r in previous['lessons']]
    lessons.extend(r for ident, r in changed.items() if ident not in existing)
    if len(lessons) > MAX_LESSONS: raise HTTPException(422, 'Превышен лимит занятий черновика')
    issues = compute_issues(lessons)
    if any(i['kind'] in {'class', 'teacher', 'room', 'ambiguous'} and set(i['lessonIds']) & changed.keys() for i in issues):
        raise HTTPException(422, 'Есть пересечение или неподтверждённая связь; требуется уточнение')
    draft = deepcopy(previous); draft.update(revision=previous['revision'] + 1, lessons=lessons, issues=issues, updatedAt=stamp)
    _save(connection, previous, draft, actor, 'school_request_approval')
    result = _public(draft)
    connection.execute('INSERT INTO timetable_command_receipts(command_key,command_hash,actor_id,actor_role,result,created_at) VALUES(?,?,?,?,?,?)', (idempotency_key, digest, *actor, json.dumps(result, ensure_ascii=False), stamp))
    connection.execute('DELETE FROM timetable_command_receipts WHERE id NOT IN (SELECT id FROM timetable_command_receipts ORDER BY id DESC LIMIT ?)', (RECEIPT_LIMIT,))
    return result
