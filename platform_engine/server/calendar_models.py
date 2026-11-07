"""Explicit school calendar inputs. No inferred period, timezone or holidays."""
from datetime import date, time, timedelta
import re
from typing import Literal
import unicodedata
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_DAYS = 366
MAX_OCCURRENCES = 366
MAX_REVISION = 2**31 - 1


def iso_date(value: str) -> date:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("Требуется дата YYYY-MM-DD")
    return date.fromisoformat(value)


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    @field_validator("*", mode="after")
    @classmethod
    def text(cls, value):
        if isinstance(value, str) and any(unicodedata.category(c) == 'Cs' or (unicodedata.category(c) == 'Cc' and c not in '\n\r\t') for c in value):
            raise ValueError("Некорректный текст")
        return value


class Evidence(StrictInput):
    source: str = Field(min_length=1, max_length=500)
    confirmed: bool = False
    note: str = Field(default='', max_length=2000)


class Bell(StrictInput):
    day: int = Field(ge=0, le=5)
    slot: int = Field(ge=1, le=20)
    start: str = Field(pattern=r'^[0-9]{2}:[0-9]{2}$')
    end: str = Field(pattern=r'^[0-9]{2}:[0-9]{2}$')

    @model_validator(mode='after')
    def interval(self):
        time.fromisoformat(self.start)
        time.fromisoformat(self.end)
        if self.start >= self.end:
            raise ValueError('Окончание звонка должно быть позже начала')
        return self


class CalendarConfig(Evidence):
    timezone: str = Field(min_length=1, max_length=100)
    periodStart: str
    periodEnd: str
    requiresTravel: bool = False
    # Empty retains explicit planning-slot times for existing installations.
    # A provided schedule must cover every slot used by the normative plan.
    bells: list[Bell] = Field(default_factory=list, max_length=100)

    @field_validator('timezone')
    @classmethod
    def zone(cls, value):
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError('Укажите существующий часовой пояс IANA') from None
        return value

    @model_validator(mode='after')
    def period(self):
        start, end = iso_date(self.periodStart), iso_date(self.periodEnd)
        if not 0 <= (end - start).days < MAX_DAYS:
            raise ValueError('Период календаря ограничен 366 днями')
        keys = [(bell.day, bell.slot) for bell in self.bells]
        if len(keys) != len(set(keys)):
            raise ValueError('Номер звонка внутри дня должен быть уникальным')
        for day in range(6):
            ordered = sorted((bell.start, bell.end) for bell in self.bells if bell.day == day)
            if any(first[1] > second[0] for first, second in zip(ordered, ordered[1:])):
                raise ValueError('Звонки одного дня не должны пересекаться')
        return self


class Week(Evidence):
    weekStart: str
    schoolDays: list[int] = Field(max_length=7)
    type: str = Field(default='', max_length=80)

    @model_validator(mode='after')
    def week(self):
        start = iso_date(self.weekStart)
        if start.weekday() != 0:
            raise ValueError('Неделя начинается в понедельник')
        start + timedelta(days=6)
        if any(type(day) is not int or not 0 <= day <= 6 for day in self.schoolDays) or len(set(self.schoolDays)) != len(self.schoolDays):
            raise ValueError('Учебные дни должны быть уникальными числами 0..6')
        self.schoolDays = sorted(self.schoolDays)
        return self


class Day(Evidence):
    date: str
    status: Literal['school', 'closed']

    @field_validator('date')
    @classmethod
    def day(cls, value):
        iso_date(value)
        return value


class ExceptionInput(Evidence):
    startDate: str
    endDate: str
    kind: Literal['school', 'closed', 'teacher_unavailable', 'room_unavailable']
    targetId: str = Field(default='', max_length=160)
    startTime: str | None = None
    endTime: str | None = None

    @model_validator(mode='after')
    def bounds(self):
        start, end = iso_date(self.startDate), iso_date(self.endDate)
        if not 0 <= (end - start).days < MAX_DAYS:
            raise ValueError('Исключение ограничено 366 днями')
        resource = self.kind in {'teacher_unavailable', 'room_unavailable'}
        if bool(self.targetId) != resource:
            raise ValueError('Недоступность требует точный идентификатор ресурса; учебный день не требует ресурса')
        if (self.startTime is None) != (self.endTime is None):
            raise ValueError('Укажите начало и конец интервала вместе')
        if self.startTime is not None:
            if not resource:
                raise ValueError('Учебный или неучебный день задаётся целиком')
            for value in (self.startTime, self.endTime):
                if not re.fullmatch(r'[0-9]{2}:[0-9]{2}', value):
                    raise ValueError('Требуется время HH:MM')
                time.fromisoformat(value)
            if self.startTime >= self.endTime:
                raise ValueError('Окончание должно быть позже начала')
        return self


class Recurrence(ExceptionInput):
    weekdays: list[int] = Field(min_length=1, max_length=7)

    @field_validator('weekdays')
    @classmethod
    def weekdays_valid(cls, value):
        if any(type(day) is not int or not 0 <= day <= 6 for day in value) or len(set(value)) != len(value):
            raise ValueError('Дни повторения должны быть уникальными числами 0..6')
        return sorted(value)


class Revision(StrictInput):
    expectedRevision: int = Field(ge=0, le=MAX_REVISION)
    previewHash: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')


class RecordWrite(Revision):
    record: dict


class ConfigWrite(StrictInput):
    expectedRevision: int = Field(ge=0, le=MAX_REVISION)
    config: CalendarConfig


class PreviewInput(StrictInput):
    expectedRevision: int = Field(ge=0, le=MAX_REVISION)
    record: Recurrence
    recordId: str | None = Field(default=None, min_length=1, max_length=80)


class ResolveDateInput(StrictInput):
    expression: str = Field(min_length=1, max_length=30)
    messageTimestamp: str = Field(min_length=1, max_length=80)


MODELS = {'weeks': Week, 'days': Day, 'exceptions': ExceptionInput, 'rules': Recurrence}
