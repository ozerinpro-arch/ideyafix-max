"""Russian conversation adapter over the independent native platform RPC.

Native schemas, authorization, revisions and domain decisions stay authoritative.
No original school runtime, HTTP listener, credentials or domain storage is used.
"""
from copy import deepcopy
import base64
from datetime import datetime,timezone
import hashlib
import json
import math
from pathlib import Path
import re
import time
from urllib.parse import quote, urlencode, urlsplit, parse_qs


NAMESPACES = {'schedule', 'calendar', 'preferences', 'requests', 'replacements',
              'publish', 'analytics', 'admin', 'exchange', 'restore', 'makeups',
              'outcomes', 'notifications'}
TITLES = dict(schedule='Расписание и расчёты', calendar='Календарь', preferences='Предпочтения',
              requests='Обращения и отсутствие', replacements='Замены', publish='Публикации',
              analytics='Аналитика', admin='Доступ сотрудников', exchange='Обмен данными',
              restore='История и восстановление версии', makeups='Отработки', outcomes='Итоги замен',
              notifications='Уведомления')
WORDS = {
 'id':'Идентификатор', 'identity':'Запись', 'identifier':'Запись', 'kind':'Вид',
 'name':'Название', 'title':'Заголовок', 'text':'Текст', 'note':'Примечание',
 'reason':'Причина', 'date':'Дата', 'startDate':'Дата начала', 'endDate':'Дата окончания',
 'weekStart':'Начало недели', 'periodStart':'Начало периода', 'periodEnd':'Конец периода',
 'startTime':'Время начала', 'endTime':'Время окончания', 'timezone':'Часовой пояс',
 'expectedRevision':'Ожидаемая версия', 'expectedDraftRevision':'Версия черновика',
 'expectedActiveVersion':'Версия публикации', 'expectedPublicationVersion':'Версия публикации',
 'requestId':'Номер команды', 'previewHash':'Подтверждение предпросмотра',
 'proposalHash':'Подтверждение предложения', 'teacherId':'Преподаватель',
 'teacherRef':'Преподаватель', 'replacementTeacher':'Заменяющий преподаватель',
 'lessonId':'Занятие', 'lessonIds':'Занятия', 'replacementId':'Замена',
 'publishedVersion':'Версия публикации', 'target':'Выбранное занятие',
 'recordHash':'Подтверждение записи', 'eventHash':'Подтверждение события',
 'decision':'Решение', 'confirmed':'Подтверждено', 'source':'Источник сведений',
 'schoolDays':'Учебные дни', 'weekdays':'Дни недели (0 — понедельник)',
 'config':'Настройки', 'record':'Запись', 'records':'Записи', 'input':'Параметры',
 'status':'Состояние', 'revision':'Версия', 'items':'Список', 'blockers':'Препятствия',
 'message':'Сообщение', 'notice':'Пояснение', 'ready':'Готово', 'lessons':'Занятия',
 'teachers':'Преподаватели', 'groups':'Группы', 'rooms':'Аудитории',
 'className':'Класс', 'subject':'Предмет', 'slot':'Номер занятия', 'day':'День',
 'start':'Начало', 'end':'Окончание', 'members':'Сотрудники', 'history':'История',
 'runId':'Расчёт', 'experiment_id':'План', 'experimentId':'План',
 'member_id':'Сотрудник', 'memberId':'Сотрудник', 'approval_id':'Согласование',
 'respondentMembershipId':'Адресат вопроса', 'schoolRequestId':'Обращение',
 'expectedSchoolRequestRevision':'Версия обращения', 'action':'Действие',
 'recordId':'Запись', 'record_id':'Запись', 'replacesId':'Заменяет запись',
 'preference':'Предпочтение', 'occurrences':'События по датам', 'content':'Содержимое',
 'format':'Формат', 'csv':'Таблица CSV', 'fileName':'Имя файла',
 'acknowledgeUnchecked':'Подтвердить ручную ответственность без проверки ограничений',
 'requiresTravel':'Учитывать переходы', 'bells':'Звонки', 'durationMinutes':'Минуты',
 'body':'Параметры', 'path':'Выбранные записи', 'query':'Параметры просмотра',
 'filename':'Имя файла', 'commandId':'Номер команды', 'attendancePercent':'Посещаемость, %',
 'averageGrade':'Средний балл', 'weeklyHours':'Часов в неделю', 'available':'Доступен',
 'due':'Срок', 'owner':'Ответственный', 'description':'Описание', 'type':'Тип',
 'schoolName':'Название демо', 'academicYear':'Учебный год', 'calendarTeacherIds':'Привязки к модели',
 'role':'Роль','username':'Логин тестового сотрудника','teacherRefs':'Преподаватели в расписании',
 'nominationId':'Назначение сотрудника','approval_id':'Согласованное решение','enabled':'Доступ включён',
 'eventId':'Занятие плана','otherEventId':'Второе занятие','slotId':'Время проведения','roomId':'Аудитория',
 'pinned':'Закреплено','operations':'Правки','acceptWorkingAssumptions':'Явно согласовать рабочие предположения',
 'useWorkingAssumptions':'Использовать рабочие предположения','expectedPackageHash':'Подтверждение источника',
 'expectedVersionHash':'Подтверждение версии','expectedRunHash':'Подтверждение расчёта',
 'expectedConfigRevision':'Версия нормативной модели','expectedLedgerRevision':'Версия журнала отработок',
 'ruleScope':'Область действия','background':'Выполнить в фоне','proposal':'Предложение',
 'targetExperimentId':'Экспериментальный план','expectedTurn':'Номер ответа','timeLimitSeconds':'Лимит расчёта, секунд',
 'teacher':'Преподаватель','teacherIds':'Преподаватели','groupIds':'Группы','roomIds':'Аудитории',
 'allowedSlotIds':'Допустимое время','sourceLessonIds':'Занятия исходной сетки','confirmed':'Факты подтверждены',
 'weeklyCount':'Занятий в неделю','maxPerDay':'Не более занятий в день','requiredTags':'Необходимое оснащение',
 'qualifications':'Квалификация','unavailableSlotIds':'Недоступное время','maxDailyMinutes':'Не более минут в день',
 'maxWeeklyMinutes':'Не более минут в неделю','preferences':'Предпочтения','baseline':'Исходное размещение',
 'subject':'Предмет','slots':'Время занятий','demands':'Учебная нагрузка','notes':'Примечания',
 'student':'Ученик','complete':'Полное расписание','publicationState':'Состояние публикации',
 'bindingStatus':'Подтверждение состава','bindingRevision':'Версия привязки','publicationVersion':'Версия публикации',
 'issues':'Замечания','edited':'Изменено вручную','raw':'Исходная запись','sourceCell':'Ячейка источника',
 'sourceName':'Название источника','updatedAt':'Обновлено','createdAt':'Создано',
 'deadlineAt':'Срок','nextAction':'Следующее действие','state':'Состояние','attempt':'Попытка',
 'validation':'Проверка ограничений','valid':'Ограничения соблюдены','diagnostics':'Результаты проверки',
 'mode':'Режим','coverage':'Охват исходных данных','assumptions':'Рабочие предположения',
 'expression':'Дата: сегодня, завтра или ГГГГ-ММ-ДД','messageTimestamp':'Исходное время сообщения с часовым поясом',
 'withinPeriod':'В календарном периоде','targetId':'Ресурс','schoolDays':'Учебные дни',
 'targetWeekStart':'Неделя для правки','respondentMembershipId':'Адресат вопроса',
 'resolutions':'Сопоставления исходных данных','classMap':'Сопоставление классов','groupMap':'Группы каждой ячейки',
 'teacherMap':'Сопоставление преподавателей','roomIds':'Допустимые аудитории','teacherLimits':'Нагрузка преподавателей',
 'teacherOverrides':'Уточнения преподавателей','countOverrides':'Уточнения часов','subjectOverrides':'Уточнения предметов',
 'sharedLessons':'Совместные занятия','excludedCells':'Исключённые ячейки','sourceConditions':'Подтверждение условий источника',
 'unallocatedRows':'Нераспределённые строки','expectedSourceRevision':'Версия сопоставлений','expectedRegistryRevision':'Версия реестра фактов',
}
VALUES = {'true':'Да', 'false':'Нет', 'null':'Не задано', 'approve':'Одобрить',
 'reject':'Отклонить', 'archive':'В архив', 'restore':'Восстановить',
 'pending':'Ожидает', 'approved':'Одобрено', 'rejected':'Отклонено',
 'unavailable':'Недоступность', 'wish':'Пожелание', 'conducted':'Проведено',
 'not_conducted':'Не проведено', 'cancelled':'Отменено', 'other':'Другое',
 'retract':'Отозвать', 'unavailability':'Отсутствие', 'add_lesson':'Добавить занятие',
 'change_lesson':'Изменить занятие', 'question':'Вопрос', 'add':'Добавить',
 'change':'Изменить', 'students':'Ученики', 'teacherProfiles':'Профили преподавателей',
 'tasks':'Задачи', 'documents':'Документы', 'assignments':'Задания',
 'materials':'Материалы', 'absences':'Отсутствия', 'notifications':'Уведомления',
 'lessons':'Занятия', 'planningConfiguration':'Модель планирования',
 'lessonProgress':'Учебный прогресс', 'weeks':'Недели', 'holidays':'Каникулы',
 'days':'Особые дни', 'exceptions':'Исключения', 'rules':'Правила', 'events':'События', 'draft':'Черновик', 'published':'Опубликовано',
 'teacher':'Преподаватель','student':'Ученик','director':'Директор','planner':'Составитель расписания',
 'access_admin':'Администратор доступа','text':'Текст','link':'Ссылка','generate':'Рассчитать заново','repair':'Исправить расписание'}
