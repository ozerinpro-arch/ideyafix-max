"""Bounded extraction DSL. These models grant no authority to mutate a draft."""
from __future__ import annotations

import unicodedata
from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .timetable import EditableLessonInput, canonical

LessonId = Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")]
ClassName = Annotated[str, Field(min_length=1, max_length=80)]
Subject = Annotated[str, Field(min_length=1, max_length=160)]
Teacher = Annotated[str, Field(min_length=1, max_length=120)]
Room = Annotated[str, Field(min_length=1, max_length=80)]
Clock = Annotated[str, Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")]


def _safe(value: str, *, multiline: bool = False) -> str:
    if any(unicodedata.category(char) == "Cs" or
           (unicodedata.category(char) == "Cc" and not (multiline and char in "\n\r\t"))
           for char in value):
        raise ValueError("Текст содержит недопустимые управляющие символы")
    return value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, revalidate_instances="always")


class LessonSelector(_Strict):
    lessonId: LessonId | None = None
    className: ClassName | None = None
    day: Annotated[int, Field(ge=0, le=5)] | None = None
    slot: Annotated[int, Field(ge=1, le=20)] | None = None
    subject: Subject | None = None
    teacher: Teacher | None = None
    room: Room | None = None
    match: Literal["one", "all"] = "one"

    @field_validator("className", "subject", "teacher", "room")
    @classmethod
    def names(cls, value):
        return _safe(value) if value is not None else value

    @model_validator(mode="after")
    def has_filter(self):
        if not any(getattr(self, name) is not None for name in
                   ("lessonId", "className", "day", "slot", "subject", "teacher", "room")):
            raise ValueError("Для выбора урока нужен хотя бы один точный признак")
        return self


class PartialEditableLesson(_Strict):
    className: ClassName | None = None
    day: Annotated[int, Field(ge=0, le=5)] | None = None
    slot: Annotated[int, Field(ge=1, le=20)] | None = None
    start: Clock | None = None
    end: Clock | None = None
    subject: Subject | None = None
    teachers: Annotated[list[Teacher], Field(max_length=8)] | None = None
    rooms: Annotated[list[Room], Field(max_length=8)] | None = None

    @field_validator("className", "subject")
    @classmethod
    def names(cls, value):
        return _safe(value) if value is not None else value

    @field_validator("teachers", "rooms")
    @classmethod
    def unique_names(cls, values):
        if values is not None:
            for value in values:
                _safe(value)
            if len({canonical(value) for value in values}) != len(values):
                raise ValueError("Повторяющиеся ресурсы требуют уточнения")
        return values

    @model_validator(mode="after")
    def nonempty(self):
        if not self.model_fields_set or any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("Изменения должны содержать непустой набор полей без null")
        if ("start" in self.model_fields_set) != ("end" in self.model_fields_set):
            raise ValueError("Явное время требует start и end вместе")
        if self.start is not None and self.start >= self.end:
            raise ValueError("Окончание урока должно быть позже начала")
        return self


class TextEditOperation(_Strict):
    kind: Literal["update", "swap", "add", "remove"]
    selector: LessonSelector | None = None
    target: LessonSelector | None = None
    changes: PartialEditableLesson | None = None
    lesson: EditableLessonInput | None = None
    evidence: str = Field(min_length=1, max_length=8000)

    @field_validator("evidence")
    @classmethod
    def safe_evidence(cls, value):
        return _safe(value, multiline=True)

    @model_validator(mode="after")
    def operation_shape(self):
        actual = {field for field in ("selector", "target", "changes", "lesson")
                  if getattr(self, field) is not None}
        expected = {"update": {"selector", "changes"}, "remove": {"selector"},
                    "swap": {"selector", "target"}, "add": {"lesson"}}[self.kind]
        if actual != expected:
            raise ValueError("Набор полей не соответствует операции")
        if self.kind == "swap" and (self.selector.match != "one" or self.target.match != "one"):
            raise ValueError("Обмен требует два однозначно выбранных урока")
        return self


class TextEditPlan(_Strict):
    operations: list[TextEditOperation] = Field(min_length=1, max_length=10)
    scope: Literal["current_week"]
    explanation: str = Field(min_length=1, max_length=2000)

    @field_validator("explanation")
    @classmethod
    def safe_explanation(cls, value):
        return _safe(value, multiline=True)


