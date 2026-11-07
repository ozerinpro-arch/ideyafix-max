"""All names, records and aggregates below are fictional demonstration data."""
from copy import deepcopy


def seed_state() -> dict:
    teachers = [
        {"id": "t1", "name": "Кузнецова М.А.", "initials": "МК", "subject": "Биология", "color": "#7c73d9", "weeklyHours": 22, "available": False, "conflict": "Основной педагог"},
        {"id": "t2", "name": "Смирнова Е.В.", "initials": "ЕС", "subject": "Биология", "color": "#4ba593", "weeklyHours": 18, "available": True, "conflict": None},
        {"id": "t3", "name": "Волков А.И.", "initials": "АВ", "subject": "Биология", "color": "#c49351", "weeklyHours": 24, "available": False, "conflict": None},
        {"id": "t4", "name": "Морозова С.Н.", "initials": "СМ", "subject": "Биология", "color": "#b7769e", "weeklyHours": 20, "available": False, "conflict": None, "requiredRoom": "305"},
        {"id": "t5", "name": "Лебедев Д.О.", "initials": "ДЛ", "subject": "География", "color": "#6c99c3", "weeklyHours": 21, "available": True, "conflict": None},
        {"id": "t6", "name": "Фролова Н.С.", "initials": "НФ", "subject": "Математика", "color": "#899650", "weeklyHours": 23, "available": True, "conflict": None},
    ]
    schedule = [
        {"id": "l1", "day": 0, "slot": 3, "time": "10:30–11:15", "subject": "Биология", "className": "7А", "room": "205", "teacherId": "t1", "teacherName": "Кузнецова М.А.", "status": "normal", "materialId": "m1"},
        {"id": "l2", "day": 0, "slot": 1, "time": "08:30–09:15", "subject": "Биология", "className": "7А", "room": "205", "teacherId": "t1", "teacherName": "Кузнецова М.А.", "status": "normal", "materialId": "m2"},
        {"id": "l3", "day": 0, "slot": 2, "time": "09:25–10:10", "subject": "Математика", "className": "7А", "room": "302", "teacherId": "t6", "teacherName": "Фролова Н.С.", "status": "normal", "materialId": "m3"},
        {"id": "l4", "day": 0, "slot": 3, "time": "10:30–11:15", "subject": "Биология", "className": "9А", "room": "301", "teacherId": "t3", "teacherName": "Волков А.И.", "status": "normal", "materialId": "m2"},
        {"id": "l5", "day": 0, "slot": 3, "time": "10:30–11:15", "subject": "География", "className": "8А", "room": "305", "teacherId": "t5", "teacherName": "Лебедев Д.О.", "status": "normal", "materialId": "m4"},
        {"id": "l6", "day": 0, "slot": 4, "time": "11:25–12:10", "subject": "Биология", "className": "7А", "room": "205", "teacherId": "t1", "teacherName": "Кузнецова М.А.", "status": "normal", "materialId": "m2"},
        {"id": "l7", "day": 1, "slot": 1, "time": "08:30–09:15", "subject": "Математика", "className": "7А", "room": "302", "teacherId": "t6", "teacherName": "Фролова Н.С.", "status": "normal", "materialId": "m3"},
        {"id": "l8", "day": 1, "slot": 2, "time": "09:25–10:10", "subject": "Биология", "className": "7А", "room": "205", "teacherId": "t1", "teacherName": "Кузнецова М.А.", "status": "normal", "materialId": "m1"},
        {"id": "l9", "day": 2, "slot": 2, "time": "09:25–10:10", "subject": "Биология", "className": "7А", "room": "205", "teacherId": "t1", "teacherName": "Кузнецова М.А.", "status": "normal", "materialId": "m2"},
        {"id": "l10", "day": 3, "slot": 2, "time": "09:25–10:10", "subject": "География", "className": "7А", "room": "305", "teacherId": "t5", "teacherName": "Лебедев Д.О.", "status": "normal", "materialId": "m4"},
        {"id": "l11", "day": 4, "slot": 1, "time": "08:30–09:15", "subject": "Математика", "className": "7А", "room": "302", "teacherId": "t6", "teacherName": "Фролова Н.С.", "status": "normal", "materialId": "m3"},
    ]
    students = [
        {"id": "s1", "name": "Алексей Соколов", "className": "7А", "attendance": 96, "average": 4.6, "progress": 84},
        {"id": "s2", "name": "Дарья Новикова", "className": "7А", "attendance": 98, "average": 4.8, "progress": 91},
        {"id": "s3", "name": "Михаил Белов", "className": "9А", "attendance": 94, "average": 4.2, "progress": 76},
        {"id": "s4", "name": "София Васильева", "className": "8А", "attendance": 97, "average": 4.7, "progress": 88},
    ]
    tasks = [
        {"id": "task1", "title": "Согласовать замену по биологии", "description": "7А · третий урок. Выберите свободного педагога в разделе замен.", "category": "Расписание", "status": "todo", "due": "Сегодня, 10:30", "owner": "Администратор", "ownerId": "ad1", "roles": ["admin", "director"], "replacementId": "r1"},
        {"id": "task2", "title": "Проверить самостоятельные работы", "description": "Ответы учеников 7А ждут обратной связи.", "category": "Обучение", "status": "progress", "due": "Сегодня, 16:00", "owner": "Кузнецова М.А.", "ownerId": "t1", "roles": ["teacher", "director", "admin"]},
        {"id": "task3", "title": "Утвердить план методической недели", "description": "Учебный документ подготовлен и ожидает согласования.", "category": "Документы", "status": "todo", "due": "Завтра, 12:00", "owner": "Директор", "ownerId": "d1", "roles": ["director", "admin"]},
    ]
    documents = [
        {"id": "doc1", "title": "План методической недели", "type": "Учебный план", "author": "Методический совет", "date": "08.10.2026", "status": "review", "content": "Демонстрационный учебный план.\n\nПонедельник: открытый урок по биологии.\nВторник: встреча предметных групп.\nСреда: мастерская обратной связи.\nЧетверг: обмен учебными материалами.\nПятница: подведение итогов.\n\nВсе записи синтетические.", "roles": ["director", "admin", "teacher", "accountant"]},
        {"id": "doc2", "title": "Порядок проведения замен", "type": "Регламент", "author": "Учебная часть", "date": "07.10.2026", "status": "approved", "content": "Демонстрационный регламент.\n\n1. Педагог сообщает причину отсутствия.\n2. Администратор проверяет предмет, занятость и кабинет.\n3. Директор или администратор согласует замену.\n4. Назначенный педагог отмечает проведение урока.\n\nДокумент относится только к локальному демо.", "roles": ["director", "admin", "teacher", "accountant"]},
        {"id": "doc3", "title": "План внеурочного чтения · 8А", "type": "План занятия", "author": "Учебная часть", "date": "06.10.2026", "status": "draft", "content": "Демонстрационный план занятия: выбрать рассказ, подготовить пять вопросов, обсудить главную мысль в группе.", "roles": ["director", "admin", "teacher"]},
    ]
    assignments = [
        {"id": "a1", "subject": "Биология", "title": "Клетка и её органоиды", "description": "Объясните, какую роль выполняют ядро и митохондрии. Приведите один пример.", "due": "Сегодня, 18:00", "status": "todo", "answer": "", "feedback": "", "grade": None, "studentId": "s1", "teacherId": "t1", "className": "7А"},
        {"id": "a2", "subject": "География", "title": "Климатические пояса", "description": "Назовите два признака умеренного климата.", "due": "Завтра, 18:00", "status": "todo", "answer": "", "feedback": "", "grade": None, "studentId": "s1", "teacherId": "t5", "className": "7А"},
        {"id": "a3", "subject": "Биология", "title": "Основные группы растений", "description": "Сравните мхи и папоротники.", "due": "Сегодня, 18:00", "status": "submitted", "answer": "Папоротники имеют развитые корни; мхи закрепляются ризоидами.", "feedback": "", "grade": None, "studentId": "s2", "teacherId": "t1", "className": "7А"},
        {"id": "a4", "subject": "Биология", "title": "Правила работы с микроскопом", "description": "Перечислите три правила работы.", "due": "07.10.2026", "status": "reviewed", "answer": "Начать с малого увеличения, настроить освещение, аккуратно перемещать препарат.", "feedback": "Все основные правила названы. Хорошая работа.", "grade": 5, "studentId": "s1", "teacherId": "t1", "className": "7А"},
    ]
    materials = [
        {"id": "m1", "title": "Многообразие растений", "subject": "Биология", "type": "Конспект", "content": "Растения образуют органические вещества в процессе фотосинтеза. Мхи, папоротники, голосеменные и покрытосеменные различаются строением и способами размножения.\n\nПрактика: сравните способы размножения двух групп и заполните таблицу.", "classNames": ["7А"], "teacherId": "t1"},
        {"id": "m2", "title": "Строение клетки", "subject": "Биология", "type": "Учебный материал", "content": "Клетка — основная единица живого организма. Ядро хранит наследственную информацию. Митохондрии участвуют в получении энергии. Мембрана регулирует обмен веществ.\n\nПроверьте себя: почему органоиды клетки работают совместно?", "classNames": ["7А", "8А", "9А"], "teacherId": "t1"},
        {"id": "m3", "title": "Линейные уравнения", "subject": "Математика", "type": "Практикум", "content": "Чтобы решить линейное уравнение, перенесите слагаемые с неизвестной в одну часть, остальные — в другую. При переносе меняйте знак.\n\nПример: 3x + 6 = 18; 3x = 12; x = 4.", "classNames": ["7А", "8А"], "teacherId": "t6"},
        {"id": "m4", "title": "Климатические пояса", "subject": "География", "type": "Конспект", "content": "Климат определяется температурой, осадками и сезонностью. В умеренном поясе выражена смена времён года.\n\nЗадание: опишите климат своей местности без персональных данных.", "classNames": ["7А", "8А"], "teacherId": "t5"},
    ]
    return deepcopy({
        "school": {"name": "Лицей — Инженерный центр · демо", "students": 645, "classes": 26, "teachers": 47},
        "schedule": schedule, "teachers": teachers, "students": students, "tasks": tasks, "documents": documents,
        "notifications": [
            {"id": "n1", "title": "Нужна замена по биологии", "text": "7А · 10:30. Один свободный кандидат найден.", "time": "09:10", "read": False, "readBy": [], "roles": ["director", "admin", "teacher"], "userIds": ["d1", "ad1", "t1"]},
            {"id": "n2", "title": "План готов к согласованию", "text": "План методической недели ожидает решения.", "time": "08:45", "read": False, "readBy": [], "roles": ["director", "admin", "accountant"]},
            {"id": "n3", "title": "Новое задание по биологии", "text": "Ответьте на вопросы о строении клетки до 18:00.", "time": "08:30", "read": False, "readBy": [], "roles": ["student"], "userIds": ["s1"]},
        ],
        "history": [{"id": "h1", "title": "Запрошена замена", "detail": "Биология · 7А · Кузнецова М.А.", "time": "09:10", "actor": "Кузнецова М.А.", "roles": ["director", "admin", "teacher"], "userIds": ["d1", "ad1", "t1"]}],
        "replacements": [{"id": "r1", "lessonId": "l1", "absentTeacherId": "t1", "teacherId": None, "reason": "Болезнь преподавателя", "status": "pending", "candidates": ["t2", "t3", "t4"]}],
        "assignments": assignments, "materials": materials,
    })
