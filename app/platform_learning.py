"""Russian conversation adapters for the independently copied native platform.

No domain state or authorization is reimplemented here. Every operation uses
the native authenticated RPC, including revision checks and document jobs.
"""
from __future__ import annotations

import base64
from contextvars import ContextVar
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from urllib.parse import quote, urlencode
from uuid import NAMESPACE_URL, uuid5


NAMESPACES = {'materials', 'learning', 'journal', 'consultations', 'clubs', 'recovery', 'ai'}
_READ_TRACE = ContextVar('ideyafix_learning_native_reads',default=None)
STATUS = {'sent': 'Выдано', 'active': 'В работе', 'submitted': 'На проверке',
          'revision_requested': 'Нужна доработка', 'reviewed': 'Проверено',
          'offered': 'Предложена', 'booked': 'Запись подтверждена', 'cancelled': 'Отменена',
          'shared': 'Доступ открыт', 'revoked': 'Доступ отозван', 'stale': 'Условия изменились',
          'draft': 'Черновик', 'confirmed': 'Подтверждено', 'paused': 'Приостановлено',
          'completed': 'Завершено', 'present': 'Присутствовал', 'absent': 'Отсутствовал',
          'excused': 'Уважительная причина', 'requested': 'Материал запрошен',
          'missing': 'Материал не передан'}


def _button(label, command):
    return {'label': str(label)[:100], 'command': command}


def _reply(text, buttons=(), document=None):
    result = {'text': str(text)[:3900], 'buttons': list(buttons)[:12]}
    if document is not None:
        result['document'] = document
    return result


def _label(row):
    if row.get('replacementId') and row.get('materialRevision'):
        return row.get('date','') + ' · версия материала ' + str(row['materialRevision'])
    if row.get('replacementId'):
        return row.get('date','') + ' · ' + row.get('className','') + ' · ' + row.get('subject','')
    if row.get('lessonDate') and row.get('subject'):
        return row['lessonDate'] + ' · ' + row['subject'] + ' · ' + row.get('className', '')
    return str(row.get('title') or row.get('name') or row.get('topic') or row.get('programTitle')
               or row.get('subject') or 'Запись')[:160]


def _id(event_id, operation):
    return str(uuid5(NAMESPACE_URL, 'ideyafix:education:' + str(event_id) + ':' + operation))


def _field(key, prompt, kind='text', *, options=None, optional=False):
    return {'key': key, 'prompt': prompt, 'kind': kind, 'options': options or [], 'optional': optional}


class NativeFailure(Exception):
    def __init__(self, response):
        self.status = response.get('status', 503)
        data = response.get('data')
        detail = data.get('detail') if isinstance(data, dict) else None
        self.message = detail if isinstance(detail, str) else 'Не удалось подтвердить результат операции.'
        self.uncertain = isinstance(data, dict) and data.get('uncertain') is True