class ExperimentalLessonSelector(LessonSelector):
    """Select a physical event in one explicitly chosen experimental version."""
    eventId: LessonId | None = None
    date: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")] | None = None

    @field_validator('date')
    @classmethod
    def valid_date(cls, value):
        if value is not None:
            date.fromisoformat(value)
        return value

    @model_validator(mode='after')
    def has_filter(self):
        if not any(getattr(self, name) is not None for name in
                   ('eventId', 'lessonId', 'className', 'day', 'slot', 'subject', 'teacher', 'room', 'date')):
            raise ValueError('Для выбора занятия нужен хотя бы один точный признак')
        return self


class ExperimentalLessonChanges(_Strict):
    date: Annotated[str, Field(pattern=r'^\d{4}-\d{2}-\d{2}$')] | None = None
    day: Annotated[int, Field(ge=0, le=5)] | None = None
    slot: Annotated[int, Field(ge=1, le=20)] | None = None
    start: Clock | None = None
    end: Clock | None = None
    teachers: Annotated[list[Teacher], Field(min_length=1, max_length=8)] | None = None
    rooms: Annotated[list[Room], Field(min_length=1, max_length=1)] | None = None

    @field_validator('date')
    @classmethod
    def valid_date(cls, value):
        if value is not None:
            date.fromisoformat(value)
        return value

    @field_validator('teachers', 'rooms')
    @classmethod
    def unique_names(cls, values):
        if values is not None:
            for value in values:
                _safe(value)
            if len({canonical(value) for value in values}) != len(values):
                raise ValueError('Повторяющиеся ресурсы требуют уточнения')
        return values

    @model_validator(mode='after')
    def nonempty(self):
        if not self.model_fields_set or any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError('Нужен непустой набор изменений без null')
        if ('start' in self.model_fields_set) != ('end' in self.model_fields_set):
            raise ValueError('Время требует start и end вместе')
        if self.start is not None and self.start >= self.end:
            raise ValueError('Окончание занятия должно быть позже начала')
        return self


class ExperimentalTextOperation(_Strict):
    kind: Literal['update', 'swap', 'pin']
    selector: ExperimentalLessonSelector
    target: ExperimentalLessonSelector | None = None
    changes: ExperimentalLessonChanges | None = None
    pinned: bool | None = None
    evidence: str = Field(min_length=1, max_length=8000)

    @field_validator('evidence')
    @classmethod
    def safe_evidence(cls, value):
        return _safe(value, multiline=True)

    @model_validator(mode='after')
    def operation_shape(self):
        actual = {field for field in ('target', 'changes', 'pinned') if getattr(self, field) is not None}
        expected = {'update': {'changes'}, 'swap': {'target'}, 'pin': {'pinned'}}[self.kind]
        if actual != expected:
            raise ValueError('Набор полей не соответствует экспериментальной операции')
        if self.selector.match != 'one' or (self.target and self.target.match != 'one'):
            raise ValueError('Правка требует однозначного выбора физических занятий')
        return self


class ExperimentalTextEditPlan(_Strict):
    operations: list[ExperimentalTextOperation] = Field(min_length=1, max_length=10)
    scope: Literal['target_experiment']
    explanation: str = Field(min_length=1, max_length=2000)

    @field_validator('explanation')
    @classmethod
    def safe_explanation(cls, value):
        return _safe(value, multiline=True)


class ModelPartialEditableLesson(PartialEditableLesson):
    """Transport-only nulls mean omitted optional changes, never clearing fields."""

    @model_validator(mode='before')
    @classmethod
    def omit_unset_nulls(cls, value):
        if isinstance(value, dict):
            return {key: item for key, item in value.items()
                    if item is not None or key not in cls.model_fields}
        return value


class ModelTextEditOperation(TextEditOperation):
    changes: ModelPartialEditableLesson | None = None


class ModelTextEditPlan(TextEditPlan):
    operations: list[ModelTextEditOperation] = Field(min_length=1, max_length=10)


class ModelExperimentalLessonChanges(ExperimentalLessonChanges):
    @model_validator(mode='before')
    @classmethod
    def omit_unset_nulls(cls, value):
        if isinstance(value, dict):
            return {key: item for key, item in value.items()
                    if item is not None or key not in cls.model_fields}
        return value


class ModelExperimentalTextOperation(ExperimentalTextOperation):
    changes: ModelExperimentalLessonChanges | None = None


class ModelExperimentalTextEditPlan(ExperimentalTextEditPlan):
    operations: list[ModelExperimentalTextOperation] = Field(min_length=1, max_length=10)
