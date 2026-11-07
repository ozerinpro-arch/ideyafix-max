"""Pure, role-checked commands for two explicitly synthetic demonstrations."""
from copy import deepcopy
from datetime import datetime, timezone
import secrets

ACTORS = [
    dict(id=710001, name='Лера Демо', role='participant', label='Участница хакатона'),
    dict(id=710002, name='Тимур Демо', role='participant', label='Участник хакатона'),
    dict(id=710003, name='Марина Демо', role='mentor', label='Преподаватель демокурса'),
    dict(id=710004, name='Саша Демо', role='learner', label='Ученик демокурса'),
    dict(id=710005, name='Роман Демо', role='organizer', label='Организатор хакатона'),
]
BY_ID = {a['id']: a for a in ACTORS}
LESSON = 'От идеи к гипотезе'
INSTRUCTIONS = 'Опишите проблему, для кого она важна и как проверить решение за один день. Достаточно трёх предложений.'

def now():
    return datetime.now(timezone.utc).isoformat()

def seed():
    return {'kind': 'ideyafix-synthetic-v1', 'teams': [], 'assignments': [
        dict(id='З-001', title=LESSON, instructions=INSTRUCTIONS,
             learnerId=710004, mentorId=710003, state='assigned', submission=None,
             review=None, history=[], createdAt=now())]}

def my_team(data, actor_id):
    return next((t for t in data['teams'] if actor_id in t['memberIds']), None)

def pair(argument):
    a, separator, b = argument.partition('|')
    if not separator or not a.strip() or not b.strip():
        return None
    return a.strip(), b.strip()

