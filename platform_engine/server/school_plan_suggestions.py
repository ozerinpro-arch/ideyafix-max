"""Read-only source mapping proposals from the explicit planning registry.

Identity matching is canonical equality only. Observed legacy class labels may
support a separately confirmed б→В proposal; they never create registry facts.
"""
from collections import defaultdict
from copy import deepcopy

from .scheduling_models import PlanningInput, canonical
from .school_plan_sources import (ConfirmedCount, SourceCell, SourcePackage,
                                  SourceResolutions, _parts, source_conditions)


LABELS = {
    'class_map_required': 'Обозначения классов',
    'group_map_required': 'Состав и привязка групп',
    'teacher_map_required': 'Фактические педагоги',
    'room_map_required': 'Разрешённые кабинеты',
    'count_or_subject_unresolved': 'Предметы и недельные часы',
    'source_condition_uncompiled': 'Условия документов и методдни',
    'stale_condition_attestation': 'Повторная проверка условий',
    'unallocated_source_row': 'Строки без распределения по классам',
    'source_rule_required_load': 'Обязательные курсы',
    'source_cell_excluded': 'Исключённые назначения',
}


def group_source_questions(issues):
    grouped = {}
    for issue in issues:
        key = issue['code']
        group = grouped.setdefault(key, {'code': key, 'label': LABELS.get(key, 'Проверка назначений'),
                                         'count': 0, 'sourceCells': set(), 'conditionIds': set(), 'examples': []})
        group['count'] += 1
        group['sourceCells'].update(issue.get('sourceCells', []))
        if issue.get('conditionId'): group['conditionIds'].add(issue['conditionId'])
        example = issue.get('clarification') or issue.get('message', '')
        if example and example not in group['examples'] and len(group['examples']) < 3:
            group['examples'].append(example)
    return [{**group, 'sourceCells': sorted(group['sourceCells']), 'conditionIds': sorted(group['conditionIds'])}
            for group in grouped.values()]


