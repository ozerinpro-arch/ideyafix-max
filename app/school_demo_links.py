"""Fixed local entry points for the fictional school cabinets."""
from urllib.parse import urlencode


DEMO_PORT = 5212
DEMO_ORIGIN = f'http://127.0.0.1:{DEMO_PORT}'
DEMO_ROLES = {
    'student': ('Ученик', 'Личное расписание, задания и оценки.', 'Мои задания', 'learning'),
    'teacher': ('Преподаватель', 'Мои уроки, материалы и работы учеников.', 'Мои материалы', 'materials'),
    'planner': ('Завуч', 'Расписание школы, замены и публикации.', 'Замены уроков', 'replacements'),
    'director': ('Директор', 'Расписание, учебные отчёты и управление.', 'Управление', 'management'),
}


def demo_url(role='student', view=None):
    query = {'role': role}
    if view is not None:
        query['view'] = view
    return DEMO_ORIGIN + '/?' + urlencode(query)


DEMO_URLS = frozenset(
    demo_url(role, view)
    for role, (_, _, _, action) in DEMO_ROLES.items()
    for view in (None, 'schedule', action)
)
REUSABLE_NAVIGATION = (
    '/home', '/start', '/help', '/role', '/section study', '/section school',
    '/section management', '/demo', *(f'/demo {role}' for role in DEMO_ROLES),
)


def demo_role_reply(role):
    if role not in DEMO_ROLES:
        return {
            'text': 'Школа действия · демо-роли\n\nВыберите кабинет с вымышленными данными.',
            'buttons': [
                *({'label': details[0], 'command': '/demo ' + key} for key, details in DEMO_ROLES.items()),
                {'label': 'Главная', 'command': '/home'},
            ],
        }
    label, description, action_label, action = DEMO_ROLES[role]
    url = demo_url(role)
    return {
        'text': f'Школа действия · {label}\n{description}\n\nЛокальное демо на этом Mac\n{url}',
        'buttons': [
            {'label': 'Открыть демо-кабинет', 'url': url},
            {'label': 'Расписание', 'url': demo_url(role, 'schedule')},
            {'label': action_label, 'url': demo_url(role, action)},
            {'label': 'Другая роль', 'command': '/demo'},
            {'label': 'Главная', 'command': '/home'},
        ],
    }
