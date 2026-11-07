"""Director-confirmed school journal rows tied to dated published lessons."""

from contextlib import closing
from datetime import date, timedelta
import json
import re
from typing import Literal

from fastapi import HTTPException
from pydantic import Field, field_validator, model_validator

from . import school_ops as ops, school_publications as publications
from .scheduling_models import canonical
from .student_learning import configured_service
from .student_schedule import StudentScheduleService


KIND = 'lessonProgress'
KEY = '_schoolLessonProgress'
LIMIT = 100_000
NOTICE = ('Строки подтверждённого школьного журнала. Укажите ID ученика, дату, ID урока, '
          'версию публикации и код источника. Заполните явку, оценку или оба поля.')


class LessonProgressInput(ops.StrictInput):
    studentId: str = Field(min_length=1, max_length=80)
    publicationVersion: int = Field(ge=1, le=ops.MAX_REVISION)
    lessonId: str = Field(min_length=1, max_length=160)
    lessonDate: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    attendance: Literal['present', 'absent', 'excused'] | None = None
    grade: float | None = Field(default=None, ge=0, le=5)
    sourceRef: str = Field(min_length=1, max_length=120)

    @field_validator('lessonDate')
    @classmethod
    def valid_date(cls, value):
        date.fromisoformat(value)
        return value

    @field_validator('studentId', 'lessonId', 'sourceRef')
    @classmethod
    def single_line(cls, value):
        if re.search(r'[\r\n\t]', value):
            raise ValueError('Укажите значение в одну строку')
        return value

    @model_validator(mode='after')
    def has_observation(self):
        if self.attendance is None and self.grade is None:
            raise ValueError('Укажите явку или оценку')
        return self


def ledger(state, school_id):
    value = state.get(KEY)
    if value is None:
        return {'schoolId': school_id, 'revision': 0, 'records': []}
    if (not isinstance(value, dict) or value.get('schoolId') != school_id
            or type(value.get('revision')) is not int or not 0 <= value['revision'] <= ops.MAX_REVISION
            or not isinstance(value.get('records'), list) or len(value['records']) > LIMIT):
        raise HTTPException(503, 'Хранилище школьного журнала требует проверки')
    return value


def occurrence(record):
    return record['studentId'], record['lessonDate'], record['lessonId']


def index(records):
    try:
        result = {occurrence(record): record for record in records}
        if len(result) != len(records):
            raise ValueError()
        return result
    except (KeyError, TypeError, ValueError):
        raise HTTPException(503, 'Записи школьного журнала требуют проверки') from None