VALUES.update(move='Переместить занятие',swap='Поменять местами',room='Изменить аудиторию',
              pin='Закрепить занятие',metadata='Изменить предмет или преподавателей',
              delete='Отменить занятие',school='Учебный день',closed='Выходной',
              lesson='Правка занятия',planning='Рассчитанный план',once='Только сейчас',persistent='Постоянное правило')
VALUES.update(offline_preparation='Локальная подготовка',working_assumptions='Рабочие предположения',
              off='Отключено',available='Доступно',acknowledged='Прочтение подтверждено',
              submitted='Передано',completed='Завершено',queued='В очереди',running='Выполняется',
              blocked='Нужны дополнительные условия',stale='Требует обновления',expired='Срок истёк',
              failed='Не завершено',unknown='Результат неизвестен',optimal='Оптимальное решение',
              feasible='Допустимое решение',infeasible='Допустимого решения нет',candidate_ready='Предложение проверено')
VALUES.update(teacher_unavailable='Недоступность преподавателя',room_unavailable='Недоступность аудитории')


def _label(name):
    return WORDS.get(name, re.sub(r'([a-z])([A-Z])', r'\1 \2', name).replace('_', ' ').capitalize())


def _display(value):
    if value is True: return 'Да'
    if value is False: return 'Нет'
    if value is None: return 'Не задано'
    return VALUES.get(str(value), str(value))


def _record_label(row,number=1):
    for key in ('name','title','label'):
        if isinstance(row.get(key),str)and row[key]:return row[key]
    parts=[str(row[k])for k in ('className','subject','date','start','ownerName','originalTeacher','replacementTeacher')if row.get(k)]
    if isinstance(row.get('preference'),dict):
        value=row['preference'];parts += [_display(value['kind']),str(value.get('startDate',''))]
    if parts:return ' · '.join(parts)
    for key in ('message','text'):
        if isinstance(row.get(key),str)and row[key]:return row[key]
    if row.get('status'):return _display(row['status'])+' · запись '+str(number)
    return 'Запись '+str(number)


def _private_invitation(data):
    """Extract trusted native invitation capability and redact all human copies."""
    tokens=[]
    def find(value):
        if isinstance(value,dict):
            token=value.get('token')
            if isinstance(token,str)and re.fullmatch(r'[A-Za-z0-9_-]{16,256}',token):tokens.append(token)
            for key,item in value.items():
                if key in {'path','url'}and isinstance(item,str)and '#invite='in item:
                    query=parse_qs(urlsplit(item).fragment)
                    candidate=query.get('invite',[])
                    if len(candidate)==1 and re.fullmatch(r'[A-Za-z0-9_-]{16,256}',candidate[0]):tokens.append(candidate[0])
                find(item)
        elif isinstance(value,list):
            for item in value:find(item)
    find(data)
    tokens=list(dict.fromkeys(tokens))
    if len(tokens)>1:raise ValueError('native_invitation_ambiguous')
    def redact(value):
        if isinstance(value,dict):return {k:redact(v)for k,v in value.items()if k not in {'token','password','path','url'}}
        if isinstance(value,list):return [redact(v)for v in value]
        if isinstance(value,str)and ('#invite='in value or any(t in value for t in tokens)):return 'Приватное приглашение'
        return value
    return tokens[0]if tokens else None,redact(data)


