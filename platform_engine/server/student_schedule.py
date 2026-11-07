"""Own published timetable and explicit subgroup membership; no draft fallback."""
from contextlib import closing, contextmanager
from copy import deepcopy
import json

from fastapi import HTTPException
from pydantic import Field, model_validator

from . import school_publications as publications
from .scheduling_models import Identifier, PlanningInput, StrictModel, canonical
from .student_learning import parse


class ScheduleBindingInput(StrictModel):
    requestId: str = Field(pattern=r'^[A-Za-z0-9_.:-]{16,80}$')
    studentId: str = Field(min_length=1, max_length=80)
    expectedRevision: int = Field(ge=0, le=2**31 - 1)
    expectedPublicationVersion: int = Field(ge=1, le=2**31 - 1)
    groupIds: list[Identifier] = Field(max_length=200)

    @model_validator(mode='after')
    def unique_groups(self):
        if len(self.groupIds) != len(set(self.groupIds)):
            raise ValueError('Повторная подгруппа')
        return self


def _issue(code, message):
    return {'code': code, 'message': message}


class StudentScheduleService:
    def __init__(self, learning):
        self.learning = learning

    @contextmanager
    def _published(self, connection=None, *, school=None):
        school = self.learning.school if school is None else school
        if self.learning.domain_storage is not None:
            if school != self.learning.school:
                raise HTTPException(503, 'Учебный источник относится к другой школе')
            with self.learning.domain_scope(connection) as domain:
                yield domain, publications.read_active_snapshot(school, domain)
            return
        if connection is not None:
            # The caller owns the domain transaction and its lifetime.
            yield connection, publications.read_active_snapshot(school, connection)
            return
        # Keep one immutable domain read transaction while personal bindings are
        # read/written. Publication and roster changes cannot cross its commit.
        connection = publications._read_connection()
        if connection is None:
            yield None, None
            return
        with closing(connection):
            connection.execute('BEGIN')
            active = publications.read_active_snapshot(school, connection)
            yield connection, active

    def _origin(self, connection, active):
        school = active['schoolId']
        if active.get('replacementId'):
            from .school_replacements import _origin_publication
            origin = _origin_publication(connection, active, school)
            if active.get('sourceRunId') != origin.get('runId'):
                raise ValueError('Invalid source run')
        else:
            origin = active
        published = publications._row(connection,
            'SELECT run_id,evidence_json FROM school_publications WHERE id=? AND school_id=?',
            (origin['publicationId'], school))
        if not published or origin['schoolId'] != school or origin.get('runId', published['run_id']) != published['run_id']:
            raise ValueError('Invalid source publication')
        evidence = json.loads(published['evidence_json'])
        if not isinstance(evidence, dict):
            raise ValueError('Invalid publication evidence')
        run = publications._row(connection, 'SELECT * FROM planning_runs WHERE id=?', (published['run_id'],)) \
            if publications._exists(connection, 'planning_runs') else None
        return origin, evidence, run

    def _confirmed_model(self, origin, evidence, run):
        if not run or run['status'] != 'approved' or evidence.get('effectiveInputHash') != run['input_hash']:
            return None
        raw = json.loads(run['input_json'])
        base = json.loads(run['base_snapshot_json'])
        if (publications._hash(raw) != run['input_hash']
                or base.get('calendarBinding', {}).get('schoolId') != origin['schoolId']):
            return None
        plan = PlanningInput.model_validate(raw)
        rows = publications._canonical_grid(origin['lessons'])
        if not plan.confirmed or publications._hash(rows) != evidence.get('materializedHash'):
            return None
        return {'groups': {group.id: group.model_dump() for group in plan.groups},
                'origin': {row['id']: row for row in rows}}

    def _model(self, connection, active):
        if not active:
            return None
        try:
            return self._confirmed_model(*self._origin(connection, active))
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            return None

    def _learning_scope(self, connection, active, *, teacher_refs=None):
        """None permits legacy class access. An empty scope grants no lessons."""
        if not active:
            return None
        try:
            origin, evidence, run = self._origin(connection, active)
            model = self._confirmed_model(origin, evidence, run)
            lesson_markers = {'planningGroups', '_planningGroups', 'planningEventId', '_planningEventId'}
            proof_markers = {'effectiveInputHash', 'normativeInputHash', 'materializedHash', 'validator'}
            working_markers = {'experimentalId', 'experimentalVersionHash', 'workingAssumptions'}
            if (model is None and run is None and not proof_markers & evidence.keys()
                    and origin.get('mode') != 'working_assumptions' and not working_markers & origin.keys()
                    and not any(lesson_markers & row.keys() for snapshot in (active, origin) for row in snapshot['lessons'])):
                return None
            rows = publications._canonical_grid(active['lessons'])
            # An approved working version can prove whole-class rows. It cannot
            # verify subgroup membership from the confirmed normative model.
            whole_only = (model is None and active is origin and run is None
                          and active.get('mode') == evidence.get('mode') == 'working_assumptions'
                          and active.get('experimentalVersionHash') == evidence.get('experimentalVersionHash')
                          and publications._hash(rows) == evidence.get('materializedHash'))
            if model is None and not whole_only:
                return None, {}
            by_class = {}
            for row in rows:
                if teacher_refs is not None and teacher_refs.isdisjoint(row['teachers']):
                    continue
                if model is not None:
                    membership = self._classification(row, model, active)
                else:
                    membership = [group for group in row.get('planningGroups', [])
                                  if group['kind'] == 'whole' and canonical(group['className']) == canonical(row['className'])]
                if membership:
                    by_class.setdefault(canonical(row['className']), []).append((row['subject'], membership))
            return model, by_class
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            return None, {}

    def _learning_subjects(self, scope, student, state):
        model, by_class = scope
        lessons = by_class.get(canonical(student['className']), [])
        if not lessons:
            return []
        binding = {'groupIds': []}
        if model is not None:
            groups, signature = self._membership(model, student['className'])
            binding = self._binding(state, student, groups, signature)
        return sorted({subject for subject, membership in lessons if self._attends(membership, binding)})

    @staticmethod
    def _attends(membership, binding):
        return any(group['kind'] == 'whole' or group['id'] in binding['groupIds'] for group in membership)

    def _membership(self, model, class_name):
        groups = sorted((group for group in model['groups'].values()
                         if canonical(group['className']) == canonical(class_name)), key=lambda group: group['id'])
        signature = [{key: group[key] for key in ('id', 'className', 'kind', 'partition', 'size')}
                     | {'attendanceUnitIds': sorted(group.get('attendanceUnitIds', []))} for group in groups]
        return groups, publications._hash(signature)

    def _classification(self, row, model, active):
        observed = row.get('planningGroups')
        if observed is None and active.get('replacementId'):
            previous = model['origin'].get(row['id'])
            geometry = ('className', 'day', 'slot', 'start', 'end', 'subject', 'rooms')
            if previous and all(row.get(key) == previous.get(key) for key in geometry):
                observed = previous.get('planningGroups')
        if not isinstance(observed, list) or not observed or len(observed) > 200:
            return None
        result = []
        for group in observed:
            known = model['groups'].get(group.get('id')) if isinstance(group, dict) else None
            if (not known or canonical(known['className']) != canonical(row['className'])
                    or any(group.get(key) != known[key] for key in ('id', 'className', 'kind', 'partition'))):
                return None
            result.append(known)
        if len({group['id'] for group in result}) != len(result):
            return None
        return result

    def _binding(self, state, student, groups, signature):
        value = state['bindings'].get(student['id'], {}).get('scheduleBinding')
        revision = value.get('revision', 0) if isinstance(value, dict) else 0
        if type(revision) is not int or not 0 <= revision <= 2**31 - 1:
            raise HTTPException(503, 'Привязка подгрупп требует проверки')
        allowed = {group['id'] for group in groups if group['kind'] == 'subgroup'}
        current = bool(isinstance(value, dict) and value.get('className') == canonical(student['className'])
                       and value.get('membershipHash') == signature
                       and isinstance(value.get('groupIds'), list)
                       and all(isinstance(ident, str) for ident in value['groupIds'])
                       and len(value['groupIds']) <= 200 and len(value['groupIds']) == len(set(value['groupIds']))
                       and set(value['groupIds']) <= allowed)
        return {'revision': revision, 'status': 'confirmed' if current else 'stale' if value else 'missing',
                'groupIds': list(value['groupIds']) if current else []}

    def own(self, actor, *, connection=None):
        self.learning._actor(actor, {'student'})
        with self._published(connection) as (domain, active):
            personal = self.learning._connect(readonly=True)
            if personal is None:
                raise HTTPException(403, 'Личная привязка ученика не подтверждена')
            with closing(personal):
                personal.execute('BEGIN')
                state = self.learning._load(personal)
                student = self.learning._student(actor, state, connection=domain)
                result = {'student': {'name': student['name'], 'className': student['className']},
                          'source': 'publication', 'publicationVersion': active['version'] if active else None,
                          'periodStart': active.get('periodStart') if active else None,
                          'publicationState': 'published' if active else 'required',
                          'bindingRevision': 0, 'bindingStatus': 'missing', 'complete': False,
                          'blockers': [], 'lessons': []}
                model = self._model(domain, active)
                if not active:
                    result['blockers'].append(_issue('publication_required', 'Расписание ещё не опубликовано.'))
                elif model is None:
                    result['blockers'].append(_issue('published_model_required', 'Для этой публикации не подтверждён состав классов и групп.'))
                else:
                    groups, signature = self._membership(model, student['className'])
                    binding = self._binding(state, student, groups, signature)
                    result.update(bindingRevision=binding['revision'], bindingStatus=binding['status'])
                    if not groups:
                        result['blockers'].append(_issue('published_class_missing', 'Класс пока отсутствует в подтверждённой опубликованной модели.'))
                    elif any(group['kind'] == 'subgroup' for group in groups) and binding['status'] != 'confirmed':
                        result['blockers'].append(_issue('subgroup_binding_required', 'Директору нужно подтвердить ваши подгруппы. Общие уроки класса показаны.'))
                    try:
                        rows = publications._canonical_grid(active['lessons'])
                    except (ValueError, TypeError, KeyError):
                        raise HTTPException(409, 'Опубликованное расписание требует проверки') from None
                    for row in rows:
                        if canonical(row['className']) != canonical(student['className']):
                            continue
                        membership = self._classification(row, model, active)
                        if membership is None:
                            result['blockers'].append(_issue('lesson_membership_unconfirmed', 'Принадлежность одного из занятий требует подтверждения.'))
                            continue
                        if self._attends(membership, binding):
                            result['lessons'].append({key: deepcopy(row[key]) for key in
                                ('id', 'className', 'day', 'slot', 'start', 'end', 'subject', 'teachers', 'rooms')})
                    result['lessons'].sort(key=lambda row: (row['day'], row['start'], row['id']))
                    result['complete'] = not result['blockers']
                self.learning._student(actor, state, connection=domain)
                return result

    def details(self, actor, student_id, *, connection=None):
        self.learning._actor(actor, {'director'})
        with self._published(connection) as (connection, active):
            student = self.learning._students(connection=connection).get(student_id)
            if not student:
                raise HTTPException(404, 'Запись ученика недоступна')
            model = self._model(connection, active)
            state = self.learning._read()
            groups, signature = self._membership(model, student['className']) if model else ([], '')
            binding = self._binding(state, student, groups, signature)
            self.learning._actor(actor, {'director'})
            return {'student': student, 'publicationVersion': active['version'] if active else None,
                    'binding': binding,
                    'groups': [{key: group[key] for key in ('id', 'name', 'className', 'kind', 'partition', 'size')}
                               for group in groups if group['kind'] == 'subgroup'],
                    'ready': bool(model and groups and self.learning._bound_account(state, student_id)),
                    'blockers': [] if model and groups else [_issue('published_model_required', 'Нужна опубликованная нормативная модель этого класса.')]}

    def save(self, actor, body, *, connection=None, before_return=None):
        self.learning._actor(actor, {'director'})
        data = parse(ScheduleBindingInput, body)
        with self._published(connection) as (connection, active):
            student = self.learning._students(connection=connection).get(data['studentId'])
            if not student:
                raise HTTPException(404, 'Запись ученика недоступна')
            with self.learning._transaction() as (_personal, state):
                # Current authority is checked before recovering a receipt/CAS.
                self.learning._actor(actor, {'director'})
                previous = self.learning._receipt(state, actor, data, 'schedule-binding')
                if previous:
                    if canonical(previous['student']['className']) != canonical(student['className']):
                        raise HTTPException(409, 'Класс ученика изменился; подтвердите подгруппы заново')
                    if before_return is not None:
                        before_return()
                    return previous
                model = self._model(connection, active)
                if not active or active['version'] != data['expectedPublicationVersion'] or model is None:
                    raise HTTPException(409, 'Публикация изменилась или её группы не подтверждены')
                groups, signature = self._membership(model, student['className'])
                binding = self._binding(state, student, groups, signature)
                if binding['revision'] != data['expectedRevision'] or binding['revision'] == 2**31 - 1:
                    raise HTTPException(409, 'Привязка подгрупп изменилась; обновите данные')
                account = state['bindings'].get(student['id'])
                if not account or not self.learning._bound_account(state, student['id']):
                    raise HTTPException(409, 'Нужен действующий личный доступ ученика')
                options = {group['id']: group for group in groups if group['kind'] == 'subgroup'}
                if not groups or not set(data['groupIds']) <= options.keys():
                    raise HTTPException(422, 'Выберите подгруппы из опубликованной модели этого класса')
                selected = [options[ident] for ident in data['groupIds']]
                partitions = [group['partition'] for group in selected if group['partition']]
                if len(partitions) != len(set(partitions)):
                    raise HTTPException(422, 'Нельзя выбрать несколько непересекающихся групп одного деления')
                units = [set(group.get('attendanceUnitIds', [])) for group in selected if group.get('attendanceUnitIds')]
                if units and not set.intersection(*units):
                    raise HTTPException(422, 'Выбранные группы не имеют общих подтверждённых участников')
                account['scheduleBinding'] = {'revision': binding['revision'] + 1,
                    'className': canonical(student['className']), 'groupIds': sorted(data['groupIds']),
                    'membershipHash': signature, 'publicationVersion': active['version'],
                    'approvedBy': actor['membershipId']}
                result = {'student': student, 'publicationVersion': active['version'],
                          'binding': {'revision': binding['revision'] + 1, 'status': 'confirmed',
                                      'groupIds': sorted(data['groupIds'])}}
                self.learning._actor(actor, {'director'})
                self.learning._command(state, actor, data, 'schedule-binding', 'completed', result)
                if before_return is not None:
                    before_return()
                return result
