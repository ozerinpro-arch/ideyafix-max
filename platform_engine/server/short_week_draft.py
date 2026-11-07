"""Pure dated carryover of an observed grid, never a normative solver model."""
from copy import deepcopy
from datetime import date, time, timedelta
import hashlib
import json
import re

from .timetable import EditableLessonInput, canonical, compute_issues


FIELDS = ('id', 'className', 'day', 'slot', 'start', 'end', 'subject', 'teachers', 'rooms')


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def source_rows_fingerprint(rows):
    return _hash([{key: row[key] for key in FIELDS} for row in rows])


def _validate_rules(rules):
    identifiers = set()
    keys = {'days', 'slots', 'minSlot', 'startAtOrAfter', 'startBefore', 'endAtOrBefore'}
    for rule in rules:
        if (not isinstance(rule, dict) or not isinstance(rule.get('id'), str) or not rule['id']
                or rule['id'] in identifiers or not isinstance(rule.get('message'), str)
                or not isinstance(rule.get('teachers'), list) or not rule['teachers']
                or any(not isinstance(name, str) or not name for name in rule['teachers'])
                or not (rule.get('allowed') or rule.get('forbidden'))):
            raise ValueError('Некорректное явное условие доступности.')
        identifiers.add(rule['id'])
        for windows in (rule.get('allowed', []), rule.get('forbidden', [])):
            if not isinstance(windows, list) or len(windows) > 100:
                raise ValueError('Некорректные окна доступности.')
            for window in windows:
                if not isinstance(window, dict) or not window or not set(window) <= keys:
                    raise ValueError('Неизвестное условие окна не применяется молча.')
                for field, low, high in (('days', 0, 5), ('slots', 1, 20)):
                    if field in window and (not isinstance(window[field], list) or not window[field]
                            or any(type(n) is not int or not low <= n <= high for n in window[field])):
                        raise ValueError('Некорректный день или номер урока.')
                if 'minSlot' in window and (type(window['minSlot']) is not int or not 1 <= window['minSlot'] <= 20):
                    raise ValueError('Некорректный начальный урок.')
                for field in ('startAtOrAfter', 'startBefore', 'endAtOrBefore'):
                    if field in window:
                        if not isinstance(window[field], str) or not re.fullmatch(r'\d{2}:\d{2}', window[field]):
                            raise ValueError('Некорректное время ограничения.')
                        time.fromisoformat(window[field])


def _window(row, window):
    return (('days' not in window or row['day'] in window['days'])
        and ('slots' not in window or row['slot'] in window['slots'])
        and ('minSlot' not in window or row['slot'] >= window['minSlot'])
        and ('startAtOrAfter' not in window or row['start'] >= window['startAtOrAfter'])
        and ('startBefore' not in window or row['start'] < window['startBefore'])
        and ('endAtOrBefore' not in window or row['end'] <= window['endAtOrBefore']))


def _rule_issues(rows, rules):
    result = []
    for rule in rules:
        names = {canonical(name) for name in rule['teachers']}
        for row in rows:
            if not names.intersection(map(canonical, row['teachers'])):
                continue
            if rule.get('subjectIncludes') and not any(canonical(s) in canonical(row['subject']) for s in rule['subjectIncludes']):
                continue
            if rule.get('classGrades'):
                grade = re.match(r'\d+', row['className'])
                if not grade or int(grade[0]) not in rule['classGrades']:
                    continue
            allowed = not rule.get('allowed') or any(_window(row, w) for w in rule['allowed'])
            forbidden = any(_window(row, w) for w in rule.get('forbidden', []))
            if not allowed or forbidden:
                result.append({'id': 'swi_' + _hash([rule['id'], row['id']])[:20],
                    'kind': 'teacher_availability', 'code': 'explicit_availability', 'severity': 'error',
                    'conditionId': rule.get('conditionId', rule['id']), 'ruleId': rule['id'],
                    'message': (f"{', '.join(row['teachers'])}: {row['className']}, "
                        f"{row.get('date', 'день ' + str(row['day'] + 1))}, {row['slot']} урок "
                        f"({row['start']}–{row['end']}). {rule['message']} "
                        f"Источник: {row.get('sourceCell') or row.get('sourceLessonId') or row['id']}"),
                    'lessonIds': [row['id']],
                    'sourceCells': [row.get('sourceCell', '')], 'evidence': deepcopy(rule.get('evidence'))})
    return result


def _conflict_pairs(rows):
    """Strict interval pairs for a repair; no inferred disjoint subgroups."""
    result = set()
    for index, first in enumerate(rows):
        for second in rows[index + 1:]:
            if (first['day'] != second['day'] or first['start'] >= second['end']
                    or second['start'] >= first['end']):
                continue
            ids = tuple(sorted((first['id'], second['id'])))
            if canonical(first['className']) == canonical(second['className']):
                result.add(('class', *ids))
            for field in ('teachers', 'rooms'):
                if set(map(canonical, first[field])).intersection(map(canonical, second[field])):
                    result.add((field, *ids))
    return result


