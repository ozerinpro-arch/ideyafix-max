"""Fictional profile catalog; native authority is independently checked by the core."""
PROFILES=[
    dict(id=710001,name='Лера Демо',role='mentor',platformRole='teacher',label='Преподаватель'),
    dict(id=710002,name='Тимур Демо',role='learner',platformRole='student',label='Ученик'),
    dict(id=710003,name='Марина Демо',role='mentor',platformRole='teacher',label='Преподаватель'),
    dict(id=710004,name='Саша Демо',role='learner',platformRole='student',label='Ученик'),
    dict(id=710005,name='Роман Демо',role='organizer',platformRole='director',label='Директор'),
    dict(id=710006,name='Алекс Демо',role='planner',platformRole='planner',label='Завуч'),
    dict(id=710007,name='Славик Демо',role='learner',platformRole='student',label='Личный профиль ученика'),
    dict(id=710008,name='Администратор Идеяфикс',role='platform_admin',platformRole='platform_admin',label='Администратор платформы'),
    dict(id=710009,name='Доступ Демо',role='access_admin',platformRole='access_admin',label='Администратор доступа'),
]
BY_ID={p['id']:p for p in PROFILES}
ALIASES=dict(лера=710001,тимур=710002,марина=710003,саша=710004,роман=710005,завуч=710006,алекс=710006,славик=710007,администратор=710008,доступ=710009)