def command(data, actor_id, text, *, actor_override=None):
    """Mutate only a caller-owned test object and return the bot's actual reply."""
    actor = actor_override if actor_override is not None else BY_ID[actor_id]
    verb, _, arg = text.strip().partition(' ')
    verb, arg = verb.lower(), arg.strip()
    role = actor['role']
    if verb in {'/start', '/help'}:
        return (f"{actor['name']}, это тестовый бот «Идеяфикс». Все люди и данные вымышлены. "
                'Выберите «Хакатон» или «Учебное демо». Настоящие сообщения MAX не отправляются.')
    if verb == '/hackathon':
        team = my_team(data, actor_id)
        if role == 'organizer':
            return 'Вы организатор тестового хакатона. /applications — заявки команд; /approve ID — принять; /return ID | комментарий — вернуть.'
        if role != 'participant':
            return 'Для участия выберите Леру или Тимура Демо. Преподаватель и ученик доступны в учебном демо.'
        return (f"Ваша команда: {team['name']}. Код: {team['code']}. /idea Название | Описание → /apply."
                if team else 'Создайте команду: /team Название. Или присоединитесь по коду: /join КОД. Затем опишите идею и отправьте тестовую заявку.')
    if verb == '/education':
        if role == 'mentor':
            return 'Вы преподаватель демокурса. /task Название | Инструкция — новое задание; /review принять | Комментарий или /review доработать | Комментарий — проверка ответа.'
        if role == 'learner':
            return 'Вы ученик демокурса. /lessons — задания, /submit Текст ответа — сдать последнее открытое задание, /progress — результат.'
        return 'Учебное демо: переключитесь на Марину Демо (преподаватель) или Сашу Демо (ученик). Все материалы учебные и вымышленные.'
    if verb in {'/team', '/join', '/idea', '/apply'}:
        if role != 'participant':
            return 'Эта команда доступна участникам хакатона — Лере и Тимуру Демо.'
        team = my_team(data, actor_id)
        if verb == '/team':
            if team:
                return f"Вы уже в команде «{team['name']}». Код: {team['code']}."
            if not 2 <= len(arg) <= 60:
                return 'Укажите название команды от 2 до 60 символов: /team Спектр.'
            if any(t['name'].casefold() == arg.casefold() for t in data['teams']):
                return 'Такое название команды уже занято. Выберите другое.'
            code = secrets.token_hex(3).upper()
            while any(t['code'] == code for t in data['teams']):
                code = secrets.token_hex(3).upper()
            data['teams'].append(dict(id=f"К-{len(data['teams']) + 1:03}", name=arg,
                code=code, memberIds=[actor_id], idea=None, application=None))
            return f'Команда «{arg}» создана. Код для второго участника: {code}. Дальше: /idea Название | Описание.'
        if verb == '/join':
            if team:
                return f"Вы уже в команде «{team['name']}»."
            target = next((t for t in data['teams'] if t['code'] == arg.upper()), None)
            if not target:
                return 'Команда по этому коду не найдена. Проверьте код у создателя команды.'
            if target['application']:
                return 'Состав этой команды уже зафиксирован заявкой. Для демо создайте другую команду.'
            target['memberIds'].append(actor_id)
            return f"Вы присоединились к команде «{target['name']}»."
        if not team:
            return 'Сначала создайте команду /team Название или присоединитесь по коду /join КОД.'
        if verb == '/idea':
            values = pair(arg)
            if not values or not 2 <= len(values[0]) <= 100 or not 5 <= len(values[1]) <= 1500:
                return 'Формат: /idea Название | Описание. Название 2–100 символов, описание 5–1500.'
            if team['application'] and team['application']['status'] != 'revision':
                return 'Заявка уже отправлена. Изменение идеи доступно после возврата организатором.'
            team['idea'] = dict(title=values[0], description=values[1])
            return f'Идея «{values[0]}» сохранена. /apply — отправить тестовую заявку.'
        if not team['idea']:
            return 'Сначала опишите идею: /idea Название | Описание.'
        application = team['application']
        if application and application['status'] != 'revision':
            return f"Заявка {application['id']} уже существует. Статус: {application['status']}. Повторная заявка не создана."
        if not application:
            application = dict(id=f"ИФ-{len([t for t in data['teams'] if t['application']]) + 1:03}",
                status='submitted', comment='', attempt=1)
            team['application'] = application
        else:
            application.update(status='submitted', comment='', attempt=application['attempt'] + 1)
        return f"Тестовая заявка {application['id']} отправлена организатору внутри стенда. Реальная заявка на хакатон не подавалась."
    if verb in {'/applications', '/approve', '/return'}:
        if role != 'organizer':
            return 'Заявки и решения доступны организатору — Роману Демо.'
        applications = [(t, t['application']) for t in data['teams'] if t['application']]
        if verb == '/applications':
            return '\n'.join(f"{a['id']} · {t['name']} · {a['status']}" for t, a in applications) or 'Пока нет тестовых заявок. Создайте команду и отправьте идею от участника.'
        values = pair(arg) if verb == '/return' else None
        if verb == '/return' and (not values or len(values[1]) > 1000):
            return 'Формат: /return ИФ-001 | Что нужно доработать (до 1000 символов).'
        identifier = values[0] if values else arg
        item = next(((t, a) for t, a in applications if a['id'] == identifier), None)
        if not item:
            return 'Заявка не найдена. Проверьте ID через /applications.'
        team, application = item
        if application['status'] != 'submitted':
            return 'По этой версии заявки уже есть решение. Повторного решения не записано.'
        application.update(status='accepted' if verb == '/approve' else 'revision', comment='' if verb == '/approve' else values[1])
        return f"Заявка {identifier} команды «{team['name']}»: {'принята в тестовом стенде' if verb == '/approve' else 'возвращена на доработку'}."
    if verb in {'/lessons', '/task', '/submit', '/review', '/progress'}:
        if verb in {'/lessons', '/progress'}:
            tasks = [t for t in data['assignments'] if (role == 'mentor' and t['mentorId'] == actor_id) or (role == 'learner' and t['learnerId'] == actor_id)]
            if not tasks:
                return 'Учебные задания доступны Марине и Саше Демо. Выберите их в списке тестовых пользователей.'
            recent = list(reversed(tasks))[:2]
            return '\n\n'.join(f"{t['id']} · {t['title']} · {t['state']}\n{t['instructions'][:600]}" + (f"\nПроверка: {t['review']['comment'][:500]}" if t['review'] else '') for t in recent) + ('\n\nОстальные задания доступны в панели демо.' if len(tasks) > 2 else '')
        if verb == '/task':
            if role != 'mentor':
                return 'Выдавать задания может только преподаватель — Марина Демо.'
            values = pair(arg)
            if not values or not 2 <= len(values[0]) <= 100 or not 5 <= len(values[1]) <= 1500:
                return 'Формат: /task Название | Инструкция. Название 2–100 символов, инструкция 5–1500.'
            if len(data['assignments']) >= 30:
                return 'В демо уже 30 заданий. Используйте открытые задания.'
            task = dict(id=f"З-{len(data['assignments']) + 1:03}", title=values[0], instructions=values[1],
                learnerId=710004, mentorId=actor_id, state='assigned', submission=None, review=None, history=[], createdAt=now())
            data['assignments'].append(task)
            return f"Задание {task['id']} выдано Саше Демо. Переключитесь на ученика и отправьте /submit Текст ответа."
        if verb == '/submit':
            if role != 'learner':
                return 'Сдать ответ может ученик — Саша Демо.'
            if not 3 <= len(arg) <= 1800:
                return 'Укажите ответ от 3 до 1800 символов: /submit Текст ответа.'
            task = next((t for t in reversed(data['assignments']) if t['learnerId'] == actor_id and t['state'] in {'assigned', 'revision'}), None)
            if not task:
                return 'Нет открытого задания для сдачи. Дождитесь проверки или нового задания.'
            attempt = (task['submission']['attempt'] if task['submission'] else 0) + 1
            task.update(state='submitted', submission=dict(text=arg, attempt=attempt, submittedAt=now()), review=None)
            task['history'].append(dict(kind='submission', attempt=attempt, text=arg, at=now()))
            return f"Ответ на {task['id']} сохранён. Попытка {attempt}; ожидает проверки преподавателя."
        if role != 'mentor':
            return 'Проверять ответы может преподаватель — Марина Демо.'
        values = pair(arg)
        if not values or values[0].lower() not in {'принять', 'доработать'} or len(values[1]) > 1000:
            return 'Формат: /review принять | Комментарий или /review доработать | Комментарий (до 1000 символов).'
        task = next((t for t in reversed(data['assignments']) if t['mentorId'] == actor_id and t['state'] == 'submitted'), None)
        if not task:
            return 'Пока нет ответа, ожидающего проверки.'
        decision = 'accepted' if values[0].lower() == 'принять' else 'revision'
        review = dict(decision=decision, comment=values[1], reviewedAt=now())
        task.update(state=decision, review=review)
        task['history'].append(dict(kind='review', **review))
        return f"Задание {task['id']}: {'принято' if decision == 'accepted' else 'возвращено на доработку'}. {values[1]}"
    return 'Команда не распознана. Выберите действие под чатом или отправьте /start. Свободный текст в этом демо не обрабатывается ИИ.'