class LearningAssistant:
    def __init__(self, runtime, state_store=None):
        self.runtime = runtime
        if state_store is None:
            from .platform_chat import ChatState
            state_store = ChatState(runtime.root / 'conversations.sqlite3')
        self.store = state_store

    def _role(self, actor):
        # The native registry includes profiles accepted after startup. It is
        # only navigation metadata; every read/write still rechecks native auth.
        return next((row['role'] for row in self.runtime.profiles() if row['id']==actor), '')

    def handle(self, actor_id, text, event_id, *, session_key=None):
        if not isinstance(text, str):
            return None
        text = text.strip()
        if not text: return None
        scope = str(session_key or 'actor') + ':' + str(actor_id)
        prefix = text.split(maxsplit=1)[0].lstrip('/').lower() if text.startswith('/') else ''
        active = self.store.peek('education', scope)
        if prefix not in NAMESPACES and not active.get('namespace'):
            return None
        if prefix and prefix not in NAMESPACES | {'cancel'}:
            return None
        def transition(state):
            try:
                return self._handle(actor_id, text, event_id, state)
            except NativeFailure as error:
                namespace = state.get('namespace', prefix if prefix in NAMESPACES else 'learning')
                if error.uncertain:
                    return _reply('Результат ранее начатого действия неизвестен. Повторная запись с новым запросом не выполняется. Откройте актуальные записи и проверьте результат.',
                                  [_button('Открыть раздел', '/' + namespace)])
                if error.status==409 and state.get('operation')=='material_update':
                    state['materialConflict']=True
                    return _reply('Версия материала изменилась. Ваш введённый текст сохранён в диалоге. Сначала сравните текущую серверную версию.',[_button('Сравнить версии','/materials compare'),_button('Отменить ввод','/cancel')])
                state.clear()
                if error.status == 401:
                    message = 'Текущий доступ не подтверждён. Выберите тестовую роль заново через /role.'
                elif error.status == 403:
                    message = 'Операция недоступна. ' + error.message
                elif error.status == 409:
                    message = 'Данные или условия доступа изменились. Откройте актуальный список и повторите действие.\n' + error.message
                else:
                    message = error.message
                return _reply(message, [_button('Открыть раздел', '/' + namespace)])
            except (ValueError, TypeError, KeyError, IndexError, InvalidOperation):
                return _reply('Проверьте ответ и допустимый формат. Действие не подтверждено. Можно повторить ввод или отменить.',
                              [_button('Отменить', '/cancel')])
        return self.store.run('education',scope,event_id,text,lambda state:self._guarded_transition(state,transition))

    def _guarded_transition(self,state,transition):
        inherited=list(state.get('_readGuards',[]))
        trace={'guards':[],'wrote':False}
        token=_READ_TRACE.set(trace)
        try:
            reply=transition(state)
            if reply is not None and not trace['wrote']:
                # A fresh native read supersedes the preceding snapshot.
                # Otherwise form/selection steps still render that snapshot.
                retained=any(key in state for key in ('form','choices','selected','journalFilters','documentJob'))
                guards=trace['guards']or(inherited if retained else [])
                if guards:
                    state['_readGuards']=guards
                    reply['resourceGuards']=reply.get('resourceGuards',[])+guards
                else:state.pop('_readGuards',None)
            else:
                # A mutation acknowledgement must not carry its pre-write
                # revision, which the successful write has just changed.
                state.pop('_readGuards',None)
            return reply
        finally:
            _READ_TRACE.reset(token)

    @staticmethod
    def _text_file_limit(state):
        if not state.get('form')or state.get('confirm'):return None
        field=state['form'][state['index']]
        if field['kind']!='text'or field['options']:return None
        limits={'material_create':{'content':24000,'answers':12000},
                'material_update':{'content':24000,'answers':12000},
                'assignment_create':{'content':4000,'answers':8000},
                'learning_draft':{'text':8000},'learning_submit':{'text':8000}}
        return limits.get(state.get('operation'),{}).get(field['key'])

    def handle_file(self,actor_id,document,event_id,*,session_key=None):
        scope=str(session_key or 'actor')+':'+str(actor_id)
        try:
            encoded=json.dumps(document,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
        except (TypeError,ValueError):return _reply('Нужен текстовый файл .txt в UTF-8.')
        fingerprint='file:'+sha256(encoded).hexdigest()
        def transition(state):
            limit=self._text_file_limit(state)
            if limit is None:
                return _reply('Сначала откройте ввод длинного текста материала, ответов или работы ученика. Файл не меняет выбранное действие.',[_button('Отменить ввод','/cancel')])
            try:
                if not isinstance(document,dict)or not isinstance(document.get('name'),str)or not document['name'].lower().endswith('.txt'):
                    raise ValueError()
                raw=base64.b64decode(document['base64'],validate=True)
                if not 0<len(raw)<=limit*4+3:raise ValueError()
                text=raw.decode('utf-8-sig')
                if not text.strip()or len(text)>limit or any(ord(c)<32 and c not in '\n\r\t'or 0x7f<=ord(c)<=0x9f or 0xd800<=ord(c)<=0xdfff for c in text):raise ValueError()
            except (ValueError,TypeError,KeyError,UnicodeError):
                return _reply('Нужен файл .txt в UTF-8, не более '+str(limit)+' знаков. Текущий ввод сохранён; отправьте исправленный файл.',[_button('Отменить ввод','/cancel')])
            state['_inputEvent']=str(event_id);state['_actor']=actor_id
            return self._advance(actor_id,state['namespace'],'',state,event_id,literal_value=text)
        return self.store.run('education',scope,event_id,fingerprint,lambda state:self._guarded_transition(state,transition))

    def _rpc(self, actor, method, path, payload=None, *, event=None, operation='request'):
        result = self.runtime.request(actor, method, path, payload=payload,
                                      event_id=None if event is None else str(event) + ':' + operation)
        if not 200 <= result.get('status', 503) < 300:
            raise NativeFailure(result)
        trace=_READ_TRACE.get()
        if trace is not None:
            readonly_posts={'/api/school/data-exchange/preview','/api/school/data-exchange/export',
                            '/api/school/clubs/charges/preview','/api/director/club-reconciliation/preview'}
            if method=='GET':
                data=result.get('data')
                guard={'kind':'native_read','path':path}
                if path=='/api/teacher/preparation':
                    data={key:value for key,value in data.items()if key!='lessonRefreshInMs'}
                    guard['projection']='teacher_preparation'
                encoded=json.dumps({'data':data,'file':result.get('file')},ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
                guard['fingerprint']=sha256(encoded).hexdigest()
                trace['guards']=[row for row in trace['guards']if row['path']!=path]+[guard]
            elif method not in {'HEAD'}and path not in readonly_posts:
                trace['wrote']=True
        data = result.get('data')
        if data is None and (result.get('file') or {}).get('contentType', '').startswith('application/json'):
            data = json.loads(base64.b64decode(result['file']['base64'], validate=True))
        if isinstance(data, dict) and result.get('file'):
            data = {**data, 'file': result['file']}
        return data if data is not None else result

    def _handle(self, actor, text, event, state):
        state['_inputEvent'] = str(event)
        state['_actor'] = actor
        if text == '/cancel':
            namespace = state.get('namespace', 'learning'); state.clear()
            return _reply('Ввод отменён. Подтверждённые записи сохранены.', [_button('Вернуться', '/' + namespace)])
        parts = text.split(maxsplit=1)
        explicit = parts[0][1:] if text.startswith('/') else ''
        if explicit in NAMESPACES:
            namespace = explicit
            command = parts[1].strip() if len(parts) > 1 else ''
            if command.split(maxsplit=1)[0:1] and command.split(maxsplit=1)[0] in {'choose','page','answer','skip','confirm'} and state.get('namespace') != namespace:
                return _reply('Этот шаг относится к другому разделу. Откройте нужный раздел и выберите действие заново.', [_button('Открыть раздел','/'+namespace)])
            if not command:
                state.clear(); state['namespace'] = namespace
                return self._menu(actor, namespace)
        else:
            namespace = state.get('namespace', 'learning'); command = text
        if command.startswith('choose ') and state.get('choices') is not None:
            selected = int(command.split()[1]) - 1
            row = state['choices'][selected]
            operation = state['choiceOperation']; context = state.get('context', {})
            state.clear(); state.update(namespace=namespace, _inputEvent=str(event), _actor=actor)
            return self._selected(actor, namespace, operation, row, context, state, event)
        if command.startswith('page ') and state.get('choices') is not None:
            return self._choice_page(state, int(command.split()[1]))
        if 'form' in state:
            return self._advance(actor, namespace, command, state, event)
        state['namespace'] = namespace
        return self._command(actor, namespace, command, state, event)

    def _menu(self, actor, namespace):
        role = self._role(actor)
        teacher, student, director = role == 'teacher', role == 'student', role == 'director'
        if namespace == 'materials' and role in {'director', 'planner'}:
            return _reply('Материалы по заменам\n\n'
                          'Посмотрите, какие версии переданы преподавателям и где материал ещё ждут.',
                          [_button('Текущие передачи', '/materials shares'),
                           _button('История передач', '/materials share_history')])
        menus = {
            'materials': [('Моя библиотека', 'list'), ('Новый материал', 'new'), ('Подготовка к урокам', 'dashboard'),
                          ('По классу', 'classes'), ('Передача материалов', 'shares'), ('История передач', 'share_history'), ('Черновик с ИИ', 'ai')],
            'learning': [('Все задания' if teacher else 'Открыть задания', 'list'), *(([('Выдать задание', 'assign')] if teacher else [])),
                         *(([('На проверку', 'submitted'), ('На доработке', 'revision'), ('Проверено', 'reviewed'), ('Поиск работ', 'search')] if teacher else [])),
                         *(([('Самостоятельная практика', 'practice')] if student else [])),
                         ('Обзор обучения', 'overview')],
            'recovery': [('Требуют внимания', 'list'), ('На доработке', 'revision'), ('Срок прошёл', 'overdue'),
                         ('На проверке', 'submitted'), *(([('По прошедшему уроку', 'lessons')] if student else [])), ('Все мои задания', 'all')],
            'journal': [('Успеваемость и явка', 'view'), ('Выбрать период', 'period'),
                        *(([('Внести запись', 'new'), ('Выгрузить журнал', 'export')] if role in {'director','platform_admin'} else []))],
            'consultations': [('Мои консультации', 'list'), *(([('Предложить время', 'new')] if teacher else []))],
            'clubs': ([('Программы', 'programs'), ('Участие учеников', 'enrollments'), ('Начисления', 'charges'),
                       *(([('Сверка поступлений', 'reconcile')] if director else []))] if director or role == 'platform_admin' else [('Мои кружки', 'mine'), ('Мои документы', 'documents')]),
            'ai': ([('Черновик материала', 'material')] if teacher else
                   [('Практика с ИИ', 'practice'), ('Обсудить задание с ИИ', 'tutor')]),
        }
        title = {'materials': 'Мои материалы', 'learning': 'Работы учеников' if teacher else 'Мои задания',
                 'recovery': 'После пропусков', 'journal': 'Оценки и посещаемость',
                 'consultations': 'Консультации', 'clubs': 'Кружки и документы', 'ai': 'Помощник ИИ'}[namespace]
        description = {
            'materials': 'Откройте библиотеку, подготовьте урок или передайте материал на замену.',
            'learning': 'Выдайте задание или выберите работу для проверки.' if teacher else
                        'Откройте задание, чтобы прочитать условия и отправить ответ.',
            'recovery': 'Посмотрите задания, которые требуют внимания после пропусков.',
            'journal': 'Посмотрите оценки и посещаемость за нужный период.',
            'consultations': 'Откройте свои записи и предложения преподавателей.',
            'clubs': 'Откройте программы, записи и документы, доступные вашему профилю.',
            'ai': 'Выберите учебный сценарий помощника.',
        }[namespace]
        return _reply(title + '\n\n' + description,
                      [_button(label, '/' + namespace + ' ' + command) for label, command in menus[namespace]])

    def _choices(self, state, namespace, operation, rows, title, *, context=None):
        if not rows:
            state.clear(); state['namespace'] = namespace
            return _reply(title + '\nПока нет доступных записей.', [_button('Меню раздела', '/' + namespace)])
        current = {key:state[key]for key in ('_inputEvent','_actor')if key in state}
        state.clear();state.update(current)
        state.update(namespace=namespace, choices=rows, choiceOperation=operation, choiceTitle=title, context=context or {})
        return self._choice_page(state, 0)

    def _choice_page(self, state, page):
        rows = state['choices']; page = max(0, min(page, (len(rows) - 1) // 8)); state['page'] = page
        start = page * 8; selected = rows[start:start + 8]; namespace = state['namespace']
        buttons = [_button(str(start + n + 1) + '. ' + _label(row), f'/{namespace} choose {start+n+1}') for n, row in enumerate(selected)]
        if page: buttons.append(_button('← Назад', f'/{namespace} page {page-1}'))
        if start + 8 < len(rows): buttons.append(_button('Далее →', f'/{namespace} page {page+1}'))
        buttons.append(_button('Отменить', '/cancel'))
        lines = [state['choiceTitle']]
        for n, row in enumerate(selected):
            status = STATUS.get(row.get('status'), row.get('status', ''))
            lines.append(f'{start+n+1}. {_label(row)}' + (f' — {status}' if status else ''))
        return _reply('\n'.join(lines), buttons)

    def _form(self, state, namespace, operation, fields, *, values=None, context=None):
        context = dict(context or {})
        context['nativeIntent'] = _id(state['_inputEvent'], str(state['_actor']) + ':' + operation)
        state.clear(); state.update(namespace=namespace, form=fields, index=0, values=values or {}, operation=operation, context=context)
        return self._prompt(state)

    def _prompt(self, state, page=0):
        field = state['form'][state['index']]
        page=max(0,min(page,(len(field['options'])-1)//8)) if field['options'] else 0
        start=page*8
        buttons = [_button(str(start+n+1) + '. ' + str(label), '/' + state['namespace'] + ' answer ' + str(start+n+1))
                   for n, (label, _) in enumerate(field['options'][start:start+8])]
        if page:buttons.append(_button('← Назад','/'+state['namespace']+' options '+str(page-1)))
        if start+8<len(field['options']):buttons.append(_button('Далее →','/'+state['namespace']+' options '+str(page+1)))
        if field['optional']: buttons.append(_button('Пропустить', '/' + state['namespace'] + ' skip'))
        buttons.append(_button('Отменить', '/cancel'))
        limit=self._text_file_limit(state)
        note='\nМожно отправить .txt в UTF-8, до '+str(limit)+' знаков.'if limit else''
        return _reply(field['prompt']+note, buttons)

    def _advance(self, actor, namespace, command, state, event, *, literal_value=None):
        if command.startswith('options ')and not state.get('confirm'):
            return self._prompt(state,int(command.split()[1]))
        if state.get('materialConflict'):
            if command=='compare':
                remote=self._rpc(actor,'GET','/api/teacher/preparation/materials/'+quote(state['context']['id'],safe=''))['material']
                state['context']['remoteRevision']=remote['revision']
                full=remote['title']+' · версия '+str(remote['revision'])+'\n\n'+remote['content']+'\n\nОтветы преподавателя:\n'+remote.get('answers','')
                return _reply('Текущая серверная версия:\n'+full+'\n\nВаш ввод остаётся в диалоге. Следующий шаг позволит сохранить его вместо этой версии.',[_button('Оставить мой ввод, принять версию','/materials rebase'),_button('Отменить ввод','/cancel')],self._text_file('current-material.txt',full)if len(full)>2500 else None)
            if command=='rebase' and state['context'].get('remoteRevision'):
                state['values']['expectedRevision']=state['context'].pop('remoteRevision')
                state['context']['nativeIntent']=_id(event,str(actor)+':material_update')
                state.pop('materialConflict');state['confirm']=True
                return _reply('Ваш текст сохранён: '+state['values']['title']+'\n\n'+state['values']['content'][:1800]+'\n\nПодтвердить замену проверенной серверной версии вашим вводом?',[_button('Подтвердить','/materials confirm'),_button('Отменить ввод','/cancel')],self._text_file('my-material-draft.txt',state['values']['content']+'\n\nОтветы преподавателя:\n'+state['values'].get('answers','')))
            return _reply('Сначала сравните новую серверную версию; ваш ввод сохранён.',[_button('Сравнить версии','/materials compare'),_button('Отменить ввод','/cancel')])
        if state.get('confirm'):
            if command.lower() not in ('confirm', 'подтвердить', 'да'):
                return _reply('Подтвердите действие кнопкой или отмените ввод.', [_button('Подтвердить', '/' + namespace + ' confirm'), _button('Отменить', '/cancel')])
            context = state['context']
            operation = state['operation']; values = dict(state['values'])
            result = self._execute(actor, operation, values, context, context['nativeIntent'])
            state.clear(); state['namespace'] = namespace
            if operation == 'journal_view': state['journalFilters'] = values
            return result
        field = state['form'][state['index']]
        value = literal_value if literal_value is not None else command[7:].strip() if command.startswith('answer ') else command.strip()
        if literal_value is None and value == 'skip' and field['optional']:
            value = None if field['kind'] in {'int', 'grade', 'money', 'datetime'} else ''
        elif field['options']:
            index = int(value) - 1
            if not 0 <= index < len(field['options']): raise ValueError()
            value = field['options'][index][1]
        elif field['kind'] == 'int':
            value = int(value)
            if not 0 <= value <= 2**31-1: raise ValueError()
        elif field['kind'] == 'grade':
            value = float(value.replace(',', '.'))
            if not 0 <= value <= 5: raise ValueError()
        elif field['kind'] == 'money':
            amount = Decimal(value.replace(',', '.'))
            if not amount.is_finite() or amount < 0 or amount.as_tuple().exponent < -2: raise ValueError()
            value = int(amount * 100)
        elif field['kind'] == 'date':
            value = date.fromisoformat(value).isoformat()
        elif field['kind'] == 'datetime':
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None or parsed.utcoffset() is None: raise ValueError()
            value = parsed.isoformat()
        elif not value or not value.strip() or len(value) > 24000:
            raise ValueError()
        state['values'][field['key']] = value
        if state['operation'] == 'journal_create' and field['key'] == 'studentId':
            selected_class = state['context']['studentClasses'][value]
            lesson_field = next(item for item in state['form'] if item['key']=='lesson')
            lesson_field['options'] = [(label,row) for label,row in lesson_field['options'] if row['className']==selected_class]
            if not lesson_field['options']:
                state.clear();state['namespace']='journal'
                return _reply('Для выбранного ученика нет уроков в подтверждённой публикации.',[_button('Открыть журнал','/journal')])
        state['index'] += 1
        if state['index'] < len(state['form']): return self._prompt(state)
        if state['operation']=='assignment_search':
            query=state['values']['query'].casefold();data=self._dashboard(actor)
            students={row['id']:row['name']for row in data.get('students',[])}
            rows=[row for row in data['assignments']if any(query in str(value).casefold()for value in (row['title'],row['topic'],row['subject'],students.get(row.get('studentId'),'')))]
            return self._choices(state,'learning','assignment_open',rows,'Поиск работ: '+state['values']['query'])
        if state['operation'] == 'journal_create':
            values = state['values']; lesson = values['lesson']; context = state['context']
            record = {'studentId':values['studentId'], 'lessonId':lesson['lessonId'], 'lessonDate':lesson['lessonDate'],
                      'publicationVersion':context['publicationVersion'], 'attendance':values['attendance'], 'grade':values['grade'],
                      'sourceRef':'bot-' + sha256(context['nativeIntent'].encode()).hexdigest()[:24]}
            payload = {'kind':'lessonProgress', 'format':'json', 'expectedRevision':values['expectedRevision'],
                       'content':json.dumps({'version':1,'kind':'lessonProgress','records':[record]},ensure_ascii=False,allow_nan=False)}
            preview = self._rpc(actor,'POST','/api/school/data-exchange/preview',payload,event=context['nativeIntent'],operation='journal-preview')
            if not preview['ready']:
                errors = [error['message'] for row in preview['rows'] for error in row['errors']]
                state.clear(); state['namespace']='journal'
                return _reply('Запись не прошла штатную проверку.\n'+'\n'.join(errors),[_button('Открыть журнал','/journal')])
            context['nativePayload'] = {**payload, 'previewHash':preview['previewHash']}
            summary = lesson['lessonDate']+' · '+lesson['subject']+' · '+lesson['className']+'\nЯвка: '+STATUS[values['attendance']]+'\nОценка: '+(str(values['grade']) if values['grade'] is not None else 'не указана')+'\nШтатная проверка пройдена. Добавить подтверждённую запись?'
        elif state['operation'] in {'charge_create', 'reconciliation_apply'}:
            values = state['values']; op = state['operation']
            path = '/api/school/clubs/charges/preview' if op == 'charge_create' else '/api/director/club-reconciliation/preview'
            preview = self._rpc(actor, 'POST', path, values, event=state['context']['nativeIntent'], operation='preview')
            if op == 'charge_create': values['expectedHash'] = preview['hash']; amount = preview['proposal']['amountKopeks']
            else: values['previewHash'] = preview['previewHash']; amount = preview['amountKopeks']
            summary = f'Предварительный расчёт: {Decimal(amount)/100:.2f} ₽. Это тестовый учёт, деньги не переводятся.'
        else:
            lines = []
            for item in state['form']:
                answer = state['values'].get(item['key'])
                if item['options']: answer = next((label for label, value in item['options'] if value == answer), answer)
                elif item['kind'] == 'money' and answer is not None: answer = f'{Decimal(answer)/100:.2f} ₽'
                if answer not in ('', None): lines.append(item['prompt'].split('\n')[0] + '\n' + str(answer)[:800])
            summary = '\n\n'.join(lines)
            if state['context'].get('checkedMaterial'):
                summary += '\n\nПроверенный сохранённый материал:\n' + state['context']['checkedMaterial'][:1800]
        state['confirm'] = True
        checked = state['context'].get('checkedMaterial')
        document=self._text_file('checked-assignment.txt',checked)if checked and len(checked)>1800 else None
        if document is None and any(isinstance(state['values'].get(item['key']),str)and len(state['values'][item['key']])>800 for item in state['form']):
            complete_rows=[]
            for item in state['form']:
                answer=state['values'].get(item['key'])
                if item['options']:answer=next((label for label,value in item['options']if value==answer),answer)
                if item['kind']=='money'and answer is not None:answer=f'{Decimal(answer)/100:.2f} ₽'
                complete_rows.append(item['prompt'].split('\n')[0]+'\n'+(str(answer)if answer is not None else'Не указано'))
            complete='\n\n'.join(complete_rows)
            document=self._text_file('draft-to-confirm.txt',complete)
        return _reply('Проверьте перед сохранением:\n\n' + summary, [_button('Подтвердить', '/' + namespace + ' confirm'), _button('Отменить', '/cancel')],
                      document)

    def _dashboard(self, actor):
        return self._rpc(actor, 'GET', '/api/' + ('teacher' if self._role(actor) == 'teacher' else 'student') + '/learning')

    def _command(self, actor, ns, command, state, event):
        role = self._role(actor)
        if ns == 'ai':
            target = {'material': ('materials', 'ai'),
                      'practice': ('learning', 'practice'), 'tutor': ('learning', 'list')}.get(command)
            if target: return self._command(actor, target[0], target[1], state, event)
        if ns == 'materials':
            base = '/api/teacher/preparation'
            if command == 'dashboard':
                data = self._rpc(actor, 'GET', base)
                blockers = {'publication_or_teacher_binding_required':'нужна публикация и привязка преподавателя',
                    'no_current_lesson_in_publication':'сейчас урока нет', 'no_upcoming_lesson_in_publication':'в этой публикации следующих уроков нет',
                    'publication_period_expired':'неделя публикации закончилась', 'confirmed_current_calendar_required':'нужно подтвердить актуальный календарь',
                    'publication_changed':'публикация изменилась', 'published_lessons_overlap':'есть пересечение опубликованных уроков',
                    'published_lesson_calendar_conflict':'есть конфликт с календарём'}
                lines = ['Материалов в библиотеке: ' + str(data['materialsTotal']), 'ИИ: ' + ('доступен' if data['ai']['enabled'] else 'выключен; платных запросов нет')]
                for key, label in [('currentLesson','Текущий урок'),('nextLesson','Ближайший урок')]:
                    lesson = data.get(key)
                    lines.append(label + ': ' + (lesson['subject']+' · '+lesson['className']+' · '+lesson.get('date','')+' '+lesson.get('start','') if lesson else blockers.get(data.get(key+'Blocker'),'текущие условия не подтверждены')))
                lines += [row['className']+': уроков '+str(row['lessonCount'])+', материалов '+str(row['materialCount'])+', с текстом '+str(row['withContentCount']) for row in data['classes']]
                buttons = [_button('Библиотека', '/materials list'), _button('Создать материал', '/materials new'), _button('По классу','/materials classes')]
                if data.get('nextLesson'): buttons.append(_button('Подготовить следующий урок','/materials next'))
                return _reply('\n'.join(lines), buttons)
            if command == 'list':
                return self._choices(state, ns, 'material_open', self._rpc(actor, 'GET', base + '/materials')['materials'], 'Выберите материал. У каждого сохраняется собственная версия.')
            if command == 'classes':
                dashboard = self._rpc(actor, 'GET', base)
                return self._choices(state, ns, 'material_class', [{'title':row['className'], **row} for row in dashboard['classes']], 'Выберите класс для фильтра библиотеки.')
            if command in ('new', 'ai', 'next'):
                dashboard = self._rpc(actor, 'GET', base)
                lesson = dashboard.get('nextLesson') if command == 'next' else None
                if command == 'next' and not lesson:
                    return _reply('Нет подтверждённой даты следующего урока. Выберите опубликованный урок при создании материала или создайте личный материал.',[_button('Создать материал','/materials new')])
                duration = None
                if lesson:
                    def minutes(value):
                        hour, minute = map(int,value.split(':')); return hour*60+minute
                    length = minutes(lesson['end'])-minutes(lesson['start'])
                    duration = length if 0<length<=240 else None
                if command == 'ai' and not dashboard['ai']['enabled']:
                    return _reply('Штатный ИИ подготовки выключен: ключ или выбранная модель недоступны. Материал можно создать вручную; платный запрос не выполнялся.', [_button('Создать вручную', '/materials new')])
                return self._form(state, ns, 'material_ai' if command == 'ai' else 'material_create',
                    [_field('kind', 'Что подготовить?', options=[('План урока', 'lesson_plan'), ('Учебный материал', 'material'), ('Проверочные задания', 'assessment')]),
                     _field('title', 'Название материала', optional=command == 'ai'), _field('topic', 'Тема'),
                     *([] if lesson else [_field('className', 'Для какого класса? Можно пропустить.', optional=True), _field('subject', 'Предмет', optional=True)]),
                     _field('objective', 'Цель занятия', optional=True),
                     *([] if lesson else [_field('durationMinutes', 'Продолжительность занятия в минутах (можно пропустить)', 'int', optional=True),
                     _field('lessonId', 'Привязать к опубликованному уроку?', options=[('Без привязки', None)] + [(row['subject'] + ' · ' + row['className'] + ' · урок ' + str(row['slot']), row['id']) for row in dashboard['lessons']])]),
                     _field('instructions' if command == 'ai' else 'content', 'Пожелания к черновику' if command == 'ai' else 'Текст материала'),
                     *([] if command == 'ai' else [_field('answers', 'Ответы для преподавателя (можно пропустить)', optional=True)])],
                     values={'className':lesson['className'],'subject':lesson['subject'],'durationMinutes':duration,'lessonId':lesson['id']} if lesson else None)
            if command in ('shares','share_history'):
                path = '/api/school/replacement-materials' if role in {'director', 'planner'} else '/api/teacher/replacement-materials'
                data = self._rpc(actor, 'GET', path)
                if command == 'share_history':
                    return self._choices(state,ns,'share_history_open',data['history'],'История разрешений на версии материалов.',context={'overview':data})
                return self._choices(state, ns, 'share_open', data['items'], 'Передача материалов для опубликованных замен. Доступ открывает одну выбранную версию.', context={'overview': data})
            if command in ('edit', 'export', 'assign', 'share_material', 'share_request', 'grant_open', 'grant_revoke'):
                return self._context_action(actor, ns, command, state, event)
        if ns in {'learning', 'recovery'}:
            if ns=='learning'and command=='search':
                return self._form(state,ns,'assignment_search',[_field('query','Поиск по заданию, предмету или ученику')])
            if command == 'overview':
                data = self._dashboard(actor)
                if role == 'teacher':
                    lines = ['Доступных учеников: ' + str(len(data['students'])), 'Заданий: ' + str(len(data['assignments']))]
                    lines += [row['name'] + ' · ' + row['className'] + ' · ' + ', '.join(row['subjects']) for row in data['students']]
                else:
                    performance = data.get('performance') or {}
                    lines = [data['student']['name'] + ' · ' + data['student']['className'], 'Заданий: ' + str(len(data['assignments'])),
                        'Средняя оценка из личной карточки: ' + (str(performance.get('averageGrade')) if performance.get('averageGrade') is not None else 'не указана'),
                        'Посещаемость из личной карточки: ' + (str(performance.get('attendancePercent')) + '%' if performance.get('attendancePercent') is not None else 'не указана'),
                        'Предметы практики: ' + ', '.join(data.get('practiceSubjects', []))]
                lines.append('ИИ: ' + ('доступен' if data['ai']['enabled'] else 'выключен; платного запроса не было'))
                return _reply('\n'.join(lines), [_button('Мои задания', '/learning list'), _button('Подтверждённый журнал', '/journal view')])
            if ns == 'recovery' and command == 'lessons':
                schedule = self._rpc(actor,'GET','/api/student/schedule')
                if not schedule.get('complete') or not schedule.get('periodStart'):
                    return _reply('Для выбора прошедшего урока нужно подтверждённое личное расписание.',[_button('Мои задания','/learning list')])
                first = date.fromisoformat(schedule['periodStart']); rows=[]
                for lesson in schedule['lessons']:
                    when = date.fromisoformat(lesson['date']) if lesson.get('date') else first+timedelta(days=lesson['day'])
                    if when < date.today(): rows.append({**lesson,'date':when.isoformat(),'title':lesson['subject']+' · '+when.isoformat()})
                return self._choices(state,ns,'recovery_lesson',rows,'Выберите прошедший урок. Это фильтр по предмету, а не подтверждение пропуска.')
            if command in ('list', 'all', 'revision', 'overdue', 'submitted', 'reviewed'):
                data = self._dashboard(actor); rows = data['assignments']
                if ns=='learning'and command in {'revision','submitted','reviewed'}:
                    status={'revision':'revision_requested','submitted':'submitted','reviewed':'reviewed'}[command]
                    rows=[row for row in rows if row['status']==status]
                if ns == 'recovery' and command != 'all':
                    def overdue(row): return row['status'] in {'sent','revision_requested'} and bool(row.get('dueAt')) and datetime.fromisoformat(row['dueAt']).timestamp()<datetime.now(timezone.utc).timestamp()
                    rows = [row for row in rows if (row['status']=='revision_requested' if command=='revision' else overdue(row) if command=='overdue' else row['status']=='submitted' if command=='submitted' else row['status']in{'revision_requested','submitted'}or overdue(row))]
                    rows.sort(key=lambda row:(datetime.fromisoformat(row['dueAt']).timestamp() if row.get('dueAt') else float('inf'),row['createdAt']))
                return self._choices(state, ns, 'assignment_open', rows, 'Выберите задание. Ответы и история попыток сохраняются в платформе.')
            if command in ('assign', 'assign_ai'):
                data = self._dashboard(actor)
                if command == 'assign_ai':
                    return _reply('Сначала сохраните и проверьте личный черновик материала, затем отдельно назначьте готовый текст ученику.', [_button('Подготовить черновик', '/materials ai'), _button('Библиотека', '/materials list')])
                students = [row for row in data['students'] if row['accountReady']]
                if not students: return _reply('Нет доступных учеников с активной тестовой учётной записью.')
                subjects = sorted({subject for row in students for subject in row['subjects']})
                return self._form(state, 'learning', 'assignment_create',
                    [_field('studentId', 'Кому выдать задание?', options=[(row['name'] + ' · ' + row['className'], row['id']) for row in students]),
                     _field('subject', 'Предмет', options=[(item, item) for item in subjects]), _field('topic', 'Тема'),
                     _field('gap', 'Какой пробел нужно устранить?'), _field('title', 'Название задания'),
                     _field('content', 'Условие задания' if command == 'assign' else 'Пожелания к заданию'),
                     _field('answers', 'Ответы для преподавателя', optional=True), _field('dueAt', 'Срок: ГГГГ-ММ-ДДTЧЧ:ММ+03:00. Можно пропустить.', 'datetime', optional=True)],
                    values={'generate': False})
            if command == 'practice':
                data = self._dashboard(actor)
                if not data['ai']['enabled']: return _reply('Штатный ИИ самостоятельной практики выключен. Существующие задания доступны; платного запроса не было.', [_button('Мои задания', '/learning list')])
                return self._form(state, ns, 'practice_create', [_field('subject', 'Предмет', options=[(item, item) for item in data['practiceSubjects']]), _field('topic', 'Тема практики'), _field('instructions', 'Что хотите потренировать?', optional=True)])
            if command in ('draft', 'submit', 'review', 'message', 'tutor', 'history'):
                return self._context_action(actor, ns, command, state, event)
        if ns == 'journal':
            if command in ('view', 'period') and role not in {'teacher', 'student', 'director'}:
                if role == 'platform_admin':
                    return _reply('Личный журнал и отчёт доступны ученику, преподавателю и директору. Для внесения записей оставайтесь администратором; для отчёта смените тестовую роль через /role.', [_button('Внести запись', '/journal new'), _button('Выгрузить журнал', '/journal export')])
                return _reply('Личный журнал недоступен этой роли. Выберите ученика, преподавателя или директора через /role.')
            if command == 'view':
                state['journalFilters'] = {}
                return self._journal(actor, {})
            if command.startswith('history '):
                return self._journal(actor, {**state.get('journalFilters',{}),'page':int(command.split()[1])})
            if command == 'period':
                fields=[_field('start', 'Начало периода: ГГГГ-ММ-ДД', 'date'), _field('end', 'Конец периода: ГГГГ-ММ-ДД', 'date'), _field('subject', 'Предмет (можно пропустить)', optional=True)]
                if role != 'student': fields.append(_field('className', 'Класс (можно пропустить)', optional=True))
                return self._form(state, ns, 'journal_view', fields)
            if command in ('new', 'export'):
                catalog = self._rpc(actor, 'GET', '/api/school/data-exchange/catalog')
                dataset = next(row for row in catalog['datasets'] if row['kind']=='lessonProgress')
                revision = dataset.get('revision',catalog['revision'])
                if command == 'export':
                    exported = self._rpc(actor,'POST','/api/school/data-exchange/export',{'kind':'lessonProgress','format':'csv','expectedRevision':revision,'includeArchived':False},event=event,operation='journal-export')
                    document = {**exported['file'],'name':'ideyafix-journal.csv'}
                    return _reply('Выгрузка подтверждённого тестового журнала подготовлена штатной службой.',document=document)
                references = self._rpc(actor, 'GET', '/api/school/data-exchange/lesson-progress-references')
                return self._journal_form(state, references, revision)
        if ns == 'consultations':
            data = self._rpc(actor, 'GET', '/api/' + role + '/consultations')
            if command == 'list': return self._choices(state, ns, 'consultation_open', data['items'], 'Выберите консультацию.')
            if command == 'new':
                return self._form(state, ns, 'consultation_create', [_field('studentId', 'Кого пригласить?', options=[(row['name'], row['id']) for row in data['students']]), _field('topic', 'Тема консультации'), _field('place', 'Где состоится консультация?'), _field('startAt', 'Дата и время с часовым поясом: ГГГГ-ММ-ДДTЧЧ:ММ+03:00', 'datetime'), _field('duration', 'Продолжительность', options=[(str(n) + ' минут', n) for n in (15, 20, 30, 45, 60)])])
            if command in ('book', 'cancel_booking'): return self._context_action(actor, ns, command, state, event)
        if ns == 'clubs': return self._clubs(actor, command, state, event)
        return _reply('Выберите действие в меню раздела. Текстовые ответы принимаются после вопроса бота.', [_button('Меню', '/' + ns)])

    def _selected(self, actor, ns, operation, row, context, state, event):
        state['selected'] = row; state['context'] = context
        if operation == 'share_history_open':
            lines=[row['date']+' · версия материала '+str(row['materialRevision']),STATUS.get(row['status'],row['status'])]
            if row.get('stale'): lines.append('Разрешение относится к прежней публикации.')
            for item in row['history']:
                lines.append(('Версия открыта' if item['action']=='shared' else 'Доступ отозван')+' · '+item['at']+(('\nПричина: '+item['reason'])if item.get('reason')else''))
            return _reply('\n\n'.join(lines),[_button('Текущие передачи','/materials shares')])
        if operation == 'recovery_lesson':
            dashboard = self._dashboard(actor)
            rows = [item for item in dashboard['assignments'] if item['subject'].strip().casefold()==row['subject'].strip().casefold() and item['status'] in {'sent','revision_requested','submitted'}]
            return self._choices(state,'recovery','assignment_open',rows,row['subject']+' · урок '+row['date']+'\nПоказаны ваши задания по предмету. Они не привязаны к конкретному уроку.')
        if operation == 'material_open':
            material = self._rpc(actor, 'GET', '/api/teacher/preparation/materials/' + quote(row['id'], safe=''))['material']
            state['selected'] = material
            full = material['title'] + '\nТема: ' + material['topic'] + '\nВерсия: ' + str(material['revision']) + '\n\n' + material['content'] + ('\n\nОтветы преподавателя:\n' + material['answers'] if material.get('answers') else '')
            return _reply(full, [_button('Редактировать', '/materials edit'), _button('Перейти к выдаче задания', '/materials assign'), _button('Полный текст файлом', '/materials export'), _button('Передать на замену', '/materials shares')], self._text_file('material.txt', full) if len(full) > 3500 else None)
        if operation == 'material_class':
            materials = self._rpc(actor, 'GET', '/api/teacher/preparation/materials?' + urlencode({'className':row['className']}))['materials']
            return self._choices(state, 'materials', 'material_open', materials, 'Материалы класса ' + row['className'])
        if operation == 'assignment_open':
            data = self._rpc(actor, 'GET', '/api/' + self._role(actor) + '/learning/assignments/' + quote(row['id'], safe=''))
            state['selected'] = data
            assignment = data['assignment']; attempts = data['work']['attempts']
            lines = [assignment['title'], assignment['subject'] + ' · ' + assignment['topic'], STATUS.get(assignment['status'], assignment['status']), assignment['content']]
            if assignment.get('dueAt'): lines.append('Срок: ' + assignment['dueAt'])
            if self._role(actor) == 'teacher':
                if assignment.get('gap'): lines.append('Учебный пробел: ' + assignment['gap'])
                if assignment.get('answers'): lines.append('Ответы для преподавателя:\n' + assignment['answers'])
            if data['work'].get('draft'): lines.append('Ваш черновик:\n' + data['work']['draft']['text'])
            for number, attempt in enumerate(attempts, 1):
                lines.append(f'Попытка {number}: ' + attempt['text'])
                if attempt.get('review'): lines.append(STATUS.get(attempt['review']['decision'], attempt['review']['decision']) + ': ' + attempt['review']['comment'])
            for message in data['messages'][-6:]: lines.append(('Преподаватель' if message['kind'] in {'teacher', 'assignment'} else 'Ученик' if message['kind'] == 'student' else 'ИИ') + ': ' + message['text'])
            buttons = [_button('Написать сообщение', '/learning message'), _button('Вся переписка', '/learning history'), _button('Вопрос ИИ', '/learning tutor')]
            if self._role(actor) == 'student': buttons[:0] = [_button('Сохранить черновик', '/learning draft'), _button('Сдать работу', '/learning submit')]
            if self._role(actor) == 'teacher' and attempts: buttons.insert(0, _button('Проверить попытку', '/learning review'))
            full = '\n\n'.join(lines)
            return _reply(full, buttons, self._text_file('learning-thread.txt', full) if len(full)>3500 else None)
        if operation == 'share_open':
            state['selected'] = row
            buttons = []
            if row.get('role') == 'recipient': buttons.append(_button('Запросить материал', '/materials share_request'))
            if row.get('role') == 'sender': buttons.append(_button('Открыть одну версию', '/materials share_material'))
            if row.get('grant') and row.get('role') in {'sender', 'recipient'}:
                buttons.append(_button('Прочитать передачу', '/materials grant_open'))
                if row.get('role') == 'sender':
                    buttons.append(_button('Отозвать передачу', '/materials grant_revoke'))
            if self._role(actor) in {'director', 'planner'}:
                buttons.append(_button('К списку замен', '/materials shares'))
            return _reply(str(row['date']) + ' · ' + row['className'] + ' · ' + row['subject'] + '\n' + STATUS.get(row['status'], row['status']) + '\nПередаётся только версия материала для этого урока.', buttons)
        if operation == 'consultation_open':
            buttons = [_button('Отменить консультацию', '/consultations cancel_booking')]
            if self._role(actor) == 'student' and row['status'] == 'offered': buttons.insert(0, _button('Записаться', '/consultations book'))
            return _reply(row['topic'] + '\n' + row['startAt'] + '\n' + row['place'] + '\n' + row['counterpart'] + '\n' + STATUS[row['status']], buttons)
        return self._club_selected(actor, ns, operation, row, context, state, event)

    def _confirm_action(self, state, ns, operation, values, context, text):
        context = {**context, 'nativeIntent': _id(state['_inputEvent'], str(state['_actor']) + ':' + operation)}
        state.clear(); state.update(namespace=ns, form=[], confirm=True, operation=operation, values=values, context=context)
        return _reply(text, [_button('Подтвердить', '/' + ns + ' confirm'), _button('Отменить', '/cancel')])

    @staticmethod
    def _text_file(name, text):
        return {'name': name, 'contentType': 'text/plain; charset=utf-8', 'base64': base64.b64encode(text.encode()).decode()}

    def _context_action(self, actor, ns, command, state, event):
        row = state.get('selected'); context = state.get('context', {})
        if not row: return _reply('Сначала выберите запись из актуального списка.', [_button('Открыть список', '/' + ns + (' shares' if command.startswith(('share', 'grant')) else ' list'))])
        if ns == 'materials':
            if command == 'export': return _reply('Полный текст выбранного материала.', document=self._text_file('material.txt', row['title'] + '\n\n' + row['content'] + '\n\n' + row.get('answers', '')))
            if command == 'assign':
                row = self._rpc(actor, 'GET', '/api/teacher/preparation/materials/' + quote(row['id'],safe=''))['material']
                if not row.get('subject','').strip() or not row['content'].strip() or len(row['content'])>4000 or len(row.get('answers',''))>8000:
                    return _reply('Сохраните и проверьте материал: нужен предмет и текст до 4000 знаков, ответы преподавателя до 8000. Текст не сокращается автоматически.',[_button('Редактировать материал','/materials edit')])
                students = [item for item in self._dashboard(actor)['students'] if item['accountReady'] and row['subject'] in item['subjects'] and (not row.get('className') or item['className']==row['className'])]
                if not students: return _reply('Для предмета и класса материала нет доступных учеников с активной учётной записью.')
                return self._form(state,'learning','assignment_create',[
                    _field('studentId','Кому выдать проверенный материал?',options=[(item['name']+' · '+item['className'],item['id'])for item in students]),
                    _field('gap','Какой пробел нужно устранить?'),
                    _field('dueAt','Срок: ГГГГ-ММ-ДДTЧЧ:ММ+03:00. Можно пропустить.','datetime',optional=True)],
                    values={key:row.get(key,'')for key in ('subject','topic','title','content','answers')}|{'generate':False},
                    context={'checkedMaterial':row['title']+' · версия '+str(row['revision'])+'\n\n'+row['content']+'\n\nОтветы преподавателя:\n'+row.get('answers','')})
            if command == 'edit':
                dashboard = self._rpc(actor, 'GET', '/api/teacher/preparation')
                return self._form(state, ns, 'material_update', [_field('title', 'Новое название'), _field('topic', 'Тема'),
                    _field('className', 'Класс (можно пропустить)', optional=True), _field('subject', 'Предмет (можно пропустить)', optional=True),
                    _field('objective', 'Цель (можно пропустить)', optional=True), _field('durationMinutes', 'Продолжительность в минутах (можно пропустить)', 'int', optional=True),
                    _field('lessonId', 'Привязка к уроку', options=[('Без привязки', None)] + [(item['subject'] + ' · ' + item['className'] + ' · урок ' + str(item['slot']), item['id']) for item in dashboard['lessons']]),
                    _field('content', 'Полный текст новой версии'), _field('answers', 'Ответы преподавателя', optional=True)], values={'expectedRevision': row['revision']}, context={'id': row['id']})
            overview = context['overview']; values = {'expectedRevision': overview['revision'], 'replacementId': row['replacementId'], 'expectedPublicationVersion': overview['publicationVersion']}
            if command == 'share_request': return self._confirm_action(state, ns, 'share_request', values, {}, 'Запросить материал для выбранной опубликованной замены?')
            if command == 'share_material':
                materials = self._rpc(actor, 'GET', '/api/teacher/preparation/materials')['materials']
                return self._choices(state, ns, 'share_choose_material', materials, 'Выберите версию своего материала.', context={'values': {**values, 'recipientMembershipId': row['recipientMembershipId']}})
            grant = row['grant']
            if command == 'grant_open':
                data = self._rpc(actor, 'GET', '/api/teacher/replacement-materials/grants/' + quote(grant['id'], safe=''))
                full = data['material']['title'] + '\n\n' + data['material']['content'] + '\n\n' + data['material'].get('answers', '')
                return _reply(full + '\n\n' + data['notice'], document=self._text_file('shared-material.txt', full)) | {'resourceGuards':[{'kind':'material_grant','grantId':grant['id']}]}
            if command == 'grant_revoke': return self._form(state, ns, 'share_revoke', [_field('reason', 'Причина отзыва')], values={'expectedRevision': overview['revision']}, context={'id': grant['id']})
        if ns in {'learning', 'recovery'}:
            assignment = row['assignment']; revision = row['thread']['revision']; path = '/api/' + self._role(actor) + '/learning/assignments/' + quote(assignment['id'], safe='')
            if command == 'history':
                current=self._rpc(actor,'GET',path)
                lines=['Переписка по заданию «'+current['assignment']['title']+'»']
                for message in current['messages']:
                    label='Преподаватель'if message['kind']in{'teacher','assignment'}else'Ученик'if message['kind']=='student'else'ИИ'
                    lines.append(label+' · '+message['at']+'\n'+message['text'])
                return _reply('Полная переписка: '+str(len(current['messages']))+' сообщений. Текст сохранён в файле.',document=self._text_file('learning-history.txt','\n\n'.join(lines)))
            if command in ('draft', 'submit', 'message', 'tutor'):
                if command == 'tutor' and not self._dashboard(actor)['ai']['enabled']: return _reply('Штатный ИИ выключен. Сообщение преподавателю доступно; платного вызова не было.', [_button('Написать преподавателю', '/learning message')])
                return self._form(state, 'learning', 'learning_' + command, [_field('text', {'draft': 'Текст черновика', 'submit': 'Полный ответ для сдачи', 'message': 'Сообщение собеседнику', 'tutor': 'Ваш вопрос ИИ'}[command])], values={'expectedRevision': revision}, context={'path': path})
            if command == 'review':
                attempts = row['work']['attempts']
                if not attempts: return _reply('Сданных попыток пока нет.')
                return self._form(state, 'learning', 'learning_review', [_field('attemptId', 'Какую попытку проверить?', options=[('Попытка ' + str(n+1), item['id']) for n, item in enumerate(attempts)]), _field('decision', 'Результат проверки', options=[('Принять работу', 'reviewed'), ('Попросить доработать', 'revision_requested')]), _field('comment', 'Комментарий ученику', optional=True)], values={'expectedRevision': revision}, context={'path': path})
        if ns == 'consultations': return self._confirm_action(state, ns, 'consultation_decision', {'expectedRevision': row['revision'], 'decision': 'book' if command == 'book' else 'cancel'}, {'id': row['id']}, 'Подтвердить выбранное действие с консультацией?')
        return _reply('Выберите действие из меню.', [_button('Меню', '/' + ns)])

    def _journal(self, actor, filters):
        filters = {key: value for key, value in filters.items() if value not in ('', None)}
        role = self._role(actor)
        data = self._rpc(actor, 'GET', '/api/' + role + '/lesson-progress' + ('?' + urlencode(filters) if filters else ''))
        summary = data['summary']; period = data['period']
        lines = [f"Журнал: {period['start']} — {period['end']}", 'Строк: ' + str(summary['recordCount']), 'Средняя оценка: ' + (str(summary['averageGrade']) if summary['averageGrade'] is not None else 'оценок нет'),
                 'Оценок: '+str(summary['gradeCount']),
                 f"Явка: {summary['present']}; отсутствовал: {summary['absent']}; уважительная причина: {summary['excused']}; не отмечено: {summary['attendanceUnknown']}"]
        for item in data['subjects']: lines.append(item['className'] + ' · ' + item['subject'] + ': ' + str(item['recordCount']) + ' записей, средняя ' + (str(item['averageGrade']) if item['averageGrade'] is not None else 'не рассчитана'))
        if data.get('history'):
            for row in data['history']['rows']: lines.append(row['lessonDate'] + ' · ' + row['subject'] + ' · ' + STATUS.get(row['attendance'], 'Явка не отмечена') + ' · ' + (str(row['grade']) if row['grade'] is not None else 'без оценки'))
        buttons = [_button('Выбрать период', '/journal period'), _button('Обновить', '/journal view')]
        history = data.get('history')
        if history:
            if history['page']>1: buttons.append(_button('Предыдущие записи','/journal history '+str(history['page']-1)))
            if history['page']*history['pageSize']<history['total']: buttons.append(_button('Следующие записи','/journal history '+str(history['page']+1)))
        full='\n'.join(lines)
        return _reply(full, buttons, self._text_file('journal-report.txt',full) if len(full)>3500 else None)

    def _journal_form(self, state, references, revision):
        # The reference export is native and bound to one confirmed publication.
        students = references.get('students', []); lessons = references.get('lessons', [])
        if not students or not lessons: return _reply('Для записи нужен опубликованный урок и подтверждённый состав. Сначала подготовьте их в разделе расписания.')
        return self._form(state, 'journal', 'journal_create', [_field('studentId', 'Ученик', options=[(row['name'], row.get('id') or row['studentId']) for row in students]), _field('lesson', 'Опубликованный урок', options=[(row.get('subject', 'Урок') + ' · ' + row.get('className', '') + ' · ' + row.get('lessonDate', row.get('date', '')), row) for row in lessons]), _field('attendance', 'Явка', options=[('Присутствовал', 'present'), ('Отсутствовал', 'absent'), ('Уважительная причина', 'excused')]), _field('grade', 'Оценка от 0 до 5 (можно пропустить)', 'grade', optional=True)], values={'expectedRevision': revision}, context={'publicationVersion': references['publicationVersion'], 'studentClasses':{row.get('id') or row['studentId']:row['className'] for row in students}})

    def _clubs(self, actor, command, state, event):
        if self._role(actor) == 'student':
            if command == 'document_status':
                selected = state.get('selected')
                if not selected or not state.get('documentJob'):
                    return _reply('Сначала выберите документ.', [_button('Мои документы', '/clubs documents')])
                return self._club_selected(actor, 'clubs', 'club_document', selected, {}, state, event)
            data = self._rpc(actor, 'GET', '/api/student/clubs')
            if command == 'documents': return self._choices(state, 'clubs', 'club_document', data.get('documents', []), 'Выберите документ. Бот проверит его актуальную версию перед подготовкой PDF.')
            lines = ['Мои кружки'] + [row['title'] + ' · ' + STATUS.get(row['status'], row['status']) + '\n' + row['scheduleNote'] + ' · ' + row['room'] for row in data['items']]
            for row in data.get('receipts', []): lines.append(row['title'] + ' · ' + row['period'] + f" · учтено {Decimal(row['receivedKopeks'])/100:.2f} ₽")
            for row in data.get('receiptNotices', []): lines.append('Учтено поступление: ' + row['title'] + ' · ' + row['receivedOn'])
            return _reply('\n\n'.join(lines) if len(lines)>1 else 'Доступных записей о кружках пока нет.', [_button('Документы', '/clubs documents')])
        if command in ('reconcile', 'reconcile_bind', 'reconcile_receipt'):
            data = self._rpc(actor, 'GET', '/api/director/club-reconciliation')
            return self._choices(state, 'clubs', 'reconcile_charge', data['charges'], 'Тестовая сверка поступлений. Денежные переводы не выполняются.', context={'revision': data['revision']})
        data = self._rpc(actor, 'GET', '/api/school/clubs')
        if command in ('programs', 'enrollments', 'charges'):
            extra = [_button('Создать программу', '/clubs program_new')] if command == 'programs' else [_button('Записать ученика', '/clubs enrollment_new')] if command == 'enrollments' else [_button('Рассчитать начисление', '/clubs charge_new')]
            result = self._choices(state, 'clubs', 'club_' + command, data[command], {'programs':'Программы кружков','enrollments':'Участие учеников','charges':'Начисления'}[command], context={'revision':data['revision']})
            result['buttons'] = extra + result['buttons']; return result
        if command == 'program_new': return self._program_form(state, data['revision'])
        if command == 'enrollment_new': return self._enrollment_form(state, data)
        if command == 'charge_new': return self._form(state, 'clubs', 'charge_create', [_field('enrollmentId', 'Участие ученика', options=[(row['studentName'] + ' · ' + next((_label(p) for p in data['programs'] if p['id']==row['programId']), 'Кружок'), row['id']) for row in data['enrollments'] if not row['archived'] and row['status']=='active']), _field('period', 'Период: ГГГГ-ММ'), _field('quantity', 'Количество занятий (для месячного тарифа — 1)', 'int')], values={'expectedRevision':data['revision']})
        if command in ('program_edit','enrollment_edit','record_archive','record_restore','charge_confirm','charge_cancel','bind_account','record_receipt'):
            return self._club_action(actor, command, state, event, data)
        return self._menu(actor, 'clubs')

    def _program_form(self, state, revision, row=None):
        return self._form(state, 'clubs', 'program_update' if row else 'program_create', [_field('title','Название программы'),_field('teacherName','Имя тестового преподавателя',optional=True),_field('scheduleNote','Когда проходят занятия?',optional=True),_field('room','Место занятий',optional=True),_field('description','Описание',optional=True),_field('feeKopeks','Тариф в рублях, не более двух знаков после запятой. Можно пропустить.','money',optional=True),_field('billingUnit','Единица тарифа',options=[('Месяц','month'),('Занятие','lesson')])],values={'expectedRevision':revision},context={'id':row['id']} if row else {})

    def _enrollment_form(self,state,data,row=None):
        fields=[] if row else [_field('programId','Программа',options=[(p['title'],p['id'])for p in data['programs']if not p['archived']]),_field('studentId','Ученик',options=[(p['name'],p['id'])for p in data['students']if not p['archived']])]
        fields += [_field('status','Участие',options=[('Активно','active'),('Пауза','paused'),('Завершено','completed')]),_field('note','Примечание',optional=True)]
        return self._form(state,'clubs','enrollment_update'if row else'enrollment_create',fields,values={'expectedRevision':data['revision'],**({'programId':row['programId'],'studentId':row['studentId']}if row else{})},context={'id':row['id']}if row else{})

    def _club_selected(self,actor,ns,operation,row,context,state,event):
        if operation=='share_choose_material': return self._confirm_action(state,'materials','share_create',{**context['values'],'materialId':row['id'],'expectedMaterialRevision':row['revision']},{},'Открыть выбранную версию «'+row['title']+'» для одного опубликованного урока?')
        if operation=='club_document':
            data=self._rpc(actor,'POST','/api/student/clubs/documents/prepare',{'documentId':row['id'],'revision':row['revision']},event=event,operation='document')
            guard={'kind':'club_document','documentId':row['id'],'revision':row['revision']}
            if data.get('file'):
                return self._pdf_result(data,guard)
            if data.get('status')=='ready': return self._pdf_result(data,guard)
            if data.get('status')in{'queued','running'}:
                state['documentJob']=data;return _reply('Документ поставлен в штатную очередь. Результат пока не готов.',[_button('Проверить готовность','/clubs document_status')])
            return _reply('Подготовка PDF пока не подтверждена. Документ не отправлен.')
        if operation=='reconcile_charge':
            return _reply(row['title']+' · '+row['studentName']+' · '+row['period']+f"\nНачислено {Decimal(row['amountKopeks'])/100:.2f} ₽, учтено {Decimal(row['receivedKopeks'])/100:.2f} ₽.\nЭто тестовые записи, не банковская выписка.",[_button('Связать тестовый счёт','/clubs bind_account'),_button('Учесть поступление','/clubs record_receipt')])
        if operation.startswith('club_'):
            kind=operation[5:];state['selectedKind']=kind
            lines=[_label(row)]
            for key in ('teacherName','scheduleNote','room','description','studentName','className','period','note'):
                if row.get(key):lines.append(str(row[key]))
            if row.get('status'):lines.append(STATUS.get(row['status'],row['status']))
            if kind=='charges':buttons=[_button('Подтвердить начисление','/clubs charge_confirm'),_button('Отменить начисление','/clubs charge_cancel')]
            else:buttons=[_button('Редактировать','/clubs '+('program_edit'if kind=='programs'else'enrollment_edit')),_button('Восстановить'if row['archived']else'Архивировать','/clubs '+('record_restore'if row['archived']else'record_archive'))]
            return _reply('\n'.join(lines),buttons)
        return _reply('Выберите доступное действие.',[_button('Меню','/'+ns)])

    def _club_action(self,actor,command,state,event,data):
        row=state.get('selected');context=state.get('context',{})
        if not row:return _reply('Сначала выберите запись в списке.',[_button('Кружки','/clubs')])
        if command=='program_edit':return self._program_form(state,data['revision'],row)
        if command=='enrollment_edit':return self._enrollment_form(state,data,row)
        if command in{'record_archive','record_restore','charge_confirm','charge_cancel'}:
            kind=state.get('selectedKind');action=command.split('_',1)[1]
            return self._confirm_action(state,'clubs','club_action',{'expectedRevision':data['revision']},{'kind':kind,'id':row['id'],'action':action},'Подтвердить действие с записью «'+_label(row)+'»?')
        if command=='bind_account':return self._form(state,'clubs','reconciliation_bind',[_field('accountRef','Код вымышленного лицевого счёта (например test-account-demo)')],values={'expectedRevision':context['revision'],'chargeId':row['id']})
        if command=='record_receipt':
            if not row.get('accountRef'):return _reply('Сначала свяжите подтверждённый тестовый счёт.',[_button('Связать счёт','/clubs bind_account')])
            return self._form(state,'clubs','reconciliation_apply',[_field('receiptRef','Код поступления из тестовой выписки (например test-receipt-demo)'),_field('amountKopeks','Сумма поступления в рублях','money'),_field('receivedOn','Дата поступления: ГГГГ-ММ-ДД','date'),_field('sourceConfirmed','Поступление подтверждено тестовой выпиской?',options=[('Да, подтверждено',True)])],values={'expectedRevision':context['revision'],'chargeId':row['id'],'accountRef':row['accountRef']})
        return _reply('Выберите действие в меню.',[_button('Кружки','/clubs')])

    def _pdf_result(self,data,resource_guard=None):
        document = data.get('file')
        if document:
            if document.get('contentType') != 'application/pdf': raise ValueError()
            raw = base64.b64decode(document['base64'], validate=True)
            if data.get('sha256') is not None and sha256(raw).hexdigest() != data['sha256']: raise ValueError()
        else:
            raw = base64.b64decode(data['pdf'], validate=True)
            if sha256(raw).hexdigest() != data['sha256']: raise ValueError()
            document = {'name':data['name'],'contentType':'application/pdf','base64':data['pdf']}
        if len(raw)>8*1024*1024 or not raw.startswith(b'%PDF-'): raise ValueError()
        reply=_reply('Учебный документ подготовлен штатной службой. Это тестовый документ.',document=document)
        if resource_guard is not None:reply['resourceGuards']=[resource_guard]
        return reply

    def _execute(self,actor,op,values,context,event):
        body=dict(values);body['requestId']=_id(event,op)
        method='POST';path=None
        if op.startswith('material_'):
            path='/api/teacher/preparation/'+('generate'if op=='material_ai'else'materials')
            if op=='material_ai':body.pop('title',None)
            if op=='material_update':method='PATCH';path+='/'+quote(context['id'],safe='')
        elif op=='assignment_create':
            path='/api/teacher/learning/assignments'
            if body['generate']:body['instructions']=body.pop('content')
        elif op=='practice_create':path='/api/student/learning/practice'
        elif op.startswith('learning_'):
            action=op[9:];path=context['path']+'/'+('messages'if action in{'message','tutor'}else action)
            if action in{'message','tutor'}:body['askAi']=action=='tutor'
        elif op.startswith('share_'):
            path='/api/teacher/replacement-materials/'+({'request':'requests','create':'grants','revoke':'grants/'+quote(context.get('id',''),safe='')+'/revoke'}[op[6:]])
        elif op=='consultation_create':path='/api/teacher/consultations'
        elif op=='consultation_decision':path='/api/'+self._role(actor)+'/consultations/'+quote(context['id'],safe='')+'/decision'
        elif op=='journal_view':return self._journal(actor,values)
        elif op=='journal_create':
            path='/api/school/data-exchange/apply'
            body={**context['nativePayload'],'requestId':_id(event,op)}
        elif op.startswith(('program_','enrollment_')):
            kind='programs'if op.startswith('program_')else'enrollments';path='/api/school/clubs/'+kind
            if op.endswith('_update'):method='PATCH';path+='/'+quote(context['id'],safe='')
            body={'expectedRevision':body.pop('expectedRevision'),'record':{key:value for key,value in body.items()if key!='requestId'}}
        elif op=='charge_create':path='/api/school/clubs/charges';body.pop('requestId')
        elif op=='club_action':path='/api/school/clubs/'+context['kind']+'/'+quote(context['id'],safe='')+'/'+context['action'];body.pop('requestId')
        elif op in{'reconciliation_bind','reconciliation_apply'}:
            path='/api/director/club-reconciliation/'+('binding'if op.endswith('bind')else'apply')
            if op.endswith('bind'):body.pop('requestId')
        if path is None:raise ValueError()
        data=self._rpc(actor,method,path,body,event=event,operation=op+':write')
        if isinstance(data,dict)and data.get('status')=='ready'and'pdf'in data:return self._pdf_result(data)
        return _reply('Действие подтверждено штатной платформой. Откройте обновлённый список, чтобы увидеть результат.',[_button('Открыть раздел','/'+({'material':'materials','assignment':'learning','practice':'learning','learning':'learning','share':'materials','journal':'journal','consultation':'consultations'}.get(op.split('_')[0],'clubs')))])