class PublishedLessons:
    def __init__(self, connection, state, school_id, resources=None):
        self.connection = connection
        self.school_id = school_id
        self.students = {row['id']: {'studentId': row['id'], 'name': row['name'], 'className': row['className']}
                         for row in ops._ops(state)['records']['students'] if isinstance(row, dict)
                         and row.get('archived') is False and row.get('schoolId', school_id) == school_id
                         and all(isinstance(row.get(key), str) and row[key] for key in ('id', 'name', 'className'))}
        self.publications = {}
        self.models = {}
        self.resources = resources
        self.personal = None
        self.dated_groups = None

    def publication(self, version):
        if version in self.publications:
            return self.publications[version]
        connection = self.connection
        if connection is None or not publications._exists(connection, publications.TABLE):
            raise ValueError('Укажите существующую опубликованную версию')
        row = publications._row(connection, 'SELECT id,draft_revision,snapshot_json FROM school_publications '
                                'WHERE school_id=? AND version=?', (self.school_id, version))
        if not row:
            raise ValueError('Опубликованная версия школы не найдена')
        try:
            snapshot = json.loads(row['snapshot_json'])
            if (snapshot['schoolId'] != self.school_id or snapshot['version'] != version
                    or snapshot['publicationId'] != row['id']
                    or snapshot['draftRevision'] != row['draft_revision']
                    or snapshot.get('mode') == 'working_assumptions'):
                raise ValueError()
            start = date.fromisoformat(snapshot['periodStart'])
            if start.weekday() != 0 or start > date.max - timedelta(days=6):
                raise ValueError()
            rows = publications._canonical_grid(snapshot['lessons'])
            result = start, {lesson['id']: lesson for lesson in rows}, snapshot
            self.publications[version] = result
            return result
        except (TypeError, ValueError, KeyError, AttributeError, OverflowError):
            raise ValueError('Для версии нужна проверенная опубликованная сетка') from None

    def bind(self, record):
        version = record['publicationVersion']
        start, lessons, snapshot = self.publication(version)
        lesson = lessons.get(record['lessonId'])
        if lesson is None:
            raise ValueError('Урок не найден в указанной публикации')
        if (start + timedelta(days=lesson['day'])).isoformat() != record['lessonDate']:
            raise ValueError('Дата не соответствует дню урока в публикации')
        student = self.students.get(record['studentId'])
        if not student or canonical(student['className']) != canonical(lesson['className']):
            raise ValueError('Ученик не найден в классе этого урока')
        self._require_membership(version, snapshot, lesson, student, record['lessonDate'])
        return {**record, 'className': lesson['className'], 'subject': lesson['subject']}

    def lesson_membership(self, schedule, version, snapshot, lesson):
        if schedule.learning.school != self.school_id:
            raise ValueError('Подгруппы относятся к другой школе')
        if version not in self.models:
            model = schedule._model(self.connection, snapshot)
            legacy = model is None and schedule._learning_scope(self.connection, snapshot) is None
            self.models[version] = model, legacy
        model, legacy = self.models[version]
        if legacy:
            return None, None
        membership = schedule._classification(lesson, model, snapshot) if model else None
        if not membership:
            raise ValueError('Принадлежность урока к классу или подгруппе требует проверки')
        return model, membership

    def _require_membership(self, version, snapshot, lesson, student, lesson_date):
        learning = configured_service()
        schedule = StudentScheduleService(learning)
        model, membership = self.lesson_membership(schedule, version, snapshot, lesson)
        if membership is None or any(group['kind'] == 'whole' for group in membership):
            return
        if self.personal is None:
            connection = learning._connect(readonly=True)
            if connection is None:
                self.personal = learning._empty()
            else:
                # Keep the verified personal snapshot through the domain commit.
                self.resources.enter_context(closing(connection))
                connection.execute('BEGIN')
                self.personal = learning._load(connection)
        from .dated_student_groups import _validated_snapshot
        pupil = {**student, 'id': student['studentId']}
        if self.dated_groups is None:
            self.dated_groups = _validated_snapshot(schedule, self.personal)
        binding = self.dated_groups.resolve(pupil, version, lesson_date, snapshot, model)
        if binding is None:
            active = publications._active_row(self.school_id, self.connection)
            if not active or active['version'] != version:
                raise ValueError('Для прошедшей версии требуется подтверждённый состав подгруппы на дату урока')
            groups, signature = schedule._membership(model, student['className'])
            binding = schedule._binding(self.personal, pupil, groups, signature)
        if binding['status'] != 'confirmed':
            raise ValueError('Состав подгруппы на дату урока требует подтверждения')
        if not schedule._attends(membership, binding):
            raise ValueError('Принадлежность ученика к подгруппе этого урока не подтверждена')

    def references(self, version):
        try:
            if version is None:
                active = publications.read_active_snapshot(self.school_id, self.connection) if self.connection else None
                if active is None:
                    raise ValueError('Для справочника нужно опубликованное расписание')
                version = active['version']
            start, lessons, _ = self.publication(version)
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        classes = {canonical(lesson['className']) for lesson in lessons.values()}
        return {'publicationVersion': version, 'students': [student for student in self.students.values()
                if canonical(student['className']) in classes],
                'lessons': [{'lessonId': lesson['id'], 'lessonDate': (start + timedelta(days=lesson['day'])).isoformat(),
                             'className': lesson['className'], 'subject': lesson['subject']} for lesson in lessons.values()]}