def view(data, actor_id):
    actor = deepcopy(BY_ID[actor_id])
    teams = [dict(id=t['id'], name=t['name'], code=t['code'], members=[BY_ID[i]['name'] for i in t['memberIds']],
                  idea=deepcopy(t['idea']), application=deepcopy(t['application'])) for t in data['teams']]
    tasks = []
    for t in reversed(data['assignments']):
        if (actor['role'] == 'mentor' and t['mentorId'] == actor_id) or (actor['role'] == 'learner' and t['learnerId'] == actor_id):
            tasks.append({**{k: deepcopy(t[k]) for k in ['id', 'title', 'instructions', 'state', 'submission', 'review', 'history']},
                'learner': deepcopy(BY_ID[t['learnerId']]), 'mentor': deepcopy(BY_ID[t['mentorId']])})
    actions = [dict(label='Главное меню', text='/start')]
    team = my_team(data, actor_id)
    if actor['role'] == 'participant':
        if not team:
            actions.append(dict(label='Создать команду', text='/team Спектр' if actor_id == 710001 else '/team Вектор'))
            actions.extend(dict(label='Вступить: ' + t['name'], text='/join ' + t['code']) for t in data['teams'] if not t['application'])
        elif not team['idea'] or team['application'] and team['application']['status'] == 'revision':
            actions.append(dict(label='Добавить идею', text='/idea Помощник команды | Бот помогает команде собрать гипотезы, выбрать одну и подготовить демонстрацию.'))
        if team and team['idea'] and (not team['application'] or team['application']['status'] == 'revision'):
            actions.append(dict(label='Отправить заявку', text='/apply'))
    elif actor['role'] == 'organizer':
        actions.append(dict(label='Посмотреть заявки', text='/applications'))
        for t in data['teams']:
            a = t['application']
            if a and a['status'] == 'submitted':
                actions.extend([dict(label='Принять ' + a['id'], text='/approve ' + a['id']),
                    dict(label='Вернуть ' + a['id'], text='/return ' + a['id'] + ' | Уточните способ проверки гипотезы.')])
    elif actor['role'] == 'mentor':
        actions.append(dict(label='Выдать задание', text='/task Проверка гипотезы | Предложите один эксперимент и измеримый критерий успеха.'))
        if any(t['state'] == 'submitted' for t in tasks):
            actions.extend([dict(label='Принять ответ', text='/review принять | Хорошо: критерий успеха можно измерить.'),
                dict(label='На доработку', text='/review доработать | Добавьте измеримый критерий успеха.')])
    elif actor['role'] == 'learner':
        actions.append(dict(label='Мои задания', text='/lessons'))
        if any(t['state'] in {'assigned', 'revision'} for t in tasks):
            actions.append(dict(label='Сдать ответ', text='/submit Проверим идею на пяти тестовых участниках. Успех — четверо самостоятельно завершают сценарий.'))
        actions.append(dict(label='Мой прогресс', text='/progress'))
    return dict(actor=actor, teams=teams, assignments=tasks, quickActions=actions, mode='synthetic', realProviderCalls=0,
        summary=dict(participants=2, teams=len(teams), ideas=sum(t['idea'] is not None for t in teams),
            applications=sum(t['application'] is not None for t in teams),
            acceptedApplications=sum(t['application'] is not None and t['application']['status'] == 'accepted' for t in teams),
            assignments=len(tasks), acceptedAssignments=sum(t['state'] == 'accepted' for t in tasks)))
