"""Role-specific entrances to the existing school workflows in MAX."""
from base64 import b64encode
from functools import lru_cache
from pathlib import Path


HOME = {
    'student': (
        ('Моё расписание', '/schedule'), ('Мои задания', '/learning'),
        ('Мои оценки', '/journal'), ('Мои кружки', '/clubs'),
        ('Моя учёба', '/section study'),
    ),
    'teacher': (
        ('Моё расписание', '/schedule'), ('Работы учеников', '/learning'),
        ('Мои материалы', '/materials'), ('Учебный день', '/section school'),
        ('Моя работа', '/section study'),
    ),
    'planner': (
        ('Расписание школы', '/schedule'), ('Замены уроков', '/replacements'),
        ('Учебный день', '/section school'), ('Планирование', '/section management'),
    ),
    'director': (
        ('Расписание школы', '/schedule'), ('Замены уроков', '/replacements'),
        ('Учебная работа', '/section study'), ('Работа школы', '/section school'),
        ('Управление', '/section management'),
    ),
    'platform_admin': (
        ('Расписание школы', '/schedule'), ('Календарь', '/calendar'),
        ('Учебная работа', '/section study'), ('Работа школы', '/section school'),
        ('Управление', '/section management'),
    ),
    'access_admin': (('Люди и доступ', '/admin'),),
}

PROFILE_HEADINGS = {
    'student': ('Кабинет ученика', 'Расписание, задания и оценки под рукой.'),
    'teacher': ('Кабинет преподавателя', 'Уроки, материалы и работы учеников.'),
    'planner': ('Кабинет завуча', 'Расписание, замены и публикации.'),
    'director': ('Кабинет директора', 'Расписание, учебная работа и управление.'),
    'platform_admin': ('Кабинет администратора', 'Настройки, данные и доступы.'),
    'access_admin': ('Кабинет доступа', 'Приглашения и права сотрудников.'),
}

SECTIONS = {
    'study': (
        'Учебная работа', 'Выберите, чем хотите заняться.', 'study',
        {
            'student': (
                ('Консультации', '/consultations'), ('После пропусков', '/recovery'),
                ('Уведомления', '/notifications'),
            ),
            'teacher': (
                ('Журнал класса', '/journal'), ('Консультации', '/consultations'),
                ('После пропусков', '/recovery'), ('ИИ-помощник', '/ai'),
            ),
            'director': (
                ('Оценки и явка', '/journal'), ('Кружки и документы', '/clubs'),
                ('Материалы замен', '/materials'),
            ),
            'platform_admin': (('Журнал', '/journal'), ('Кружки и документы', '/clubs')),
        },
    ),
    'school': (
        'Учебный день', 'Календарь, изменения уроков и сообщения.', 'operations',
        {
            'teacher': (
                ('Календарь', '/calendar'), ('Мои пожелания', '/preferences'),
                ('Обращения', '/requests'), ('Итоги замен', '/outcomes'),
                ('Уведомления', '/notifications'),
            ),
            'planner': (
                ('Календарь', '/calendar'), ('Пожелания учителей', '/preferences'),
                ('Обращения', '/requests'), ('Отработки', '/makeups'),
                ('Итоги замен', '/outcomes'), ('Уведомления', '/notifications'),
                ('Материалы замен', '/materials'),
            ),
            'director': (
                ('Календарь', '/calendar'), ('Пожелания учителей', '/preferences'),
                ('Обращения', '/requests'), ('Отработки', '/makeups'),
                ('Итоги замен', '/outcomes'), ('Уведомления', '/notifications'),
            ),
            'platform_admin': (
                ('Обращения', '/requests'), ('Замены уроков', '/replacements'),
                ('Отработки', '/makeups'),
            ),
        },
    ),
    'management': (
        'Управление', 'Публикации, отчёты и доступы.', 'operations',
        {
            'planner': (
                ('Публикации', '/publish'), ('Отчёты', '/analytics'),
                ('История версий', '/restore'),
            ),
            'director': (
                ('Публикации', '/publish'), ('Отчёты', '/analytics'),
                ('Импорт и экспорт', '/exchange'), ('История версий', '/restore'),
                ('Люди и доступ', '/admin'), ('Пригласить', '/people'),
                ('Доставка изменений', '/max notices'),
            ),
            'platform_admin': (
                ('Публикации', '/publish'), ('Отчёты', '/analytics'),
                ('Импорт и экспорт', '/exchange'), ('История версий', '/restore'),
                ('Люди и доступ', '/admin'), ('Пригласить', '/people'),
            ),
        },
    ),
}

ASSETS = Path(__file__).with_name('assets') / 'max-visual'
HOME_BUTTON = ('Главная', '/home')


def buttons(entries):
    return [{'label': label, 'command': command} for label, command in entries]


def home_reply(profile):
    role = profile.get('role', '')
    name = profile.get('name', 'Тестовый пользователь')
    title, summary = PROFILE_HEADINGS.get(role, ('Личный кабинет', 'Выберите действие.'))
    entries = (*HOME.get(role, ()), ('Мой профиль', '/role'))
    return {'text': f'{name}\n{title}\n\n{summary}', 'buttons': buttons(entries), 'cover': 'home'}


def section_reply(role, section):
    definition = SECTIONS.get(section)
    if definition is not None:
        title, summary, cover, by_role = definition
        entries = by_role.get(role)
        if entries:
            home_label = next((label for label, command in HOME[role] if command == '/section ' + section), title)
            return {'text': home_label + '\n\n' + summary, 'buttons': buttons((*entries, HOME_BUTTON)), 'cover': cover}
    return {'text': 'Этот раздел недоступен вашему профилю.', 'buttons': buttons((HOME_BUTTON,))}


def help_reply():
    return {
        'text': 'Выберите раздел кнопкой. Бот покажет доступные действия.\n\n'
                'Чтобы прервать ввод, нажмите «Отмена». «Главная» открывает ваш кабинет.',
        'buttons': buttons((HOME_BUTTON,)),
    }


def section_parent(role, command):
    if any(entry[1] == command for entry in HOME.get(role, ())):
        return None
    for key, (_, _, _, by_role) in SECTIONS.items():
        if any(entry[1] == command for entry in by_role.get(role, ())):
            return '/section ' + key
    return None


@lru_cache(maxsize=4)
def cover_image(key):
    if key not in {'home', 'study', 'operations', 'hackathon-school'}:
        return None
    name = key + '.jpg'
    try:
        raw = (ASSETS / name).read_bytes()
    except OSError:
        return None
    return {'name': name, 'contentType': 'image/jpeg', 'base64': b64encode(raw).decode('ascii')}