def source_mapping_suggestions(package, plan, resolutions=None, *, observed_classes=(), unresolved=None):
    """Propose missing mapping entries; never fill unknown entity attributes.

    Existing entries (including conflicting/invalid user choices) are retained
    for review. Client provenance is ignored. This function does not compile,
    solve, confirm, persist, or consume a legacy lesson as normative load.
    """
    source = SourcePackage.model_validate(package)
    supplied = deepcopy(resolutions or {})
    supplied.pop('existingProvenance', None)
    selected = SourceResolutions.model_validate(supplied)
    value = PlanningInput.model_validate(plan).model_dump(mode='json') if plan is not None else {
        'groups': [], 'teachers': [], 'rooms': [], 'slots': [], 'demands': []}
    mappings = {'classMap': {}, 'teacherMap': {}, 'groupMap': {}, 'roomIds': {}}
    reasons, skipped = [], []
    groups = {item['id']: item for item in value['groups']}
    teachers = {item['id']: item for item in value['teachers']}
    registry_classes = sorted({item['className'] for item in value['groups']})
    class_index, teacher_index = defaultdict(list), defaultdict(list)
    for name in registry_classes: class_index[canonical(name)].append(name)
    for item in value['teachers']: teacher_index[canonical(item['name'])].append(item['id'])
    active_cells = [cell for cell in source.cells if cell.sourceCell not in selected.excludedCells]
    source_classes = sorted({cell.classLabel for cell in active_cells})
    source_teachers = sorted({cell.teacher for cell in active_cells}
                             | {row['teacher'] for row in source.sourceRows if not row.get('sourceCells')})
    for name in source_classes:
        if name in selected.classMap: continue
        matches = class_index[canonical(name)]
        if len(matches) == 1:
            mappings['classMap'][name] = matches[0]
            reasons.append({'field': 'classMap', 'key': name, 'basis': 'exact_registry_name'})
        elif len(matches) > 1:
            skipped.append({'field': 'classMap', 'key': name, 'reason': 'ambiguous_registry_name'})
    for name in source_teachers:
        if name in selected.teacherMap: continue
        matches = teacher_index[canonical(name)]
        if len(matches) == 1:
            mappings['teacherMap'][name] = matches[0]
            reasons.append({'field': 'teacherMap', 'key': name, 'basis': 'exact_registry_name'})
        elif len(matches) > 1:
            skipped.append({'field': 'teacherMap', 'key': name, 'reason': 'ambiguous_registry_name'})
    classes = {**mappings['classMap'], **selected.classMap}
    teacher_map = {**mappings['teacherMap'], **selected.teacherMap}
    confirmed_counts = {key: ConfirmedCount.model_validate(count) for key, count in source.confirmedCounts.items()}
    missing = []
    for cell in source.cells:
        key, target_class = cell.sourceCell, classes.get(cell.classLabel)
        if key in selected.excludedCells:
            missing.append({'code': 'source_cell_excluded', 'sourceCells': [key],
                            'message': 'Назначение исключено пользователем: ' + selected.excludedCells[key]})
            continue
        teacher_id = selected.teacherOverrides[key].teacherId if key in selected.teacherOverrides else teacher_map.get(cell.teacher)
        parts = _parts(cell, selected, confirmed_counts)
        explicit = []
        if target_class and teacher_id in teachers and parts:
            for part in parts:
                matches = [demand for demand in value['demands']
                           if canonical(demand['subject']) == canonical(part['subject'])
                           and demand['weeklyCount'] == part['weeklyCount']
                           and set(demand['teacherIds']) == {teacher_id}
                           and target_class in {groups[g]['className'] for g in demand['groupIds']}]
                if len(matches) != 1: explicit = []; break
                explicit.append(matches[0])
        group_sets = [{gid for gid in demand['groupIds'] if groups[gid]['className'] == target_class} for demand in explicit]
        explicit_groups = sorted(group_sets[0]) if group_sets and all(ids == group_sets[0] for ids in group_sets) else []
        if key not in selected.groupMap and target_class:
            if explicit_groups:
                mappings['groupMap'][key] = explicit_groups
                reasons.append({'field': 'groupMap', 'key': key, 'basis': 'explicit_normative_demand'})
            elif any(g['className'] == target_class for g in value['groups']):
                skipped.append({'field': 'groupMap', 'key': key, 'reason': 'group_membership_requires_review'})
        actual_groups = selected.groupMap.get(key, mappings['groupMap'].get(key))
        if key not in selected.roomIds and explicit_groups and set(actual_groups or []) == set(explicit_groups):
            permitted = set.intersection(*(set(demand['roomIds']) for demand in explicit))
            if permitted:
                mappings['roomIds'][key] = sorted(permitted)
                reasons.append({'field': 'roomIds', 'key': key, 'basis': 'explicit_normative_demand'})
        for field, current, proposals, code in (
            ('classMap', selected.classMap, mappings['classMap'], 'class_map_required'),
            ('teacherMap', selected.teacherMap, mappings['teacherMap'], 'teacher_map_required'),
            ('groupMap', selected.groupMap, mappings['groupMap'], 'group_map_required'),
            ('roomIds', selected.roomIds, mappings['roomIds'], 'room_map_required'),
        ):
            mapping_key = cell.classLabel if field == 'classMap' else cell.teacher if field == 'teacherMap' else key
            if mapping_key not in current and mapping_key not in proposals:
                missing.append({'code': code, 'sourceCells': [key], 'message': LABELS[code]})
        if not parts:
            missing.append({'code': 'count_or_subject_unresolved', 'sourceCells': [key], 'message': 'Подтвердите предмет и недельные часы этой ячейки.'})

    # The sole tentative alias is visible separately and requires an explicit
    # user action. No Latin/Cyrillic transliteration or other fuzzy matching.
    observed = {str(name) for name in observed_classes if isinstance(name, str)}
    alias_index = defaultdict(list)
    for name in sorted(set(registry_classes) | observed): alias_index[canonical(name)].append(name)
    aliases = []
    for name in source_classes:
        if name in selected.classMap or name in mappings['classMap']: continue
        folded = canonical(name)
        if not folded.endswith('б') or not folded[:-1].isdigit(): continue
        matches = alias_index.get(folded[:-1] + 'в', [])
        if len(matches) == 1:
            aliases.append({'sourceClass': name, 'targetClass': matches[0], 'requiresConfirmation': True,
                            'basis': 'tentative_class_label_observation',
                            'message': 'Обозначение «б» источника может соответствовать «В» школы. Подтвердите это отдельно; совпадение не устанавливает состав группы.'})
    shared, occupied = [], ({key for link in selected.sharedLessons for key in link.sourceCells}
                            | selected.excludedCells.keys())
    cell_index = {cell.sourceCell: cell for cell in source.cells}
    source_files = {item.get('id'): item for item in source.sources}
    for observation in source.groupObservations:
        if (observation.get('status') != 'visually_confirmed_structural_link'
                or observation.get('relation') != 'horizontal_shared_annotation'
                or observation.get('verified_visually') is not True):
            continue
        keys, evidence = observation.get('sourceCells', []), observation.get('evidence', {})
        origin = source_files.get(evidence.get('sourceId'), {})
        if (len(keys) < 2 or len(keys) != len(set(keys)) or not set(keys) <= cell_index.keys()
                or evidence.get('sourceSha256') != origin.get('sha256') or not origin.get('sha256')
                or not isinstance(evidence.get('part'), str) or not evidence['part']):
            continue
        if set(keys) & occupied:
            continue
        cells = [cell_index[key] for key in keys]
        loads = [_parts(cell, selected, confirmed_counts) for cell in cells]
        actual_teachers = [selected.teacherOverrides[key].teacherId if key in selected.teacherOverrides
                           else teacher_map.get(cell_index[key].teacher, cell_index[key].teacher) for key in keys]
        if (any(not load for load in loads) or any(load != loads[0] for load in loads[1:])
                or len(set(actual_teachers)) != 1):
            skipped.append({'field': 'sharedLessons', 'key': ','.join(keys), 'reason': 'shared_source_or_assignment_conflict'})
            continue
        reason = f"Подтверждённая горизонтальная связь: {origin.get('file', evidence['sourceId'])}, {evidence['part']}; ячейки {', '.join(keys)}. Состав групп и кабинеты проверяются отдельно."
        shared.append({'sourceCells': sorted(keys), 'reason': reason, 'evidence': {
            'sourceId': evidence['sourceId'], 'sourceFile': origin.get('file', ''),
            'part': evidence['part'], 'sourceSha256': evidence['sourceSha256']}})
        occupied.update(keys)
    setup = [{'code': 'setup_registry', 'missing': [field for field in ('groups', 'teachers', 'rooms', 'slots') if not value[field]],
              'message': 'Заполните сохраняемые справочники планировщика. Численность групп, вместимость кабинетов, квалификации и пределы педагогов не подставляются из старой сетки.'}]
    if not setup[0]['missing']: setup = []
    if unresolved is None:
        missing.extend({'code': 'source_condition_uncompiled', 'sourceCells': [], 'conditionId': c['id'],
                        'message': 'Условия проверяются после подключения справочников и нагрузки.'}
                       for c in source_conditions(source.model_dump(mode='json')))
    return {'version': 1, 'mappings': deepcopy(mappings), 'reasons': reasons, 'skipped': skipped,
            'sharedLessons': shared,
            'classAliases': aliases, 'setupRegistry': setup,
            'questionGroups': group_source_questions(missing if unresolved is None else unresolved),
            'proposalCount': sum(len(value) for value in mappings.values()) + len(shared),
            'registryCounts': {field: len(value[field]) for field in ('groups', 'teachers', 'rooms', 'slots')},
            'notice': 'Предложения заполняют только отсутствующие соответствия в форме. Справочники, нормативы, сетка и подтверждения не изменяются; сохранение выполняется отдельно с проверкой версий.'}