def build_short_week_draft(source_draft, calendar, *, source_basis=None, rules=None, repairs=None):
    """Copy observed weekdays, explicitly cancelling closed dates and unknown rooms.

    Source-backed repairs are applied only to the exact audited row fingerprint,
    and only when they introduce no class/teacher/room or explicit-rule conflict.
    Existing ambiguous overlaps stay visible. No weekly-count proration occurs.
    """
    source = deepcopy(source_draft)
    cal, basis = deepcopy(calendar), deepcopy(source_basis or {})
    rules = deepcopy(basis.get('rules', []) if rules is None else rules)
    repairs = deepcopy(basis.get('repairs', []) if repairs is None else repairs)
    if (not isinstance(source, dict) or type(source.get('revision')) is not int
            or source['revision'] < 0 or not isinstance(source.get('lessons'), list)
            or not 1 <= len(source['lessons']) <= 2000):
        raise ValueError('Ожидается ограниченная исходная школьная сетка.')
    if not isinstance(rules, list) or len(rules) > 100 or not isinstance(repairs, list) or len(repairs) > 20:
        raise ValueError('Слишком много условий или перестановок.')
    _validate_rules(rules)
    first = date.fromisoformat(cal['periodStart'])
    if first.weekday() != 0:
        raise ValueError('Начало недели должно быть понедельником.')
    teaching, closed = set(cal['teachingDates']), set(cal['closedDates'])
    six_days = {(first + timedelta(days=day)).isoformat() for day in range(6)}
    if (not teaching or len(teaching) != len(cal['teachingDates'])
            or len(closed) != len(cal['closedDates']) or teaching & closed
            or teaching | closed != six_days or not isinstance(cal.get('source'), str) or not cal['source']):
        raise ValueError('Явно задайте все учебные и закрытые даты недели.')
    for stamp in teaching | closed:
        date.fromisoformat(stamp)
    original = []
    for raw in source['lessons']:
        if not isinstance(raw.get('id'), str) or not raw['id']:
            raise ValueError('Не указан идентификатор исходного урока.')
        clean = EditableLessonInput.model_validate({key: raw[key] for key in FIELDS if key != 'id'}).model_dump()
        original.append({'id': raw['id'], **clean,
            'sourceCell': raw.get('sourceCell', ''), 'raw': raw.get('raw', ''), 'edited': bool(raw.get('edited', False))})
    if len({row['id'] for row in original}) != len(original):
        raise ValueError('Повторяются исходные уроки.')
    fingerprint = source_rows_fingerprint(original)
    expected = basis.get('sourceRowsFingerprint')
    if ((expected is not None and fingerprint != expected)
            or (basis.get('originalExpectedRows') is not None and len(original) != basis['originalExpectedRows'])
            or (repairs and expected is None)):
        raise ValueError('Исходная сетка отличается от проверенной основы; перестановки не применены.')
    if basis.get('periodStart') and basis['periodStart'] != cal['periodStart']:
        raise ValueError('Основание относится к другой неделе.')
    source_start = date.fromisoformat(basis['sourcePeriodStart']) if basis.get('sourcePeriodStart') else None
    active, cancelled, provenance, unknowns = [], [], [], deepcopy(basis.get('unknowns', []))
    for row in original:
        stamp = (first + timedelta(days=row['day'])).isoformat()
        dated = {**deepcopy(row), 'id': 'swl_' + _hash([cal['periodStart'], row['id']])[:24],
            'sourceLessonId': row['id'], 'date': stamp}
        origin = {'lessonId': dated['id'], 'sourceLessonId': row['id'],
            'sourceCell': row['sourceCell'], 'sourceDay': row['day'],
            'sourceDate': (source_start + timedelta(days=row['day'])).isoformat() if source_start else None,
            'originalRooms': deepcopy(row['rooms']), 'originalSubject': row['subject'],
            'workingCarryover': True, 'basis': 'observed_weekday_template',
            'sourceFile': deepcopy(basis.get('sourceFile'))}
        invalid = [room for room in dated['rooms'] if canonical(room) == 'extra']
        if invalid:
            dated['rooms'] = [room for room in dated['rooms'] if canonical(room) != 'extra']
            first_line = dated['raw'].splitlines()[0].strip() if dated['raw'] else ''
            marker = re.search(r'\(\s*extra\s*\)\s*$', first_line, re.I)
            if marker and canonical(first_line[:marker.start()]) == canonical(dated['subject']):
                dated['subject'] = first_line
            origin.update(invalidRoomLabels=invalid, courseMarker='extra')
            dated['edited'] = True
        provenance.append(origin)
        if stamp in closed:
            cancelled.append({**dated, 'reason': 'calendar_closed', 'message': 'Каникулы: урок не перенесён на первые три дня.'})
        else:
            active.append(dated)
    before_pairs = _conflict_pairs(active)
    applied, rejected = [], []
    known_slots = {(row['day'], row['slot'], row['start'], row['end']) for row in original}
    for repair in repairs:
        current = {row['sourceLessonId']: row for row in active}
        ids = repair.get('lessonIds', [])
        try:
            selected = [current[ident] for ident in ids]
            if (repair['kind'] not in {'move', 'swap'} or len(ids) != (2 if repair['kind'] == 'swap' else 1)
                    or len(set(ids)) != len(ids) or any(len(r['teachers']) != 1 or len(r['rooms']) != 1 for r in selected)):
                raise ValueError('Перестановка требует однозначных уроков, педагогов и кабинетов.')
            proposed = deepcopy(active)
            targets = {row['sourceLessonId']: row for row in proposed}
            fields = ('day', 'slot', 'start', 'end')
            if repair['kind'] == 'swap':
                for field in fields:
                    targets[ids[0]][field], targets[ids[1]][field] = targets[ids[1]][field], targets[ids[0]][field]
            else:
                target = repair['target']
                if tuple(target[key] for key in fields) not in known_slots:
                    raise ValueError('Время перестановки отсутствует в наблюдаемых звонках.')
                targets[ids[0]].update({key: target[key] for key in fields})
            for ident in ids:
                changed = targets[ident]
                changed['date'] = (first + timedelta(days=changed['day'])).isoformat()
                if changed['date'] not in teaching:
                    raise ValueError('Перестановка попадает на каникулы.')
            old_rules = {(i['ruleId'], *i['lessonIds']) for i in _rule_issues(active, rules)}
            new_rules = _rule_issues(proposed, rules)
            if _conflict_pairs(proposed) - before_pairs or ({(i['ruleId'], *i['lessonIds']) for i in new_rules} - old_rules):
                raise ValueError('Перестановка создаёт новое пересечение или нарушение доступности.')
            changed_ids = {targets[i]['id'] for i in ids}
            if any(i.get('conditionId') == repair.get('conditionId') and changed_ids.intersection(i['lessonIds']) for i in new_rules):
                raise ValueError('Перестановка не устраняет указанное нарушение.')
            changes = [{'lessonId': targets[i]['id'], 'sourceLessonId': i,
                'before': {key: current[i][key] for key in fields},
                'after': {key: targets[i][key] for key in fields}} for i in ids]
            for ident in ids:
                targets[ident]['edited'] = True
            applied.append({'id': repair['id'], 'kind': repair['kind'], 'conditionId': repair.get('conditionId'),
                'message': repair['message'], 'changes': changes, 'evidence': deepcopy(repair.get('evidence'))})
            active = proposed
            before_pairs = _conflict_pairs(active)
        except (KeyError, TypeError, ValueError) as error:
            rejected.append({'id': repair.get('id', ''), 'message': str(error)})
    for row in active:
        if not row['rooms']:
            unknowns.append({'code': 'missing_room', 'label': 'Кабинет требует уточнения',
                'lessonIds': [row['id']], 'sourceCells': [row['sourceCell']]})
        if len(row['teachers']) > 1 or len(row['rooms']) > 1:
            unknowns.append({'code': 'group_resource_mapping_unknown', 'label': 'Не установлена связь педагогов, кабинетов и подгрупп',
                'lessonIds': [row['id']], 'sourceCells': [row['sourceCell']]})
    if not source_start:
        unknowns.append({'code': 'source_date_unknown', 'label': 'Дата исходной сетки не подтверждена.'})
    if not unknowns:
        unknowns.append({'code': 'working_carryover', 'label': 'Перенос исторической сетки требует проверки на выбранные даты.'})
    issues = compute_issues(active) + _rule_issues(active, rules)
    issues.extend({'id': 'swr_' + _hash(r)[:20], 'kind': 'repair_rejected', 'severity': 'error',
        'message': r['message'], 'lessonIds': []} for r in rejected)
    return {'mode': 'short_week_observed_carryover', 'periodStart': cal['periodStart'], 'calendar': cal,
        'lessons': active, 'cancelledLessons': cancelled, 'provenance': provenance, 'issues': issues,
        'unknowns': unknowns, 'repairsApplied': applied, 'repairsRejected': rejected,
        'sourceBinding': {'draftRevision': source['revision'], 'draftHash': _hash(source),
            'sourceRowsFingerprint': fingerprint, 'basisHash': _hash(basis)},
        'coverage': {'sourceRows': len(original), 'activeRows': len(active), 'cancelledRows': len(cancelled),
            'classes': len({r['className'] for r in active}), 'unresolvedRooms': sum(not r['rooms'] for r in active),
            'extraLabelsRemoved': sum(bool(p.get('invalidRoomLabels')) for p in provenance),
            'perDay': {str(day): sum(r['day'] == day for r in active) for day in range(6)}},
        'validation': {'valid': not unknowns and not issues, 'issues': issues,
            'knownAvailabilityViolations': len(_rule_issues(active, rules)),
            'scope': 'observed_intervals_and_explicit_rules', 'normativeModelConfirmed': False}}
