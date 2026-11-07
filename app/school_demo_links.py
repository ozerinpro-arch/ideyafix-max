"""Fixed local entry points for the fictional school cabinets."""
from urllib.parse import urlencode
import json
from pathlib import Path
import re
import stat
import os


DEMO_PORT = 5212
DEMO_ORIGIN = f'http://127.0.0.1:{DEMO_PORT}'
DEMO_ROLES = {
    'student': ('Ученик', 'Личное расписание, задания и оценки.', 'Мои задания', 'learning'),
    'teacher': ('Преподаватель', 'Мои уроки, материалы и работы учеников.', 'Мои материалы', 'materials'),
    'planner': ('Завуч', 'Расписание школы, замены и публикации.', 'Замены уроков', 'replacements'),
    'director': ('Директор', 'Расписание, учебные отчёты и управление.', 'Управление', 'management'),
}
WEB_ROLE_DESCRIPTIONS = {
    'student': 'Личные задания и переписка по учебным работам.',
    'teacher': 'Свои уроки, материалы и работы учеников.',
    'planner': 'Расписание, календарь и планирование учебной недели.',
    'director': 'Публикации расписания, учебные отчёты и доступы.',
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
SCHOOL_STAFF_URL = 'https://taskfloww.site/school-staff/'
ALLOWED_URLS = DEMO_URLS | {SCHOOL_STAFF_URL}
REUSABLE_NAVIGATION = (
    '/home', '/start', '/help', '/role', '/section study', '/section school',
    '/section management', '/demo', *(f'/demo {role}' for role in DEMO_ROLES),
)

def load_web_demo_accounts(path: Path):
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError('invalid_web_demo_accounts_file')
    info = path.stat()
    if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_size > 4096):
        raise ValueError('invalid_web_demo_accounts_file')
    try:
        accounts = json.loads(path.read_bytes())
        if not isinstance(accounts, dict) or set(accounts) != set(DEMO_ROLES):
            raise ValueError()
        for role, account in accounts.items():
            if (not isinstance(account, dict) or set(account) != {'login', 'password'}
                    or account['login'] != f'ifx.demo.{role}'
                    or not isinstance(account['password'], str)
                    or not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', account['password'])):
                raise ValueError()
        return accounts
    except (ValueError, UnicodeError):
        raise ValueError('invalid_web_demo_accounts_file') from None


def web_demo_role(value):
    name = value.strip().casefold()
    return next((role for role, details in DEMO_ROLES.items()
                 if name in (role, details[0].casefold())), '')


def web_demo_reply(role, accounts, *, personal_cabinet=False):
    if role not in accounts:
        buttons = [{'label': details[0], 'command': f'/demo {key}'}
                   for key, details in DEMO_ROLES.items()]
        keyboard_hint = 'Если кнопки не видны, напишите название роли.'
        if personal_cabinet:
            buttons.append({'label': 'Мой кабинет MAX', 'command': '/home'})
            keyboard_hint = 'Если кнопки не видны, отправьте /demo и название роли.'
        return {
            'text': ('Школа действия · хакатон Идеяфикс\n\n'
                     'Выберите роль. Бот покажет логин и пароль для входа на сайт школы. '
                     'Во всех кабинетах вымышленные данные.\n\n'
                     + keyboard_hint),
            'buttons': buttons,
        }
    label = DEMO_ROLES[role][0]
    description = WEB_ROLE_DESCRIPTIONS[role]
    account = accounts[role]
    login, password = account['login'], account['password']
    buttons = [
        {'label': 'Скопировать логин', 'copy': login},
        {'label': 'Скопировать пароль', 'copy': password},
        {'label': 'Перейти в школу', 'url': SCHOOL_STAFF_URL},
        {'label': 'Выбрать другую роль', 'command': '/demo'},
    ]
    if personal_cabinet:
        buttons.append({'label': 'Мой кабинет MAX', 'command': '/home'})
    return {
        'text': (f'Демо-кабинет · {label}\n{description}\n\n'
                 f'Логин: {login}\nПароль: {password}\n\n'
                 'Скопируйте логин и пароль, нажмите «Перейти в школу» и введите их на сайте. '
                 'Если там уже открыт другой кабинет, нажмите «Выйти» перед новым входом.'),
        'buttons': buttons,
    }


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