def _human(value, depth=0, labels=None):
    if depth > 5: return 'Дополнительные сведения'
    if isinstance(value, dict):
        def shown(k,v):
            if k in {'deadlineAt','createdAt','updatedAt','expiresAt'}and isinstance(v,(int,float))and v>1_000_000_000:
                try:return datetime.fromtimestamp(v/1000 if v>100_000_000_000 else v,tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
                except (OverflowError,OSError,ValueError):return 'Некорректная дата — исправьте значение'
            return _human(v,depth+1,labels)
        return '\n'.join(f'{_label(k)}: {shown(k,v)}' for k, v in value.items()
                         if k not in {'csrfToken','password','token','sessionToken','requestId','commandId','hash',
                                      'actor','membershipId','userId','schoolId'}
                         and not k.startswith('_')and not k.startswith('expected')and not k.endswith('Hash'))
    if isinstance(value, list):
        return '\n'.join(f'{i+1}. {_human(v, depth+1,labels)}' for i, v in enumerate(value)) or 'Список пуст.'
    if isinstance(value,str)and labels and value in labels:return labels[value]
    return _display(value)


class ScheduleAssistant:
    def __init__(self, runtime, state_store=None):
        self.runtime = runtime
        if state_store is None:
            from .platform_chat import ChatState
            state_store = ChatState(Path(runtime.root) / 'conversations.sqlite3')
        self.state_store = state_store
        inventory = json.loads(Path(__file__).with_name('platform_schedule_inventory.json').read_text())
        self.actions = [a for a in inventory['actions'] if a['namespace'][1:] in NAMESPACES
                        and a.get('nativeEndpoint') and not a['implementationStatus'].startswith('excluded_')]
        self.actions += [{**a,'namespace':'/restore'}for a in self.actions if a['nativeEndpoint']in {
            '/api/timetable/history','/api/timetable/history/{revision}','/api/timetable/restore'}]
        self.spec = None

    def handle(self, actor_id, text, event_id, *, session_key=None):
        word, _, argument = text.strip().partition(' ')
        ns = word.lstrip('/').casefold()
        scope = f'schedule:{actor_id}'
        key = session_key or f'actor:{actor_id}'
        if not word.startswith('/') or ns not in NAMESPACES:
            if word.startswith('/') and word!='/cancel': return None
            current = self.state_store.peek(scope,key)
            if not current.get('activeNamespace'): return None
            ns = current['activeNamespace']
            argument = 'cancel' if text.strip()=='/cancel' else 'answer '+text
        return self.state_store.run(scope, key, event_id, text,
                                    lambda state: self._handle(state, actor_id, ns, argument.strip(), event_id))

    def handle_file(self,actor_id,document,event_id,*,session_key=None):
        scope=f'schedule:{actor_id}';key=session_key or f'actor:{actor_id}'
        try:
            if not isinstance(document,dict) or set(document)!={'name','contentType','base64'}:raise ValueError()
            if not isinstance(document['name'],str) or not document['name'].lower().endswith(('.xlsx','.csv','.json','.txt')) or any(c in document['name'] for c in '/\\\r\n\x00'):raise ValueError()
            raw=base64.b64decode(document['base64'],validate=True)
            if not 0<len(raw)<=2*1024*1024:raise ValueError()
        except (ValueError,TypeError):return self._reply('Пришлите CSV, XLSX, JSON или текст UTF-8 (.txt) размером до 2 МиБ. Изменения не выполнялись.')
        fingerprint='file:'+hashlib.sha256(json.dumps(document,sort_keys=True).encode()).hexdigest()
        def receive(state):
            target=state.get('form',{})
            if not target.get('pending'):return None
            field=target['pending'][0]
            if field['schema'].get('type')=='file':
                if not document['name'].lower().endswith('.xlsx'):return self._reply('Для расписания нужен именно XLSX. Изменения не выполнялись.')
                target['document']=document
            elif target['namespace']=='exchange'and field['path'][-1]=='content':
                fmt=target['body'].get('format')
                if not document['name'].lower().endswith('.'+str(fmt)):return self._reply('Формат вложения отличается от выбранного. Пришлите нужный файл.')
                try:content=document['base64']if fmt=='xlsx'else raw.decode('utf-8-sig')
                except UnicodeDecodeError:return self._reply('Нужен текстовый файл в UTF-8. Изменения не выполнялись.')
                self._set(target,field['path'],content)
            elif document['name'].lower().endswith('.txt') and field['schema'].get('type','string')=='string' and not any(k in field['schema'] for k in ('anyOf','oneOf','enum','const')):
                try:content=raw.decode('utf-8-sig')
                except UnicodeDecodeError:return self._reply('Нужен текстовый файл в UTF-8. Изменения не выполнялись.')
                if any((ord(c)<32 and c not in '\r\n\t') or 0x7f<=ord(c)<=0x9f or 0xd800<=ord(c)<=0xdfff for c in content):return self._reply('Текст содержит недопустимые управляющие символы. Изменения не выполнялись.')
                return self._handle(state,actor_id,target['namespace'],'answer '+content,event_id)
            else:return None
            target['pending'].pop(0)
            return self._next(state,actor_id,target['namespace'],event_id)
        return self.state_store.run(scope,key,event_id,fingerprint,receive)

    def _schema(self, value):
        value = deepcopy(value)
        seen = set()
        while '$ref' in value:
            ref = value['$ref']
            if ref in seen: raise ValueError('recursive_input_schema')
            seen.add(ref)
            target = self.spec
            for part in ref.removeprefix('#/').split('/'): target = target[part]
            value = {**deepcopy(target), **{k:v for k,v in value.items() if k != '$ref'}}
        patterns=value.get('patternProperties')
        if isinstance(patterns,dict)and len(patterns)==1:
            pattern,item=next(iter(patterns.items()))
            value.setdefault('additionalProperties',item)
            value.setdefault('propertyNames',{'pattern':pattern})
        return value

    def _record_models(self,form):
        route=form['action']['nativeEndpoint']
        prefix='/api/school/calendar' if route.startswith('/api/school/calendar/records') else '/api/school/records' if route.startswith('/api/school/records') else None
        return self.spec.get('x-record-models',{}).get(prefix,{}) if prefix else {}

    def _local_schema(self,model):
        definitions=model.get('$defs',{})
        def expand(value,seen=()):
            if isinstance(value,list):return [expand(v,seen)for v in value]
            if not isinstance(value,dict):return value
            if '$ref' in value and value['$ref'].startswith('#/$defs/'):
                name=value['$ref'].split('/')[-1]
                if name in seen:raise ValueError('recursive_record_model')
                return expand(definitions[name],seen+(name,))
            return {k:expand(v,seen)for k,v in value.items() if k!='$defs'}
        return expand(model)

    def _reply(self, text, buttons=(), document=None):
        result = {'text':text[:3900], 'buttons':list(buttons)}
        if document is not None: result['document'] = document
        return result

    def _button(self, label, ns, command):
        return {'label':label[:80], 'command':f'/{ns} {command}'.strip()}

    def _options(self,ns,labels,page=0,*,objects=False,keys=False):
        if page*8>=len(labels):page=0
        prefix='/keychoice 'if keys else'/object 'if objects else'/choice '
        buttons=[self._button(label,ns,'answer '+prefix+str(i+1))for i,label in enumerate(labels)if page*8<=i<(page+1)*8]
        kind='keys'if keys else'objects'if objects else'values'
        if (page+1)*8<len(labels):buttons.append(self._button('Следующие варианты',ns,f'options {kind} {page+1}'))
        if page:buttons.append(self._button('Предыдущие варианты',ns,f'options {kind} {page-1}'))
        return buttons

    def _title(self, action):
        route = action['nativeEndpoint']
        if route=='/api/student/schedule':return 'Моё опубликованное расписание'
        suffix = route.rsplit('/', 1)[-1]
        operations = {'preview':'Предпросмотр', 'apply':'Применить', 'publish':'Опубликовать',
            'publish-preview':'Проверить публикацию', 'decision':'Принять решение',
            'question':'Задать вопрос', 'reply':'Ответить', 'proposal':'Предложить изменение',
            'config':'Настройки', 'candidates':'Кандидаты на замену', 'prepare':'Подготовить',
            'acknowledge':'Подтвердить прочтение', 'export':'Выгрузить', 'import':'Загрузить',
            'analyze-headers':'Разобрать заголовки таблицы', 'catalog':'Каталог обмена',
            'effective':'Календарь недели', 'resolve-date':'Уточнить дату', 'version':'Версия',
            'week-preview':'Проверить неделю', 'cancel':'Отменить', 'propose':'Предложить',
            'adjust':'Уточнить', 'answer':'Ответить', 'readiness':'Готовность',
            'status':'Состояние', 'history':'История', 'template':'Шаблон',
            'approval':'Согласовать', 'revoke':'Отозвать доступ', 'approve-revoke':'Согласовать отзыв',
            'execute':'Выполнить отзыв', 'directory':'Справочник', 'nominations':'Назначить роль',
            'invites':'Пригласить', 'my-schedule':'Моё расписание', 'absences':'Отсутствия',
            'my-absence':'Сообщить об отсутствии'}
        family=action['source'].split('/')[-1].split('.')[0]
        families={'planning':'Расчёт','schedule_agent':'Помощник расписания','timetable':'Редактор расписания',
            'dated_timetable':'Расписание по датам','experimental_school':'Экспериментальный план',
            'experimental_edit_routes':'Версионный редактор','experimental_publications':'Согласование плана',
            'school_ops':'Данные демо','finance':'Учебные счета','launch_runtime':'Задачи запуска'}
        if suffix in operations: return (families.get(family,'')+' · ' if family in families else '')+operations[suffix]
        object_name = route.split('/')[-2] if suffix.startswith('{') else suffix
        nouns = {'runs':'Расчёты', 'questions':'Вопросы', 'tasks':'Задачи расчёта',
            'experimental-school':'Экспериментальные планы', 'school-source-registry':'Источники плана',
            'sources':'Исходные планы', 'records':'Записи', 'dated-draft':'Черновик по датам',
            'workspace':'Данные демо', 'invoices':'Учебные счета', 'finance':'Учебные счета', 'settings':'Настройки демо',
            'pdf':'Документ счёта'}
        noun = nouns.get(object_name, TITLES.get(action['namespace'][1:], 'Записи'))
        if action['method']=='GET': return ('Открыть ' if suffix.startswith('{') else 'Посмотреть ') + noun.lower()
        return ('Изменить ' if action['method'] in {'PATCH','PUT'} else 'Создать ') + noun.lower()

    def _handle(self, state, actor, ns, argument, event):
        state['activeNamespace'] = ns
        if argument.startswith('result '):
            try:
                index=int(argument[7:]);pages=state.get('resultPages',[])
                if not 1<=index<=len(pages):raise ValueError()
            except (ValueError,IndexError):return self._reply('Выберите страницу результата кнопкой.')
            reply=self._reply(pages[index-1],self._pages(ns,index,len(pages)))
            if state.get('resultGuards'):reply['resourceGuards']=deepcopy(state['resultGuards'])
            return reply
        if self.spec is None: self.spec = self.runtime.schema()
        actions = [a for a in self.actions if a['namespace']=='/'+ns]
        if not argument or argument == 'menu' or argument.startswith('menu '):
            state.pop('form', None)
            profiles=self.runtime.profiles()if hasattr(self.runtime,'profiles')else self.spec.get('x-platform-profiles',[])
            role=next((p.get('role')for p in profiles if p.get('id')==actor),None)
            visible=list(enumerate(actions))
            if ns=='schedule'and role=='student':visible=[(i,a)for i,a in visible if a['nativeEndpoint']=='/api/student/schedule']
            elif ns=='schedule'and role=='teacher':visible=[(i,a)for i,a in visible if
                a['method']=='GET'and a['nativeEndpoint']in {'/api/staff/my-schedule','/api/timetable','/api/launch/tasks','/api/launch/tasks/{task_id}'}
                or a['nativeEndpoint']=='/api/launch/questions/{question_id}/answer']
            elif ns=='requests'and role=='teacher':visible=[(i,a)for i,a in visible if a['nativeEndpoint']=='/api/staff/my-absence'or
                a['nativeEndpoint'].startswith('/api/school/requests')and(a['method']=='GET'or a['nativeEndpoint']=='/api/school/requests'or a['nativeEndpoint'].endswith('/reply'))]
            elif ns=='calendar'and role=='teacher':visible=[(i,a)for i,a in visible if a['method']=='GET'or a['nativeEndpoint'].endswith('/resolve-date')]
            elif ns=='preferences'and role in {'teacher','director','planner'}:
                prefix='/api/teacher/preferences'if role=='teacher'else'/api/school/teacher-preferences'
                visible=[(i,a)for i,a in visible if a['nativeEndpoint'].startswith(prefix)]
            elif ns=='outcomes'and role in {'teacher','director','planner'}:
                prefix='/api/teacher/replacement-outcomes'if role=='teacher'else'/api/school/replacement-outcomes'
                visible=[(i,a)for i,a in visible if a['nativeEndpoint'].startswith(prefix)]
            elif ns=='notifications'and role in {'teacher','student','director','planner'}:
                prefix='/api/staff/schedule-notifications'if role in {'teacher','student'}else'/api/school/schedule-notifications'
                visible=[(i,a)for i,a in visible if a['nativeEndpoint'].startswith(prefix)]
            elif ns=='admin'and role is not None:
                if role=='platform_admin':visible=[(i,a)for i,a in visible if a['nativeEndpoint'].startswith('/api/school/')]
                elif role=='director':visible=[(i,a)for i,a in visible if a['nativeEndpoint'].startswith('/api/staff/')]
                elif role=='access_admin':visible=[(i,a)for i,a in visible if a['nativeEndpoint']in {'/api/staff/directory','/api/staff/invites','/api/staff/revocations/{approval_id}/execute'}]
                else:visible=[]
            if role=='student'and ns not in {'schedule','notifications'}:visible=[]
            if role=='teacher'and ns in {'publish','makeups','analytics','exchange','restore','replacements'}:visible=[]
            if ns=='analytics'and role=='planner':visible=[(i,a)for i,a in visible if a['nativeEndpoint']=='/api/school/analytics/schedule']
            try:page=max(0,int(argument[5:])) if argument.startswith('menu ') else 0
            except ValueError:page=0
            if page*8>=len(visible):page=0
            buttons = [self._button(self._title(a), ns, 'action '+str(i+1)) for i,a in visible[page*8:(page+1)*8]]
            if (page+1)*8<len(visible):buttons.append(self._button('Следующие действия',ns,'menu '+str(page+1)))
            if page:buttons.append(self._button('Предыдущие действия',ns,'menu '+str(page-1)))
            if ns == 'schedule':
                sections=['notifications']if role=='student'else['preferences','requests','outcomes','notifications']if role=='teacher'else['publish','makeups','outcomes','notifications']
                buttons += [self._button(TITLES[n], n, '') for n in sections]
            return self._reply(TITLES[ns]+('. Выберите действие. Записи и права проверяет родной сервис.'if visible else '. Для текущей роли действия этого раздела недоступны.'),buttons)
        if argument == 'cancel':
            state.pop('form', None)
            return self._reply('Ввод отменён. Изменения не отправлялись.', [self._button('Вернуться',ns,'')])
        if argument=='continue':
            continuation=state.get('continuation')
            if not continuation or continuation['namespace']!=ns:return self._reply('Нет проверенного результата для продолжения. Выполните предпросмотр.')
            form=deepcopy(continuation);action=form['action']
            form['nativeEvent']='schedule-'+hashlib.sha256(f'{actor}:{event}:{action["id"]}'.encode()).hexdigest()
            op=self.spec['paths'][action['nativeEndpoint']][action['method'].lower()]
            schema=self._schema(op.get('requestBody',{}).get('content',{}).get('application/json',{}).get('schema',{}))
            form['pending']=[];form['ready']=False
            for name,field_schema in schema.get('properties',{}).items():
                if name in {'requestId','commandId'}and name not in form['body']:form['body'][name]=hashlib.sha256(form['nativeEvent'].encode()).hexdigest()[:32]
                elif name not in form['body']and name in schema.get('required',[]):
                    form['pending'].append({'path':['body',name],'schema':field_schema,'required':True})
            state['form']=form
            return self._next(state,actor,ns,event)
        if argument.startswith('action '):
            state.pop('continuation',None)
            try:
                index=int(argument[7:])
                if not 1<=index<=len(actions):raise ValueError()
                action = actions[index-1]
            except (ValueError,IndexError): return self._reply('Выберите действие кнопкой из меню.')
            op = self.spec.get('paths',{}).get(action['nativeEndpoint'],{}).get(action['method'].lower())
            if op is None:
                return self._reply('Этот исходный маршрут ещё не доступен в независимом сервисе. Действие не выполнено.')
            form = {'action':action, 'body':{}, 'path':{}, 'query':{}, 'pending':[],
                    'choices':[], 'namespace':ns,
                    'nativeEvent':'schedule-'+hashlib.sha256(f'{actor}:{event}:{action["id"]}'.encode()).hexdigest()}
            if action['nativeEndpoint'] in {'/api/timetable/preview','/api/timetable/import'}:
                form['pending'].append({'path':['document'],'schema':{'type':'file'},'required':True})
            if action['method']!='GET'or any(p.get('in')in {'path','query'}for p in op.get('parameters',[])):
                baselines=self._baselines(action,actor)
                for path in baselines:
                    value=self.runtime.request(actor,'GET',path)
                    if value.get('status')==200:
                        form.setdefault('baselines',{})[path]=value['data']
                self._remember(state,form.get('baselines',{}))
            for parameter in op.get('parameters',[]):
                location = parameter.get('in')
                if location in {'path','query'}:
                    form['pending'].append({'path':[location,parameter['name']],
                        'schema':parameter.get('schema',{}),'required':parameter.get('required',False)})
            body = op.get('requestBody',{}).get('content',{}).get('application/json',{}).get('schema')
            if body: form['pending'].append({'path':['body'],'schema':body,'required':True})
            state['form'] = form
            return self._next(state,actor,ns,event)
        form = state.get('form')
        if not form or form['namespace'] != ns:
            return self._reply('Сначала выберите действие.', [self._button('Открыть меню',ns,'')])
        if argument.startswith('options '):
            try:
                _,kind,page=argument.split();page=int(page)
                if page<0 or not form['pending']:raise ValueError()
                labels=form['pending'][0].get('keyLabels',[])if kind=='keys'else form['pending'][0].get('objectLabels',[])if kind=='objects'else form.get('choiceLabels',[])
                if not labels:raise ValueError()
            except (ValueError,IndexError):return self._reply('Выберите вариант текущего поля.')
            return self._reply('Варианты текущего поля.',self._options(ns,labels,page,objects=kind=='objects',keys=kind=='keys')+[self._button('Отменить',ns,'cancel')])
        if argument=='change'or argument.startswith('change '):
            if form.get('uncertain'):return self._reply('Результат прежней операции неизвестен. Исправление и новая отправка отключены. Сначала проверьте записи раздела.',[self._button('Открыть раздел',ns,'')])
            paths=[];buttons=[]
            for encoded,schema in form.get('fieldSchemas',{}).items():
                path=json.loads(encoded);name=str(path[-1])
                if path[0]not in {'body','path','query'} or name.startswith('expected') or name.endswith('Hash')or name in {'requestId','commandId'}:continue
                try:value=self._get(form,path)
                except (KeyError,IndexError,TypeError):continue
                if isinstance(value,dict):continue
                shown=self._labels(state).get(value,_display(value))if isinstance(value,str)else _display(value)
                paths.append(path);buttons.append(self._button(' → '.join(_label(str(p))for p in path[1:])+' · '+shown[:35],ns,'edit '+str(len(paths))))
            form['editPaths']=paths
            try:page=max(0,int(argument[7:]))if argument.startswith('change ')else 0
            except ValueError:page=0
            if page*8>=len(buttons):page=0
            visible=buttons[page*8:(page+1)*8]
            if (page+1)*8<len(buttons):visible.append(self._button('Следующие поля',ns,'change '+str(page+1)))
            if page:visible.append(self._button('Предыдущие поля',ns,'change '+str(page-1)))
            return self._reply('Какое поле исправить? Остальной ввод сохранён.',visible+[self._button('Отменить действие',ns,'cancel')])
        if argument.startswith('edit '):
            try:
                index=int(argument[5:]);paths=form.get('editPaths',[])
                if not 1<=index<=len(paths):raise ValueError()
                path=paths[index-1];schema=form['fieldSchemas'][json.dumps(path)]
            except (ValueError,KeyError,IndexError):return self._reply('Выберите поле кнопкой.')
            form['pending']=[{'path':path,'schema':schema,'required':True}];form['ready']=False
            form['nativeEvent']='schedule-'+hashlib.sha256(f'{actor}:{event}:{form["action"]["id"]}'.encode()).hexdigest()
            self._set(form,path,None)
            return self._next(state,actor,ns,event)
        if argument == 'confirm':
            if form.get('pending') or not form.get('ready'):
                return self._reply('Сначала заполните параметры.')
            return self._execute(state, actor, ns, event)
        if not argument.startswith('answer '):
            return self._reply('Отправьте ответ после команды '+f'/{ns} answer '+
                               'или выберите кнопку. Для отмены — '+f'/{ns} cancel')
        if not form['pending']: return self._reply('Параметры уже заполнены. Подтвердите действие.')
        field = form['pending'][0]
        answer = argument[7:]
        schema = self._schema(field['schema'])
        if schema.get('type')=='file':return self._reply('Прикрепите файл XLSX размером до 2 МиБ. Текст не заменяет файл.')
        if answer.startswith('/object '):
            try:
                index=int(answer[8:])
                if not 1<=index<=len(field.get('objectChoices',[])):raise ValueError()
                value=field['objectChoices'][index-1]
            except (ValueError,IndexError):return self._reply('Выберите объект кнопкой.')
            self._set(form,field['path'],value);form['pending'].pop(0)
            self._index_value(form,field['path'],value,schema)
            return self._next(state,actor,ns,event)
        if answer=='/manual' and field.get('objectChoices'):
            field['manual']=True
            return self._next(state,actor,ns,event)
        if answer == '/skip':
            if field['required']: return self._reply('Это обязательное поле.')
            form['pending'].pop(0)
        elif schema.get('type') == 'array':
            if answer == '/done':
                values = self._get(form,field['path'])
                if len(values) < schema.get('minItems',0): return self._reply('Добавьте необходимые элементы списка.')
                form['pending'].pop(0)
            elif answer == '/add':
                values = self._get(form,field['path'])
                if len(values) >= schema.get('maxItems',10000): return self._reply('Достигнут предел списка.')
                index=len(values); values.append(None)
                form['pending'].insert(0,{'path':field['path']+[index],'schema':schema.get('items',{}),'required':True})
            else: return self._reply('Добавьте элемент или завершите список кнопкой.')
        elif 'anyOf' in schema or 'oneOf' in schema:
            variants = schema.get('anyOf',schema.get('oneOf'))
            try:
                index=int(answer)
                if not 1<=index<=len(variants):raise ValueError()
                selected = variants[index-1]
            except (ValueError,IndexError): return self._reply('Выберите вариант кнопкой.')
            if selected.get('type')=='null':
                self._set(form,field['path'],None);form['pending'].pop(0)
            else: field['schema']=selected
        elif schema.get('type')=='object' and schema.get('additionalProperties'):
            if answer=='/done':form['pending'].pop(0)
            elif answer.startswith('/key ')or answer.startswith('/keychoice '):
                if answer.startswith('/keychoice '):
                    try:
                        index=int(answer[11:])
                        if not 1<=index<=len(field.get('keyChoices',[])):raise ValueError()
                        key=field['keyChoices'][index-1]
                    except (ValueError,IndexError):return self._reply('Выберите исходную запись кнопкой.')
                else:key=answer[5:].strip()
                if not 1<=len(key)<=500 or any(ord(c)<32 or 0x7f<=ord(c)<=0x9f or 0xd800<=ord(c)<=0xdfff for c in key):return self._reply('Укажите короткое название поля без управляющих символов.')
                pattern=schema.get('propertyNames',{}).get('pattern')
                if pattern and not re.search(pattern,key):return self._reply('Выберите допустимую исходную запись из списка.')
                values=self._get(form,field['path'])
                if key in values:return self._reply('Это поле уже добавлено.')
                if len(values)>=schema.get('maxProperties',10000):return self._reply('Достигнут предел полей.')
                values[key]=None
                item=schema['additionalProperties']
                form['pending'].insert(0,{'path':field['path']+[key],'schema':item if isinstance(item,dict) else {},'required':True})
            else:return self._reply('Введите название нового поля: '+f'/{ns} answer /key название. Либо завершите запись кнопкой.')
        else:
            try:
                if answer.startswith('/choice '):
                    index=int(answer[8:])
                    if not 1<=index<=len(form['choices']):raise ValueError()
                    value = form['choices'][index-1]
                elif schema.get('type')=='integer':
                    if field['path'][-1]=='deadlineAt'and not answer.isdigit():
                        relative=re.fullmatch(r'через ([1-9][0-9]{0,3}) (минуту|минуты|минут|час|часа|часов)',answer.casefold())
                        if relative:value=int(time.time()*1000)+int(relative[1])*(3600000 if relative[2].startswith('час')else 60000)
                        else:
                            stamp=datetime.fromisoformat(answer)
                            if stamp.tzinfo is None:raise ValueError()
                            value=int(stamp.timestamp()*1000)
                    else:value = int(answer)
                elif schema.get('type')=='number': value = float(answer)
                elif schema.get('type')=='boolean':
                    if answer not in {'true','false','да','нет'}: raise ValueError()
                    value=answer in {'true','да'}
                elif schema.get('type')=='null': value=None
                else: value=answer
                if 'enum' in schema and value not in schema['enum']: raise ValueError()
                if 'const'in schema and value!=schema['const']:raise ValueError()
                if isinstance(value,str) and not schema.get('minLength',0)<=len(value)<=schema.get('maxLength',24000): raise ValueError()
                if isinstance(value,(int,float)) and not isinstance(value,bool) and not schema.get('minimum',float('-inf'))<=value<=schema.get('maximum',float('inf')): raise ValueError()
            except (ValueError,IndexError,TypeError): return self._reply('Значение не подходит. Проверьте тип, длину и допустимые варианты.')
            self._set(form,field['path'],value);form['pending'].pop(0)
        return self._next(state,actor,ns,event)

    def _get(self, form, path):
        value=form
        for part in path: value=value[part]
        return value

    def _set(self, form, path, value):
        target=form
        for part in path[:-1]: target=target[part]
        target[path[-1]]=value

    def _index_value(self,form,path,value,schema):
        schema=self._schema(schema)
        if 'anyOf'in schema or 'oneOf'in schema:
            typ='object'if isinstance(value,dict)else'array'if isinstance(value,list)else'boolean'if isinstance(value,bool)else'integer'if isinstance(value,int)else'null'if value is None else'string'
            variants=schema.get('anyOf',schema.get('oneOf'))
            schema=next((self._schema(v)for v in variants if self._schema(v).get('type')==typ),schema)
        form.setdefault('fieldSchemas',{})[json.dumps(path)]=schema
        if isinstance(value,dict):
            for key,item in value.items():
                child=schema.get('properties',{}).get(key,schema.get('additionalProperties'))
                if not isinstance(child,dict):continue
                if child is not None:self._index_value(form,path+[key],item,child)
        elif isinstance(value,list):
            for index,item in enumerate(value):self._index_value(form,path+[index],item,schema.get('items',{}))

    def _baselines(self,action,actor):
        route=action['nativeEndpoint'];ns=action['namespace']
        profiles=self.runtime.profiles()if hasattr(self.runtime,'profiles')else self.spec.get('x-platform-profiles',[])
        role=next((p.get('role')for p in profiles if p.get('id')==actor),
                  'teacher'if actor in {710001,710003} else 'student'if actor in {710002,710004} else 'director')
        if action['source'].startswith('server/core.py'):return ['/api/state']
        if route.startswith('/api/launch'):return ['/api/launch/tasks','/api/launch/respondents','/api/launch/outbox']
        if route=='/api/planning/school-source-registry':return ['/api/planning/school-source-registry','/api/timetable','/api/planning/config','/api/planning/sources']
        if route.startswith('/api/finance'):return ['/api/finance']
        if route.startswith('/api/school/records') or route.endswith('/settings'):return ['/api/school/workspace']
        if ns=='/admin':
            paths=['/api/staff/directory','/api/timetable','/api/planning/config','/api/school/publications']
            if route.startswith('/api/staff/student-'):paths.insert(0,'/api/staff/student-access')
            return paths
        if ns=='/calendar':return ['/api/school/calendar','/api/planning/config']
        if ns=='/preferences':return ['/api/teacher/preferences' if role=='teacher' else '/api/school/teacher-preferences']
        if ns=='/requests':return ['/api/school/requests','/api/timetable','/api/launch/respondents']
        if ns=='/replacements':return ['/api/school/replacements','/api/school/requests']
        if ns=='/notifications':return ['/api/staff/schedule-notifications' if role in {'teacher','student'} else '/api/school/schedule-notifications']
        if ns=='/outcomes':return ['/api/teacher/replacement-outcomes' if role=='teacher' else '/api/school/replacement-outcomes']
        if ns=='/exchange':return ['/api/school/data-exchange/catalog','/api/timetable','/api/planning/config']
        if ns=='/restore':return ['/api/timetable','/api/timetable/history']
        if ns in {'/schedule','/publish','/makeups','/restore'}:
            paths=['/api/planning/config','/api/timetable','/api/planning/runs','/api/school/publications','/api/planning/experimental-school']
            if route.startswith('/api/timetable'):paths.insert(0,paths.pop(1))
            if route.startswith('/api/school/publications'):paths.insert(0,paths.pop(3))
            if route.startswith('/api/planning/experimental-school')or route.startswith('/api/planning/sources'):
                paths.append('/api/planning/sources')
            return paths
        return []

    def _candidates(self,value,names):
        found=[]
        def visit(v):
            if isinstance(v,dict):
                for key,item in v.items():
                    if key in names and not isinstance(item,(dict,list)) and item is not None:
                        label=_display(item)if isinstance(item,(int,float,bool))else _record_label(v,len(found)+1)
                        pair=(item,label or _display(item))
                        if not any(item==old[0] for old in found):found.append(pair)
                    visit(item)
            elif isinstance(v,list):
                for item in v:visit(item)
        visit(value)
        return found

    def _record(self,value,identifier):
        if isinstance(value,dict):
            if value.get('id')==identifier:return value
            for item in value.values():
                found=self._record(item,identifier)
                if found is not None:return found
        elif isinstance(value,list):
            for item in reversed(value):
                found=self._record(item,identifier)
                if found is not None:return found
        return None

    def _list_values(self,context,keys,*,ids=False):
        pairs=[]
        def visit(value):
            if isinstance(value,dict):
                for key,item in value.items():
                    if key in keys and isinstance(item,list):
                        for row in item:
                            if isinstance(row,dict)and ('id'in row or key=='respondents'and 'membershipId'in row):
                                pair=(row.get('id',row.get('membershipId')),_record_label(row,len(pairs)+1))
                            elif not ids and isinstance(row,(str,int)):pair=(row,_display(row))
                            else:continue
                            if not any(pair[0]==old[0]for old in pairs):pairs.append(pair)
                    visit(item)
            elif isinstance(value,list):
                for item in value:visit(item)
        visit(context)
        return pairs

    def _resource_values(self,context,prefixes):
        pairs=[]
        def rows(value):
            if isinstance(value,list):
                for item in value:rows(item)
            elif isinstance(value,dict):
                if isinstance(value.get('task'),dict):rows(value['task']);return
                if isinstance(value.get('id'),str):
                    label=_record_label(value,len(pairs)+1)
                    if not any(value['id']==p[0]for p in pairs):pairs.append((value['id'],label))
                else:
                    for item in value.values():
                        if isinstance(item,list):rows(item)
        def visit(value):
            if isinstance(value,dict):
                for key,item in value.items():
                    if any(key==prefix or key.startswith(prefix+'/')for prefix in prefixes):rows(item)
                    visit(item)
            elif isinstance(value,list):
                for item in reversed(value):visit(item)
        visit(context)
        return pairs

    def _remember(self,state,value):
        context=state.get('context',[])
        if not isinstance(context,list):context=[context]
        state['context']=(context+[value])[-8:]

    def _labels(self,state):
        labels={}
        def visit(value):
            if isinstance(value,dict):
                for key in ('id','eventId','membershipId'):
                    if isinstance(value.get(key),str):labels[value[key]]=_record_label(value,len(labels)+1)
                for item in value.values():visit(item)
            elif isinstance(value,list):
                for item in value:visit(item)
        visit(state.get('context',{}))
        return labels

    def _pages(self,ns,index,count):
        buttons=[]
        if index>1:buttons.append(self._button('Предыдущая страница',ns,'result '+str(index-1)))
        if index<count:buttons.append(self._button('Следующая страница',ns,'result '+str(index+1)))
        buttons.append(self._button('Продолжить',ns,''))
        return buttons

    def _next(self,state,actor,ns,event):
        form=state['form']
        while form['pending']:
            field=form['pending'][0];schema=self._schema(field['schema']);field['schema']=schema
            form.setdefault('fieldSchemas',{}).setdefault(json.dumps(field['path']),deepcopy(schema))
            name=str(field['path'][-1]); label=' → '.join(_label(str(p)) if not isinstance(p,int) else str(p+1) for p in field['path'][1:])
            if field['path'][0]=='body':
                target=form['path'].get('experiment_id')or form['body'].get('targetExperimentId')
                if target and form.get('detailTarget')!=target:
                    form['detailTarget']=target
                    prefix='/api/planning/experimental-school/'+quote(str(target),safe='')
                    for suffix in ['/version','/makeups']:
                        value=self.runtime.request(actor,'GET',prefix+suffix)
                        if value['status']==200:
                            form['baselines']={prefix+suffix:value['data'],**form.get('baselines',{})}
                            self._remember(state,value['data'])
                selected_run=form['path'].get('run_id')or (form['body'].get('proposal')or{}).get('runId')
                selected_task=form['path'].get('task_id')
                task_prefix='/api/launch/tasks/'if form['action']['nativeEndpoint'].startswith('/api/launch')else'/api/schedule-agent/tasks/'
                resource='/api/planning/runs/'+quote(str(selected_run),safe='')if selected_run else task_prefix+quote(str(selected_task),safe='')if selected_task else None
                if resource and resource not in form.get('loadedResources',[]):
                    form.setdefault('loadedResources',[]).append(resource)
                    value=self.runtime.request(actor,'GET',resource)
                    if value['status']==200:
                        form['baselines']={resource:value['data'],**form.get('baselines',{})}
                        self._remember(state,{resource:value['data']})
                route=form['action']['nativeEndpoint'];student=form['body'].get('studentId')
                if student and route.startswith('/api/staff/student-schedule-bindings'):
                    query={'studentId':student}
                    if route.endswith('/dated'):
                        if form['body'].get('publicationVersion')and form['body'].get('lessonDate'):
                            query.update(publicationVersion=form['body']['publicationVersion'],lessonDate=form['body']['lessonDate'])
                        else:query=None
                    resource=route+'?'+urlencode(query)if query else None
                    if resource and resource not in form.get('loadedResources',[]):
                        form.setdefault('loadedResources',[]).append(resource)
                        value=self.runtime.request(actor,'GET',resource)
                        if value['status']==200:
                            form['baselines']={resource:value['data'],**form.get('baselines',{})}
                            self._remember(state,{resource:value['data']})
            if schema.get('type')=='file':return self._reply('Прикрепите XLSX расписания размером до 2 МиБ. Сначала будет проверен файл.',[self._button('Отменить',ns,'cancel')])
            if ns=='exchange'and name=='content':
                fmt=form['body'].get('format','csv')
                return self._reply('Прикрепите тестовый файл '+fmt.upper()+' в выбранном формате, до 2 МиБ.'+
                    (f' Для CSV можно отправить таблицу текстом: /{ns} answer …'if fmt=='csv'else ''),[self._button('Отменить',ns,'cancel')])
            if name=='filename' and form.get('document'):
                self._set(form,field['path'],form['document']['name']);form['pending'].pop(0);continue
            models=self._record_models(form)
            if field['path']==['path','kind'] and models:
                schema={'type':'string','enum':list(models)};field['schema']=schema
            if field['path']==['path','action'] and form['action']['nativeEndpoint'].startswith('/api/school/calendar/records'):
                schema={'type':'string','enum':['archive','restore']};field['schema']=schema
            if name=='record' and models and form['path'].get('kind') in models:
                schema=self._local_schema(models[form['path']['kind']]);field['schema']=schema
            if name=='settings' and self.spec.get('x-settings-model'):
                schema=self._local_schema(self.spec['x-settings-model']);field['schema']=schema
            if name=='resolutions'and self.spec.get('x-source-resolutions-model'):
                schema=self._local_schema(self.spec['x-source-resolutions-model']);field['schema']=schema
            if name in {'requestId','commandId'}:
                value=hashlib.sha256((event+':'+form['action']['id']).encode()).hexdigest()[:32]
                if name=='requestId'and form['action']['nativeEndpoint']=='/api/schedule-agent/tasks'and form['body'].get('proposal')is not None:value=None
                self._set(form,field['path'],value)
                form['pending'].pop(0);continue
            if name=='existingProvenance'and 'resolutions'in field['path']:
                form['pending'].pop(0);continue
            baseline=form.get('baselines',{})
            registry=baseline.get('/api/planning/school-source-registry',{})
            if name in {'packageHash','draftHash','draftRevision'}and form['action']['nativeEndpoint']=='/api/planning/school-source-registry':
                value=registry.get('binding',{}).get(name)
                if value is not None:self._set(form,field['path'],value);form['pending'].pop(0);continue
            if name in {'expectedRegistryRevision','expectedSourceRevision'}:
                source='/api/planning/school-source-registry'if name=='expectedRegistryRevision'else'/api/planning/sources'
                value=baseline.get(source,{}).get('revision')
                if type(value)is int:self._set(form,field['path'],value);form['pending'].pop(0);continue
            if name in {'expectedPackageHash','expectedCalendarHash'}:
                key='packageHash'if name=='expectedPackageHash'else'calendarHash'
                value=baseline.get('/api/planning/sources',{}).get(key)
                if isinstance(value,str):self._set(form,field['path'],value);form['pending'].pop(0);continue
            if name=='expectedRevision'and not field['required']and form['action']['nativeEndpoint'].startswith('/api/schedule-agent/tasks')and not form['body'].get('targetExperimentId'):
                self._set(form,field['path'],None);form['pending'].pop(0);continue
            if name=='expectedRevision'and 'proposal'in field['path']and isinstance(baseline.get('/api/timetable'),dict):
                self._set(form,field['path'],baseline['/api/timetable']['revision']);form['pending'].pop(0);continue
            if name=='configRevision'and 'proposal'in field['path']and isinstance(baseline.get('/api/planning/config'),dict):
                self._set(form,field['path'],baseline['/api/planning/config']['revision']);form['pending'].pop(0);continue
            if name in {'expectedDraftRevision','baseDraftRevision'} and isinstance(baseline.get('/api/timetable'),dict):
                value=baseline['/api/timetable'].get('revision')
                if type(value)is int:
                    self._set(form,field['path'],value);form['pending'].pop(0);continue
            if name=='expectedConfigRevision' and isinstance(baseline.get('/api/planning/config'),dict):
                value=baseline['/api/planning/config'].get('revision')
                if type(value)is int:
                    self._set(form,field['path'],value);form['pending'].pop(0);continue
            if name=='expectedSchoolRequestRevision'and form['body'].get('schoolRequestId'):
                rows=baseline.get('/api/school/requests',{}).get('requests',[])
                row=next((r for r in rows if r.get('id')==form['body']['schoolRequestId']),None)
                if row and type(row.get('revision'))is int:
                    self._set(form,field['path'],row['revision']);form['pending'].pop(0);continue
            if name=='expectedVersionHash':
                versions=[v for k,v in baseline.items()if k.endswith('/version')and isinstance(v,dict)]
                candidates=[(v['versionHash'],v['versionHash'])for v in versions if isinstance(v.get('versionHash'),str)]
                if candidates:
                    self._set(form,field['path'],candidates[0][0]);form['pending'].pop(0);continue
            if name=='expectedTurn':
                primary=next(iter(baseline.values()),{})
                if isinstance(primary,dict)and type(primary.get('turn'))is int:
                    self._set(form,field['path'],primary['turn']);form['pending'].pop(0);continue
            if name=='hash'and 'proposal'in field['path']:
                candidates=self._candidates(baseline,{'runHash'})
                if candidates:
                    self._set(form,field['path'],candidates[0][0]);form['pending'].pop(0);continue
            if name in {'expectedActiveVersion','expectedPublicationVersion'}:
                publication=baseline.get('/api/school/publications',{}).get('active')
                if isinstance(publication,dict) and type(publication.get('version'))is int:
                    self._set(form,field['path'],publication['version']);form['pending'].pop(0);continue
            if name=='publicationVersion'and form['action']['nativeEndpoint'].startswith('/api/staff/student-schedule-bindings'):
                publication=baseline.get('/api/school/publications',{}).get('active')
                if isinstance(publication,dict)and type(publication.get('version'))is int:
                    self._set(form,field['path'],publication['version']);form['pending'].pop(0);continue
            if name.startswith('expected'):
                key=name[8:];key=key[:1].lower()+key[1:]
                baseline=form.get('baselines',{})
                if name=='expectedRevision':
                    primary=next(iter(baseline.values()),{})
                    if form['action']['nativeEndpoint'].startswith('/api/launch')and isinstance(primary,dict):primary=primary.get('task',primary)
                    versions=[v for k,v in baseline.items()if k.endswith('/version')and isinstance(v,dict)]
                    if versions:primary=versions[0]
                    if form['action']['nativeEndpoint'].startswith('/api/staff/student-schedule-bindings')and isinstance(primary,dict):primary=primary.get('binding',primary)
                    if ns=='exchange':
                        kind=form['body'].get('kind')
                        catalog=baseline.get('/api/school/data-exchange/catalog',{})
                        primary=next((row for row in catalog.get('datasets',[])if row.get('kind')==kind),catalog)
                    selected=next(iter(form['path'].values()),None)
                    if form['action']['namespace']=='/requests' and selected:
                        rows=primary.get('requests',[]) if isinstance(primary,dict) else []
                        primary=next((row for row in rows if row.get('id')==selected),{})
                    elif isinstance(primary,dict) and 'revision'not in primary and selected:
                        rows=primary.get('items',primary.get('records',[]))
                        if isinstance(rows,list):primary=next((row for row in rows if row.get('id')==selected),{})
                    if isinstance(primary,dict)and 'revision'not in primary and selected:
                        primary=self._record(state.get('context',{}),selected)or primary
                    candidates=[(primary['revision'],str(primary['revision']))] if isinstance(primary,dict) and type(primary.get('revision'))is int else []
                elif name=='expectedLedgerRevision':
                    ledgers=[v for k,v in baseline.items()if k.endswith('/makeups')and isinstance(v,dict)]
                    candidates=[(v['revision'],str(v['revision']))for v in ledgers if type(v.get('revision'))is int]
                else:candidates=self._candidates(baseline,{key})
                if len(candidates)==1:
                    self._set(form,field['path'],candidates[0][0]);form['pending'].pop(0);continue
            if name in {'previewHash','proposalHash','recordHash','eventHash'}:
                preview_route=form['action']['nativeEndpoint']
                for key in ('apply','decision','publish'):
                    if preview_route.endswith('/'+key):preview_route=preview_route[:-len(key)]+'preview'
                if form['action']['nativeEndpoint'].startswith('/api/school/replacements/') and form['action']['nativeEndpoint'].endswith('/publish'):
                    preview_route=form['action']['nativeEndpoint']+'-preview'
                if form['action']['nativeEndpoint'].startswith('/api/school/calendar/records'):
                    preview_route='/api/school/calendar/preview'
                for key,value in form['path'].items():preview_route=preview_route.replace('{'+key+'}',quote(str(value),safe=''))
                exact=state.get('previews',{}).get(preview_route)
                candidates=self._candidates(exact,{name}) if exact else []
                if not candidates and name!='previewHash':candidates=self._candidates(state.get('context',{}),{name})
                if len(candidates)==1:
                    self._set(form,field['path'],candidates[0][0]);form['pending'].pop(0);continue
                if not candidates and field['required']:
                    return self._reply('Для действия нужен свежий предпросмотр выбранной записи. '
                        'Откройте предпросмотр в этом разделе, затем вернитесь к применению. '
                        'Бот подставит полученное подтверждение; вводить технический код не нужно.',
                        [self._button('Выбрать предпросмотр',ns,'')])
            if not schema:
                field['schema']={'anyOf':[{'type':'string','title':'текст'},{'type':'integer','title':'целое число'},
                    {'type':'boolean','title':'да или нет'},{'type':'array','title':'список','items':{}},
                    {'type':'object','title':'поля записи','additionalProperties':True}]}
                continue
            if schema.get('type')=='object' or 'properties' in schema:
                if not schema.get('properties') and schema.get('additionalProperties'):
                    if name=='operation'and not field.get('manual'):
                        objects=[]
                        def operations(value):
                            if isinstance(value,dict):
                                if isinstance(value.get('operation'),dict)and value['operation']not in objects:objects.append(value['operation'])
                                for item in value.values():operations(item)
                            elif isinstance(value,list):
                                for item in value:operations(item)
                        operations(state.get('context',{}))
                        if objects:
                            field['objectChoices']=objects
                            field['objectLabels']=['Проверенное предложение '+str(i+1)for i in range(len(objects))]
                            return self._reply('Выберите проверенное предложение допзанятия.',self._options(ns,field['objectLabels'],objects=True))
                    try:self._get(form,field['path'])
                    except (KeyError,IndexError):self._set(form,field['path'],{})
                    if self._get(form,field['path']) is None:self._set(form,field['path'],{})
                    keys=[];key_labels=[]
                    package=baseline.get('/api/planning/sources',{}).get('package',{})
                    for cell in package.get('cells',[]):
                        key=cell.get('classLabel')if name=='classMap'else cell.get('teacher')if name=='teacherMap'else cell.get('sourceCell')if name in {'groupMap','roomIds','countOverrides','subjectOverrides','teacherOverrides','excludedCells'}else None
                        if isinstance(key,str)and key not in keys:
                            keys.append(key);key_labels.append(key if name in {'classMap','teacherMap'}else ' · '.join(str(cell[k])for k in ('sourceCell','classLabel','teacher')if cell.get(k)))
                    field['keyChoices']=keys
                    field['keyLabels']=key_labels
                    buttons=self._options(ns,key_labels,keys=True)
                    return self._reply('Заполните '+(label or 'поля записи')+'. Добавить поле: '+
                        f'/{ns} answer /key название', buttons+[self._button('Запись заполнена',ns,'answer /done'),self._button('Отменить',ns,'cancel')])
                if name in {'target','input','lesson','operation','resolutions'} and not field.get('manual'):
                    objects=[];labels=[]
                    def collect(value):
                        if isinstance(value,dict):
                            if isinstance(value.get(name),dict)and value[name] and value[name] not in objects:
                                objects.append(value[name]);row=value.get('lesson',value[name])
                                label=' · '.join(str(row[k])for k in ['title','name','className','subject','date']if isinstance(row,dict)and row.get(k))
                                labels.append(label or ('Текущая нормативная модель'if name=='input'else 'Выбранная запись '+str(len(objects))))
                            for item in value.values():collect(item)
                        elif isinstance(value,list):
                            for item in value:collect(item)
                    collect(state.get('context',{}))
                    if objects:
                        field['objectChoices']=objects;field['objectLabels']=labels
                        return self._reply('Выберите '+_label(name).lower()+' из полученных данных.',
                            self._options(ns,labels,objects=True)+
                            [self._button('Заполнить по полям',ns,'answer /manual'),self._button('Отменить',ns,'cancel')])
                self._set(form,field['path'],{})
                fields=list(schema.get('properties',{}).items())
                fields.sort(key=lambda item:0 if item[0]in {'kind','format','type'}else 1 if item[0]=='studentId'else 2 if item[0]=='publicationVersion'else 3 if item[0]=='lessonDate'else 10 if item[0].startswith('expected') or item[0]in {'requestId','commandId','previewHash','proposalHash','recordHash','eventHash','hash'} else 4)
                children=[{'path':field['path']+[k],'schema':v,'required':k in schema.get('required',[])}for k,v in fields]
                form['pending'][:1]=children;continue
            buttons=[]
            if schema.get('type')=='array':
                try:self._get(form,field['path'])
                except (KeyError,IndexError):self._set(form,field['path'],[])
                if self._get(form,field['path']) is None:self._set(form,field['path'],[])
                buttons=[self._button('Добавить элемент',ns,'answer /add'),self._button('Список заполнен',ns,'answer /done')]
            elif 'anyOf' in schema or 'oneOf' in schema:
                variants=schema.get('anyOf',schema.get('oneOf'))
                labels=[]
                for variant in variants:
                    resolved=self._schema(variant);kind=resolved.get('properties',{}).get('kind',{})
                    code=kind.get('const')
                    if code is None and len(kind.get('enum',[]))==1:code=kind['enum'][0]
                    labels.append('Не задано'if resolved.get('type')=='null'else _display(code)if code else 'Указать '+_label(resolved.get('title',resolved.get('type','значение'))))
                buttons=[self._button(label,ns,'answer '+str(i+1))for i,label in enumerate(labels)]
            else:
                choices=schema.get('enum');labels=None
                if 'const'in schema:choices=[schema['const']]
                if name=='format'and not choices:
                    pattern=schema.get('pattern','')
                    if re.fullmatch(r'\^\([A-Za-z0-9]+(?:\|[A-Za-z0-9]+)+\)\$',pattern):choices=pattern[2:-2].split('|')
                if ns=='exchange'and name=='format':
                    kind=form['path'].get('kind',form['body'].get('kind'))
                    catalog=form.get('baselines',{}).get('/api/school/data-exchange/catalog',{})
                    dataset=next((d for d in catalog.get('datasets',[])if d.get('kind')==kind),{})
                    if dataset.get('formats'):choices=dataset['formats']
                if name in {'day','weekdays','schoolDays'} or any(p in {'weekdays','schoolDays'}for p in field['path']if isinstance(p,str)):
                    if schema.get('type')=='integer':
                        choices=list(range(math.ceil(schema.get('minimum',0)),math.floor(min(schema.get('maximum',6),6))+1))
                        labels=[['Понедельник','Вторник','Среда','Четверг','Пятница','Суббота','Воскресенье'][i]for i in choices]
                if choices is None and schema.get('type')=='boolean':choices=[True,False]
                if choices is None:
                    aliases={name}
                    if name=='targetRevision':aliases.add('revision')
                    if name in {'ident','identity','identifier','record_id','member_id','invoice_id','run_id','task_id','question_id','experiment_id','lesson_id'}:aliases.add('id')
                    if name.endswith('Id'):aliases.add('id')
                    candidates=self._candidates(state.get('context',{}),aliases)
                    context=state.get('context',{})
                    if name in {'run_id','runId'}:candidates=self._resource_values(context,{'/api/planning/runs'})
                    elif name=='schoolRequestId':candidates=self._resource_values(context,{'/api/school/requests'})
                    elif name=='studentId':candidates=self._list_values(context,{'students'},ids=True)
                    elif name in {'experiment_id','experimentId','targetExperimentId'}:candidates=self._resource_values(context,{'/api/planning/experimental-school'})
                    elif name in {'task_id','taskId'}:candidates=self._resource_values(context,{'/api/schedule-agent/tasks','/api/launch/tasks'})
                    elif name=='question_id':candidates=self._list_values(context,{'questions'},ids=True)
                    elif name=='outbox_id':candidates=self._resource_values(context,{'/api/launch/outbox'})
                    elif name in {'member_id','memberId','respondentMembershipId'}:candidates=self._list_values(context,{'members','respondents'},ids=True)
                    elif name=='approval_id':candidates=self._list_values(context,{'revocations'},ids=True)
                    elif name=='nominationId':candidates=self._list_values(context,{'nominations'},ids=True)
                    elif name in {'lessonId','lesson_id'}:candidates=self._list_values(context,{'lessons'},ids=True)
                    elif name in {'identifier','ident','identity','record_id','recordId','replacementId'}:
                        kind=form['path'].get('kind')
                        if kind:candidates=self._list_values(context,{kind},ids=True)
                        elif ns=='requests':candidates=self._resource_values(context,{'/api/school/requests'})
                        elif ns=='preferences':candidates=self._list_values(context,{'records'},ids=True)
                        elif ns=='replacements':candidates=self._list_values(context,{'records'},ids=True)
                        elif ns=='notifications':candidates=self._list_values(context,{'items'},ids=True)
                    if name=='replacementTeacher':candidates=self._list_values(state.get('context',{}),{'teacherOptions'})
                    if 'classMap'in field['path']:candidates=self._candidates(context,{'className'})
                    if 'teacherMap'in field['path']:candidates=self._list_values(context,{'teachers'},ids=True)
                    if 'groupMap'in field['path']:candidates=self._list_values(context,{'groups'},ids=True)
                    if 'roomIds'in field['path']and 'resolutions'in field['path']:candidates=self._list_values(context,{'rooms'},ids=True)
                    if name=='targetId':
                        record=form['body'].get('record',form['body'])
                        if isinstance(record,dict)and record.get('kind')in {'teacher_unavailable','room_unavailable'}:
                            candidates=self._list_values(context,{'teachers'if record['kind']=='teacher_unavailable'else'rooms'},ids=True)
                    if name=='role':candidates=self._list_values(state.get('context',{}),{'roles'})
                    array_name=field['path'][-2]if isinstance(field['path'][-1],int)else None
                    if array_name in {'teacherIds','groupIds','roomIds','allowedSlotIds','sourceLessonIds','lessonIds'}:
                        keys={'teacherIds':'teachers','groupIds':'groups','roomIds':'rooms','allowedSlotIds':'slots','sourceLessonIds':'lessons','lessonIds':'lessons'}
                        candidates=self._list_values(state.get('context',{}),{keys[array_name]},ids=True)
                    elif array_name=='teacherRefs':candidates=self._list_values(state.get('context',{}),{'teacherRefs','teachers'})
                    elif array_name=='calendarTeacherIds':candidates=self._list_values(state.get('context',{}),{'teachers'},ids=True)
                    choices=[x[0] for x in candidates];labels=[x[1] for x in candidates]
                    if name.startswith('expected'):
                        source=name[len('expected'):];source=source[:1].lower()+source[1:]
                        def revisions(v):
                            if isinstance(v,dict):
                                if source in v and v[source] not in choices and isinstance(v[source],int):choices.append(v[source])
                                for x in v.values():revisions(x)
                            elif isinstance(v,list):
                                for x in v:revisions(x)
                        revisions(state.get('context',{}))
                form['choices']=choices
                form['choiceLabels']=[labels[i]if labels and i<len(labels)else _display(v)for i,v in enumerate(choices)]
                buttons=self._options(ns,form['choiceLabels'])
            if not field['required']:buttons.append(self._button('Пропустить',ns,'answer /skip'))
            buttons.append(self._button('Отменить',ns,'cancel'))
            hints=[]
            if schema.get('format')=='date':hints.append('дата ГГГГ-ММ-ДД')
            if schema.get('type')=='integer':hints.append('целое число')
            if name=='deadlineAt':hints=['например «через 1 час» или дата со смещением: 2026-11-08T10:00+03:00']
            if schema.get('description'):hints.append(schema['description'])
            return self._reply('Введите '+(label or 'значение')+('. '+', '.join(hints) if hints else '')+
                f'.\nОтвет: /{ns} answer ваш ответ',buttons)
        form['ready']=True
        if form['action']['method']=='GET':return self._execute(state,actor,ns,event)
        return self._reply('Проверьте: '+self._title(form['action'])+'\n'+
                           _human({k:v for k,v in form.items() if k in {'body','path','query'}},labels=self._labels(state))+
                           '\nОтправить действие родному сервису?',
                           [self._button('Подтвердить',ns,'confirm'),self._button('Исправить ввод',ns,'change'),self._button('Отменить',ns,'cancel')])

    def _execute(self,state,actor,ns,event):
        form=state['form'];action=form['action'];path=action['nativeEndpoint']
        for name,value in form['path'].items():path=path.replace('{'+name+'}',quote(str(value),safe=''))
        if '{' in path:return self._reply('Не выбран объект действия. Вернитесь в меню.')
        if form['query']:path+='?'+urlencode(form['query'],doseq=True)
        native_event=form['nativeEvent']
        if form.get('document'):
            result=self.runtime.upload(actor,action['method'],path,form['document'],native_event)
        else:
            result=self.runtime.request(actor,action['method'],path,
                payload=form['body'] or None,event_id=native_event)
        data=result.get('data')
        if result['status']>=400:
            detail=data.get('detail') if isinstance(data,dict) else None
            safe=detail if isinstance(detail,str) else ('Проверьте введённые поля и права выбранной роли.' if result['status']==422 else 'Сервис не подтвердил действие. Обновите данные и повторите проверку.')
            if isinstance(data,dict) and data.get('uncertain')is True:
                form['uncertain']=True
                return self._reply('Результат операции неизвестен; новая отправка отключена. Проверьте записи раздела.',[self._button('Открыть меню',ns,'')])
            return self._reply('Действие не подтверждено. '+safe,[self._button('Исправить ввод',ns,'change'),self._button('Открыть меню',ns,'')])
        self._remember(state,{path.split('?')[0]:data})
        if isinstance(data,dict)and isinstance(data.get('id'),str):
            identifier=data['id']
            aliases={'agt_':'/api/schedule-agent/tasks/','run_':'/api/planning/runs/','exp_':'/api/planning/experimental-school/'}
            for prefix,resource in aliases.items():
                if identifier.startswith(prefix):self._remember(state,{resource+identifier:data})
        if isinstance(data,dict) and ('previewHash'in data or 'proposalHash'in data):
            state.setdefault('previews',{})[path.split('?')[0]]=data
        state.pop('form',None)
        state.pop('continuation',None)
        shown=data
        native_token=None
        if action['nativeEndpoint']in {'/api/staff/invites','/api/staff/student-access/invitations'}and isinstance(data,dict):
            native_token,shown=_private_invitation(data)
            if native_token is None:raise ValueError('native_invitation_capability_missing')
            shown['notice']='Создано одноразовое тестовое приглашение. Подтверждение входа нового сотрудника этим действием не выполнялось.'
        text='Результат: '+self._title(action)+'\n'+_human(shown,labels=self._labels(state))
        state['resultPages']=[text[i:i+3500] for i in range(0,len(text),3500)] or ['Результат подтверждён.']
        state['resultGuards']=[]
        if action['method']=='GET':
            fingerprint=hashlib.sha256(json.dumps({'data':data,'file':result.get('file')},
                sort_keys=True,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode('utf-8')).hexdigest()
            state['resultGuards']=[{'kind':'native_read','path':path,'fingerprint':fingerprint}]
        buttons=self._pages(ns,1,len(state['resultPages']))
        preview_valid=isinstance(data,dict)and (data.get('ready')is True if 'ready'in data else isinstance(data.get('validation'),dict)and data['validation'].get('valid')is True)
        if preview_valid and isinstance(data.get('previewHash'),str):
            source=action['nativeEndpoint'];targets=[]
            if source.endswith('/publish-preview'):targets=[source[:-len('-preview')]]
            elif source.endswith('/preview'):targets=[source[:-7]+'apply',source[:-7]+'publish',source[:-7]+'decision']
            target=next((a for a in self.actions if a['namespace']=='/'+ns and a['method']=='POST'and a['nativeEndpoint']in targets),None)
            if target:
                operation=self.spec['paths'].get(target['nativeEndpoint'],{}).get('post',{})
                target_schema=self._schema(operation.get('requestBody',{}).get('content',{}).get('application/json',{}).get('schema',{}))
                properties=target_schema.get('properties',{})
                body={'input':deepcopy(form['body'])}if 'input'in properties and 'input'not in form['body']else {k:deepcopy(v)for k,v in form['body'].items()if k in properties}
                body['previewHash']=data['previewHash']
                if 'operation'in properties and isinstance(data.get('operation'),dict):body['operation']=deepcopy(data['operation'])
                state['continuation']={**deepcopy(form),'action':target,'body':body,'pending':[],'ready':False}
                buttons.insert(0,self._button('Продолжить с проверенными данными',ns,'continue'))
        reply=self._reply(state['resultPages'][0],buttons,result.get('file'))
        if state['resultGuards']:reply['resourceGuards']=deepcopy(state['resultGuards'])
        if native_token:
            # Private handoff to PlatformStore; it must strip this before any UI,
            # status, log or export and wrap it in a scoped one-time MAX link.
            reply['nativeInviteToken']=native_token
        return reply
