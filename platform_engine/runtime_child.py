"""Private JSON-line RPC host of the independently copied native platform core."""
from __future__ import annotations

import base64
from contextlib import ExitStack
from datetime import date, timedelta
import hashlib
import inspect
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import secrets
import re
import time
from urllib.parse import urlsplit, unquote, parse_qsl, urlencode, urlunsplit
from uuid import uuid5, NAMESPACE_URL

ENGINE = Path(__file__).resolve().parent
ORIGIN = 'https://ideyafix.internal'
PREFIX = '/client/school'
SCHOOL = 'ideyafix-synthetic'
PASSWORD = 'Independent fictional profile password2026!'
PROFILES = [
    {'id': 710001, 'role': 'teacher', 'name': 'Лера Демо', 'account': 'ifx.lera'},
    {'id': 710002, 'role': 'student', 'name': 'Тимур Демо', 'account': 'ifx.timur'},
    {'id': 710003, 'role': 'teacher', 'name': 'Марина Демо', 'account': 'ifx.teacher'},
    {'id': 710004, 'role': 'student', 'name': 'Саша Демо', 'account': 'ifx.student'},
    {'id': 710005, 'role': 'director', 'name': 'Роман Демо', 'account': 'ifx.director'},
    {'id': 710006, 'role': 'planner', 'name': 'Алекс Демо', 'account': 'ifx.planner'},
    {'id': 710007, 'role': 'student', 'name': 'Славик Демо', 'account': 'ifx.slavik'},
    {'id': 710008, 'role': 'platform_admin', 'name': 'Администратор Идеяфикс', 'account': 'ifx.owner'},
    {'id': 710009, 'role': 'access_admin', 'name': 'Доступ Демо', 'account': 'ifx.access'},
]


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def forbidden_network(*args, **kwargs):
    raise RuntimeError('platform_engine_network_disabled')


def configuration(root,nanogpt_config=None):
    os.umask(0o077)
    os.environ.clear()
    os.environ.update({
        'PATH': os.defpath, 'HOME': str(root), 'TMPDIR': str(root / 'tmp'),
        'PYTHON_DOTENV_DISABLED': '1', 'PYTHONDONTWRITEBYTECODE': '1',
        'PYTHONPATH': str(ENGINE), 'SHKOLA_WORKSPACE_MODE': 'school',
        'SHKOLA_SCHOOL_ID': SCHOOL, 'SHKOLA_DB_PATH': str(root / 'domain.sqlite3'),
        'AGENT_DB_PATH': str(root / 'agent.sqlite3'),
        'SHKOLA_STAFF_DB_PATH': str(root / 'staff.sqlite3'),
        'SHKOLA_REQUEST_AI_DB_PATH': str(root / 'request-ai.sqlite3'),
        'SHKOLA_MAX_DB_PATH': str(root / 'disabled-max.sqlite3'),
        'SHKOLA_STAFF_ORIGIN': ORIGIN, 'SHKOLA_STAFF_BASE_PATH': PREFIX,
        'SHKOLA_UI_DIRECTORY': str(ENGINE / 'internal_ui'),
        'NANOGPT_ALLOW_REQUESTS': 'false', 'NANOGPT_API_KEY': '', 'NANOGPT_MODEL': '',
        'SHKOLA_REQUEST_AI_ENABLED': 'false', 'SHKOLA_MAX_ENABLED': 'false',
        'SHKOLA_MAX_SEND_ENABLED': 'false', 'LANGSMITH_TRACING': 'false',
        'LANGCHAIN_TRACING_V2': 'false',
    })
    (root / 'tmp').mkdir(mode=0o700, exist_ok=True)
    interpreter_roots = [Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()]
    sys.path[:] = [str(ENGINE), *[p for p in sys.path if p and any(
        Path(p).resolve().is_relative_to(base) for base in interpreter_roots)]]
    provider=None
    if nanogpt_config:
        from provider_config import load_config
        provider=load_config(nanogpt_config)
    socket.socket.connect = forbidden_network
    socket.create_connection = forbidden_network
    socket.getaddrinfo = forbidden_network
    allowed = [ENGINE, root, Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(),
               Path('/usr'), Path('/System'), Path('/Library'), Path('/proc'),
               Path('/usr/share/zoneinfo').resolve(), Path('/dev/null')]
    allowed.extend(Path(p).resolve() for p in sys.path if p)
    def audit(event, arguments):
        if event in {'socket.connect', 'socket.getaddrinfo'}:
            raise RuntimeError('platform_engine_network_disabled')
        if event in {'open', 'sqlite3.connect'} and arguments:
            raw = arguments[0]
            if isinstance(raw, (str, bytes, os.PathLike)) and raw not in {':memory:', b':memory:'}:
                raw_text = os.fsdecode(raw)
                path = Path(unquote(urlsplit(raw_text).path) if event=='sqlite3.connect' and raw_text.startswith('file:') else raw_text).resolve()
                if not any(path.is_relative_to(base) for base in allowed):
                    globals()['DENIED_BASENAME'] = path.name
                    raise RuntimeError('platform_engine_external_file_access_disabled')
    sys.addaudithook(audit)
    return provider


def closure(function):
    return dict(zip(function.__code__.co_freevars,
                    (cell.cell_contents for cell in function.__closure__ or ())))


class Host:
    @property
    def stage(self):
        return self._stage

    @stage.setter
    def stage(self, value):
        self._stage = value
        globals()['CURRENT_STAGE'] = value

    def __init__(self, root, provider=None):
        self.root = root
        self.provider=provider
        self.clients = {}
        self.stack = ExitStack()
        with self.registry() as database:
            database.execute('''CREATE TABLE IF NOT EXISTS profiles(
                actor_id INTEGER PRIMARY KEY,membership_id TEXT UNIQUE,username TEXT NOT NULL,
                password TEXT NOT NULL,claim_hash TEXT UNIQUE NOT NULL,nomination_id TEXT NOT NULL,
                state TEXT NOT NULL)''')
        self.stage = 'imports'
        from fastapi.testclient import TestClient
        from server import db, staff_access, school_ops
        from server.timetable import apply_batch, batch_hash, read_draft
        from server.staff_app import create_app
        from server.planning import PlanningService, template_input
        self.TestClient, self.staff = TestClient, staff_access
        self.stage = 'fixture'
        if not (root / 'seed-complete.json').exists():
            lessons = []
            for index, (class_name, teacher, room) in enumerate([
                    ('ИФ-7А', 'Марина Демо', 'Кабинет 101'), ('ИФ-8А', 'Олег Демо', 'Кабинет 102'),
                    ('ИФ-7А', 'Лера Демо', 'Кабинет 101')]):
                lessons.append({'id': f'ifx-lesson-{index+1}', 'className': class_name,
                    'day': 1 if index == 2 else 0, 'slot': 1 if index == 2 else index+1,
                    'start': '08:50' if index == 1 else '08:00',
                    'end': '09:30' if index == 1 else '08:40',
                    'subject': 'Информатика' if index == 2 else 'Математика',
                    'teachers': [teacher], 'rooms': [room], 'sourceCell': f'C{index+2}',
                    'raw': 'Вымышленная строка Идеяфикс', 'edited': False})
            if read_draft()['revision'] == 0:
                apply_batch(lessons, 0, {'role': 'director', 'userId': 'ifx-fixture'},
                    idempotency_key='ifx_initial_timetable',
                    expected_hash=batch_hash(lessons, 0, 'Идеяфикс — тестовые данные'),
                    source_name='Идеяфикс — тестовые данные')
            with db.edit_state() as state:
                state['school']['name'] = 'Идеяфикс · вымышленная организация'
                ops = school_ops._empty_ops()
                ops['records']['students'] = [
                    {'id': 'ifx-student-1', 'name': 'Саша Демо', 'className': 'ИФ-7А', 'archived': False},
                    {'id': 'ifx-student-2', 'name': 'Вика Демо', 'className': 'ИФ-8А', 'archived': False},
                    {'id': 'ifx-student-3', 'name': 'Славик Демо', 'className': 'ИФ-7А', 'archived': False},
                    {'id': 'ifx-student-4', 'name': 'Тимур Демо', 'className': 'ИФ-7А', 'archived': False}]
                ops['records']['teacherProfiles'] = [
                    {'id': 'ifx-teacher-1', 'name': 'Марина Демо', 'subject': 'Математика', 'available': True, 'archived': False},
                    {'id': 'ifx-teacher-2', 'name': 'Олег Демо', 'subject': 'Математика', 'available': True, 'archived': False},
                    {'id': 'ifx-teacher-3', 'name': 'Лера Демо', 'subject': 'Информатика', 'available': True, 'archived': False}]
                state['_schoolOps'] = ops
            plan = template_input('example')['input']
            plan['confirmed'] = True
            plan['groups'][0].update(className='ИФ-7А', name='ИФ-7А целиком')
            plan['groups'][1].update(className='ИФ-8А', name='ИФ-8А целиком')
            plan['teachers'][0]['name'], plan['teachers'][1]['name'] = 'Марина Демо', 'Олег Демо'
            plan['teachers'].append({**plan['teachers'][0], 'id':'t3', 'name':'Лера Демо', 'subjects':['Информатика','Математика']})
            plan['slots'].append({'id':'s3','day':1,'slot':1,'start':'08:00','end':'08:40'})
            plan['demands'].append({**plan['demands'][0], 'id':'d3', 'subject':'Информатика', 'teacherIds':['t3']})
            plan['rooms'][0]['name'], plan['rooms'][1]['name'] = 'Кабинет 101', 'Кабинет 102'
            plan['notes'] = 'Явно подтверждённая вымышленная модель Идеяфикс, не сведения реальной организации.'
            service = PlanningService(root / 'domain.sqlite3')
            service.save_config({'expectedRevision': 0, 'baseDraftRevision': 1, 'input': plan},
                                {'role': 'director', 'userId': 'ifx-fixture'})
            staff_access.bootstrap_director('ifx.director', 'Роман Демо', PASSWORD)
        self.stage = 'application'
        self.app = create_app()
        self.native = closure(self.app.app.handle.__func__)['app']
        from server import auth
        from local_extensions import install
        learning_endpoint = next(r.endpoint for r in self.native.routes if getattr(r,'path','')=='/api/teacher/learning')
        learning_context = closure(learning_endpoint)
        self.learning = learning_context['learning']
        current_session = closure(learning_context['checked_learning_result'])['require_current_session']
        self.documents = install(self.native, self.learning,
            self.native.dependency_overrides[auth.session_profile], current_session, root)
        self.owner_secret = secrets.token_urlsafe(32)
        secret_path = root / 'owner-bridge.key'
        fd = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(self.owner_secret)
        os.environ.update(SHKOLA_PORTAL_MODE='closed', SHKOLA_GATEWAY_SECRET_FILE=str(secret_path))
        from server import portal_app
        portal_app._OWNER_PROFILE = {**portal_app._OWNER_PROFILE, 'name':'Администратор Идеяфикс',
            'title':'Администратор платформы · вымышленная роль', 'initials':'ИФ'}
        self.owner_app = portal_app.create_app()
        self.owner_native = self.owner_app.app
        self.owner_client = self.stack.enter_context(TestClient(self.owner_app, base_url='http://localhost:8798',
            headers={'X-School-Bridge': self.owner_secret}))
        self.stage = 'login'
        director = self.client_for('ifx.director')
        if not (root / 'seed-complete.json').exists():
            for role, name, username in [('teacher', 'Марина Демо', 'ifx.teacher'),
                    ('planner', 'Алекс Демо', 'ifx.planner'), ('teacher','Лера Демо','ifx.lera'),
                    ('access_admin','Доступ Демо','ifx.access')]:
                value = self.post(director, '/api/staff/nominations', {
                    'username': username, 'name': name, 'role': role,
                    'teacherRefs': [name] if role == 'teacher' else [],
                    'calendarTeacherIds': ['t1' if username=='ifx.teacher' else 't3'] if role=='teacher' else []})
                invitation = self.post(director, '/api/staff/invites', {'nominationId': value['id']})
                client = self.stack.enter_context(TestClient(self.app, base_url=ORIGIN))
                self.post(client, '/api/staff/accept', {'token': invitation['token'], 'password': PASSWORD})
                self.clients[username] = client
            self.stage = 'student'
            for student_id, username in [('ifx-student-1','ifx.student'),('ifx-student-3','ifx.slavik'),('ifx-student-4','ifx.timur')]:
                invitation = self.post(director, '/api/staff/student-access/invitations', {
                    'requestId': 'ifx_synthetic_access_'+username.replace('.','_'), 'studentId': student_id, 'username': username})
                client = self.stack.enter_context(TestClient(self.app, base_url=ORIGIN))
                token = invitation['invite']['path'].split('#invite=', 1)[1]
                self.post(client, '/api/staff/accept', {'token': token, 'password': PASSWORD})
                self.clients[username] = client
            self.stage = 'calendar'
            monday = date.today() - timedelta(days=date.today().weekday())
            self.post(director, '/api/school/calendar/config', {'expectedRevision': 0,
                'config': {'timezone': 'Europe/Moscow', 'periodStart': monday.isoformat(),
                           'periodEnd': (monday + timedelta(days=27)).isoformat(),
                           'source': 'Явный вымышленный календарь Идеяфикс', 'confirmed': True}}, method='PATCH')
            for index in range(4):
                self.post(director, '/api/school/calendar/records/weeks', {
                    'expectedRevision': index+1, 'record': {'weekStart': (monday + timedelta(weeks=index)).isoformat(),
                    'schoolDays': [0, 1, 2, 3, 4], 'source': 'Вымышленная учебная неделя', 'confirmed': True}})
            self.stage = 'publication'
            run = self.post(director, '/api/planning/runs', {
                'expectedConfigRevision': 1, 'expectedDraftRevision': 1,
                'mode': 'generate', 'timeLimitSeconds': 2, 'requestId': 'ifx_initial_actual_solve_001'})
            deadline = time.monotonic()+40
            while run['status'] in {'queued','running'} and time.monotonic() < deadline:
                time.sleep(.05)
                run = director.get(PREFIX+'/api/planning/runs/'+run['id']).json()
            if run['status'] not in {'optimal','feasible'}:
                raise RuntimeError('initial_native_solver_not_ready')
            task = self.post(director, '/api/planning/runs/'+run['id']+'/propose', {})
            decision = self.post(director, '/api/schedule-agent/tasks/'+task['id']+'/decision', {'decision':'approve'})
            if decision['status'] != 'completed':
                raise RuntimeError('initial_native_schedule_not_approved')
            versions = {'runId':run['id'], 'expectedActiveVersion':0,
                        'expectedDraftRevision':read_draft()['revision']}
            preview = self.post(director, '/api/school/publications/preview', versions)
            if not preview['ready']:
                raise RuntimeError('initial_native_publication_not_ready')
            self.post(director, '/api/school/publications/publish', {
                **versions, 'previewHash':preview['previewHash'], 'requestId':'ifx_initial_actual_publication_001'})
            (root / 'seed-complete.json').write_text(canonical({'kind': 'ideyafix-platform-seed-v1'}))
        self.stage = 'journal'
        with self.journal() as database:
            database.execute('''CREATE TABLE IF NOT EXISTS gateway_receipts(
                event_id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,state TEXT NOT NULL,
                response TEXT)''')
            database.execute('''CREATE TABLE IF NOT EXISTS claim_events(
                event_id TEXT PRIMARY KEY,token_hash TEXT NOT NULL)''')
        with self.registry() as database:
            database.execute('''CREATE TABLE IF NOT EXISTS profiles(
                actor_id INTEGER PRIMARY KEY,membership_id TEXT UNIQUE,username TEXT NOT NULL,
                password TEXT NOT NULL,claim_hash TEXT UNIQUE NOT NULL,nomination_id TEXT NOT NULL,
                state TEXT NOT NULL)''')
        self.stage = 'ready'

    def journal(self):
        return sqlite3.connect(self.root / 'gateway.sqlite3', timeout=20)

    def registry(self):
        database=sqlite3.connect(self.root/'profiles.sqlite3',timeout=20)
        database.row_factory=sqlite3.Row
        return database

    def profile_catalog(self):
        profiles=[dict(item) for item in PROFILES]
        with self.registry() as registry, self.staff.connect() as native:
            for row in registry.execute("SELECT * FROM profiles WHERE state='active' ORDER BY actor_id"):
                member=native.execute('SELECT * FROM staff_members WHERE id=? AND school_id=? AND enabled=1',
                    (row['membership_id'],SCHOOL)).fetchone()
                if member is not None:
                    profiles.append({'id':row['actor_id'],'role':member['role'],'name':member['name'],
                                     'account':member['username']})
        return profiles

    def client_for(self, account):
        if account not in self.clients:
            client = self.stack.enter_context(self.TestClient(self.app, base_url=ORIGIN))
            self.sign_in(client, account)
            self.clients[account] = client
        return self.clients[account]

    def sign_in(self, client, account):
        with self.registry() as database:
            row = database.execute("SELECT password FROM profiles WHERE username=? AND state='active'",
                                   (account,)).fetchone()
        self.post(client, '/api/staff/login', {
            'username': account, 'password': row['password'] if row else PASSWORD,
        })

    def post(self, client, path, body, method='POST'):
        csrf = client.get(PREFIX + '/api/portal-session').json()['csrfToken']
        response = client.request(method, PREFIX+path, json=body,
                                  headers={'Origin': ORIGIN, 'X-CSRF-Token': csrf})
        if response.status_code != 200:
            raise RuntimeError('native_seed_' + str(response.status_code) + '_' + path.replace('/', '_'))
        return response.json()

    def profiles(self):
        return [{k: item[k] for k in ('id', 'name', 'role')} for item in self.profile_catalog()]

    def authorize(self,actor_id):
        """Uncached native authority check before any outer transcript replay."""
        profile=next((item for item in self.profile_catalog() if item['id']==actor_id),None)
        if profile is None:
            return {'status':403,'data':{'detail':'Личный доступ недоступен'},'file':None}
        owner=profile['role']=='platform_admin'
        try:
            client=self.owner_client if owner else self.client_for(profile['account'])
            # The owner uses the closed-portal bridge, while staff cookies expire.
            if owner:
                response = client.get('/api/school/workspace', headers={'Origin': 'http://localhost:8798'})
            else:
                identity_url = PREFIX + '/api/staff/me'
                response = client.get(identity_url, headers={'Origin': ORIGIN})
                if response.status_code == 401:
                    self.sign_in(client, profile['account'])
                    response = client.get(identity_url, headers={'Origin': ORIGIN})
        except RuntimeError as error:
            if str(error).startswith('native_seed_401_'):
                return {'status':401,'data':{'detail':'Личный доступ изменился'},'file':None}
            raise
        if response.status_code!=200:
            return {'status':response.status_code,'data':{'detail':'Личный доступ изменился'},'file':None}
        # A live membership alone does not authorize a pupil whose underlying
        # school record/binding was archived. Use the native personal-domain
        # reader; it does not require a schedule publication to exist.
        personal={'student':'/api/student/learning','teacher':'/api/teacher/preparation'}.get(profile['role'])
        if personal:
            current=client.get(PREFIX+personal,headers={'Origin':ORIGIN})
            if current.status_code!=200:
                return {'status':current.status_code,'data':{'detail':'Личные данные больше недоступны'},'file':None}
        return {'status':200,'data':{'profile':{key:profile[key] for key in ('id','role','name')}},'file':None}

    def _activate_claim(self, pending, token_hash):
        """Read-back proves a prior accept without replaying the native mutation."""
        if pending is None:
            return None
        with self.staff.connect() as database:
            invitation=database.execute('''SELECT i.consumed,n.used,n.id,n.username
                FROM staff_invites i JOIN staff_nominations n ON n.id=i.nomination_id
                WHERE i.token_hash=? AND n.school_id=?''',(token_hash,SCHOOL)).fetchone()
            if (invitation is None or not invitation['consumed'] or not invitation['used']
                    or invitation['id']!=pending['nomination_id'] or invitation['username']!=pending['username']):
                return None
            member=database.execute('SELECT * FROM staff_members WHERE username=? AND school_id=? AND enabled=1',
                (pending['username'],SCHOOL)).fetchone()
            if member is None or not self.staff.password_matches(pending['password'],member['password_hash']):
                return None
            native=self.staff.profile(member)
            if native.get('membershipVerified') is not True or native['staffRole'] not in self.staff.ROLES:
                return None
        with self.registry() as database:
            database.execute("UPDATE profiles SET state='active',membership_id=? WHERE actor_id=?",
                             (native['membershipId'],pending['actor_id']))
        return {'id':pending['actor_id'],'name':native['name'],'role':native['staffRole']}

    def claim_native_invite(self, capability, event_id):
        if (not isinstance(capability,str) or not re.fullmatch(r'[A-Za-z0-9_-]{43,128}',capability)
                or not isinstance(event_id,str) or not 1<=len(event_id)<=200 or any(ord(c)<33 for c in event_id)):
            return {'status':422,'data':{'detail':'Некорректное приглашение'},'file':None}
        token_hash=self.staff.digest(capability)
        key='native-invite:'+token_hash
        fingerprint=hashlib.sha256(('native-invite:'+token_hash).encode()).hexdigest()
        with self.journal() as database:
            database.execute('BEGIN IMMEDIATE')
            event=database.execute('SELECT token_hash FROM claim_events WHERE event_id=?',(event_id,)).fetchone()
            if event and event[0]!=token_hash:
                return {'status':409,'data':{'detail':'ID события использован для другого приглашения'},'file':None}
            database.execute('INSERT OR IGNORE INTO claim_events VALUES (?,?)',(event_id,token_hash))
            previous=database.execute('SELECT state,response FROM gateway_receipts WHERE event_id=?',(key,)).fetchone()
            if previous is None:
                database.execute("INSERT INTO gateway_receipts VALUES (?,?,'started',NULL)",(key,fingerprint))
        with self.registry() as database:
            pending=database.execute('SELECT * FROM profiles WHERE claim_hash=?',(token_hash,)).fetchone()
        if previous is not None:
            if previous[0]=='done':
                cached=json.loads(previous[1])
                if cached['status']==200 and self._activate_claim(pending,token_hash) is None:
                    return {'status':403,'data':{'detail':'Принятый личный доступ изменился'},'file':None}
                return cached
            recovered=self._activate_claim(pending,token_hash)
            if recovered is None:
                return {'status':409,'data':{'detail':'Результат приёма неизвестен; повтор отключён','uncertain':True},'file':None}
            result={'status':200,'data':{'accepted':True,'profile':recovered},'file':None}
        else:
            with self.staff.connect() as database:
                nomination=database.execute('''SELECT n.id,n.username FROM staff_invites i
                    JOIN staff_nominations n ON n.id=i.nomination_id WHERE i.token_hash=? AND n.school_id=?''',
                    (token_hash,SCHOOL)).fetchone()
            if nomination is not None and pending is None:
                with self.registry() as database:
                    database.execute('BEGIN IMMEDIATE')
                    if database.execute('SELECT COUNT(*) FROM profiles').fetchone()[0]>=500:
                        raise RuntimeError('platform_profile_limit')
                    actor=max(720000,database.execute('SELECT COALESCE(MAX(actor_id),0) FROM profiles').fetchone()[0])+1
                    database.execute("INSERT INTO profiles VALUES (?,NULL,?,?,?,?,'pending')",
                        (actor,nomination['username'],secrets.token_urlsafe(32),token_hash,nomination['id']))
                    pending=database.execute('SELECT * FROM profiles WHERE claim_hash=?',(token_hash,)).fetchone()
            client=self.stack.enter_context(self.TestClient(self.app,base_url=ORIGIN))
            csrf=client.get(PREFIX+'/api/portal-session').json()['csrfToken']
            response=client.post(PREFIX+'/api/staff/accept',json={
                'token':capability,'password':pending['password'] if pending else secrets.token_urlsafe(32)},
                headers={'Origin':ORIGIN,'X-CSRF-Token':csrf})
            if response.status_code!=200:
                result={'status':response.status_code,'data':{'detail':'Приглашение недействительно, истекло или доступ изменился'},'file':None}
            else:
                accepted=self._activate_claim(pending,token_hash)
                if accepted is None:
                    raise RuntimeError('native_accepted_profile_not_verified')
                self.clients[pending['username']]=client
                result={'status':200,'data':{'accepted':True,'profile':accepted},'file':None}
        with self.journal() as database:
            database.execute("UPDATE gateway_receipts SET state='done',response=? WHERE event_id=? AND state='started'",
                             (canonical(result),key))
        return result

    def diagnostics(self):
        modules=[]
        for name,module in sorted(sys.modules.items()):
            if name=='server' or name.startswith(('server.','scripts.')):
                source=getattr(module,'__file__',None)
                if source is not None:
                    path=Path(source).resolve()
                    if not path.is_relative_to(ENGINE):raise RuntimeError('foreign_engine_import')
                    modules.append({'name':name,'path':str(path.relative_to(ENGINE))})
        return {'backend':'independent-native-core','loadedModules':modules,'networkDisabled':True,
                'providerCredentialsPresent':any(os.environ.get(key) for key in (
                    'NANOGPT_API_KEY','SHKOLA_MAX_BOT_TOKEN','OPENAI_API_KEY')),
                'sharedEnvironment':False,'nativeStaffSessions':True,'nativeOwnerBoundary':True,
                'documentBackend':'independent-local-document-jobs',
                'nanogpt':{'configured':self.provider is not None,'requestsEnabled':False,
                           'model':self.provider['model'] if self.provider else None}}

    def schema(self):
        from server.calendar_models import MODELS
        from server.school_ops import MODELS as RECORD_MODELS, SettingsInput
        from server.school_plan_sources import SourceResolutions
        value = json.loads(canonical(self.native.openapi()))
        owner = json.loads(canonical(self.owner_native.openapi()))
        # Prefix owner components before merging: native apps can use the same
        # short model name for distinct staff and owner DTOs.
        names = set(owner.get('components',{}).get('schemas',{}))
        def refs(item):
            if isinstance(item,dict):
                for key,value in list(item.items()):
                    if key=='$ref' and isinstance(value,str) and value.startswith('#/components/schemas/'):
                        name=value.rsplit('/',1)[-1]
                        if name in names:item[key]='#/components/schemas/Owner__'+name
                    else:refs(value)
            elif isinstance(item,list):
                for value in item:refs(value)
        refs(owner)
        owner['components']['schemas']={'Owner__'+key:value for key,value in owner['components']['schemas'].items()}
        for path, methods in owner['paths'].items():
            value['paths'].setdefault(path, {}).update(methods)
        components = value.setdefault('components', {}).setdefault('schemas', {})
        components.update(owner.get('components', {}).get('schemas', {}))
        for model in MODELS.values():
            schema = model.model_json_schema(ref_template='#/components/schemas/{model}')
            components.update(schema.pop('$defs', {}))
            components[model.__name__] = schema
        value['x-platform-profiles'] = self.profiles()
        value['x-record-models'] = {
            '/api/school/calendar': {kind:model.model_json_schema() for kind,model in MODELS.items()},
            '/api/school/records': {kind:model.model_json_schema() for kind,model in RECORD_MODELS.items()}}
        value['x-settings-model'] = SettingsInput.model_json_schema()
        value['x-source-resolutions-model'] = SourceResolutions.model_json_schema()
        value['x-native-scopes'] = {'staffPaths': list(self.native.openapi()['paths']),
                                  'ownerPaths': list(owner['paths'])}
        value['x-inactive-other-mode'] = ['/api/state','/api/finance','/api/tasks','/api/documents',
            '/api/assignments','/api/lessons','/api/materials','/api/replacements','/api/notifications',
            '/api/demo','/api/assistant']
        return value

    def upload(self, actor_id, method, path, document, event_id):
        if type(document) is not dict or set(document) != {'name', 'contentType', 'base64'}:
            return {'status': 422, 'data': {'detail': 'Некорректный документ'}, 'file': None}
        name = document['name']
        if (not isinstance(name, str) or not 1 <= len(name) <= 160 or any(c in name for c in '/\\\r\n\x00')
                or name in {'.', '..'} or len(document.get('base64', '')) > 12*1024*1024):
            return {'status': 422, 'data': {'detail': 'Некорректное имя или размер документа'}, 'file': None}
        if method != 'POST' or urlsplit(path).path not in {'/api/timetable/preview', '/api/timetable/import'}:
            return {'status': 422, 'data': {'detail': 'Маршрут не принимает файл'}, 'file': None}
        if (not isinstance(document['contentType'],str) or len(document['contentType'])>200
                or any(ord(c)<32 or ord(c)>126 for c in document['contentType'])):
            return {'status':422,'data':{'detail':'Некорректный тип документа'},'file':None}
        split=urlsplit(path)
        query=parse_qsl(split.query,keep_blank_values=True)
        filenames=[value for key,value in query if key=='filename']
        if filenames and filenames!=[name]:
            return {'status':422,'data':{'detail':'Имя документа не совпадает с запросом'},'file':None}
        if not filenames:
            query.append(('filename',name))
            path=urlunsplit((split.scheme,split.netloc,split.path,urlencode(query),split.fragment))
        try:
            raw = base64.b64decode(document['base64'], validate=True)
        except (ValueError, TypeError):
            return {'status': 422, 'data': {'detail': 'Некорректный файл'}, 'file': None}
        if len(raw) > 8*1024*1024:
            return {'status': 413, 'data': {'detail': 'Файл слишком большой'}, 'file': None}
        return self.request(actor_id, method, path, payload={'$file': document}, event_id=event_id, _raw=raw)

    def cached_learning_authority(self,client,path,cached):
        """A mutation receipt cannot outlive native access to its assignment.

        Read only: never re-POST a completed operation to rediscover permission.
        A still-authorized historical receipt keeps its original revision/body.
        """
        scope=re.fullmatch(r'/api/(teacher|student)/learning/(?:assignments(?:/([^/]+)/(?:messages|draft|submit|review))?|practice)',path)
        if scope is None or not 200<=cached.get('status',500)<300:
            return None
        data=cached.get('data')
        assignment=data.get('assignment') if isinstance(data,dict) else None
        identifier=assignment.get('id') if isinstance(assignment,dict) else None
        if (not isinstance(identifier,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',identifier)
                or scope.group(2) is not None and scope.group(2)!=identifier):
            return {'status':409,'data':{'detail':'Квитанция личного задания требует проверки'},'file':None}
        response=client.get(PREFIX+'/api/'+scope.group(1)+'/learning/assignments/'+identifier,
                            headers={'Origin':ORIGIN})
        if response.status_code!=200:
            return {'status':response.status_code,'data':{'detail':'Личное задание больше недоступно'},'file':None}
        return None

    def request(self, actor_id, method, path, payload=None, event_id=None, _raw=None):
        profile = next((p for p in self.profile_catalog() if p['id'] == actor_id), None)
        if profile is None:
            return {'status': 403, 'data': {'detail': 'Неизвестная тестовая роль'}, 'file': None}
        method = method.upper()
        split = urlsplit(path)
        if (method not in {'GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE'} or split.scheme or split.netloc
                or not split.path.startswith('/api/') or '#' in path or '\\' in path or len(path)>4096):
            return {'status': 422, 'data': {'detail': 'Недопустимый внутренний маршрут'}, 'file': None}
        if split.path in {'/api/portal-session', '/api/staff/login', '/api/staff/accept', '/api/staff/logout'}:
            return {'status': 403, 'data': {'detail': 'Сеансом управляет внутренний шлюз'}, 'file': None}
        owner = profile['role'] == 'platform_admin'
        try:
            client = self.owner_client if owner else self.client_for(profile['account'])
        except RuntimeError as error:
            if str(error).startswith('native_seed_401_'):
                return {'status':401,'data':{'detail':'Личный доступ изменился'},'file':None}
            raise
        authorized=self.authorize(actor_id)
        if authorized['status']!=200:
            return authorized
        write = method not in {'GET', 'HEAD'}
        fingerprint = hashlib.sha256(canonical([actor_id, method, path, payload]).encode()).hexdigest()
        if write:
            if not isinstance(event_id, str) or not 1 <= len(event_id) <= 200 or any(ord(c)<33 for c in event_id):
                return {'status': 422, 'data': {'detail': 'Для изменения нужен стабильный ID события'}, 'file': None}
            with self.journal() as database:
                database.execute('BEGIN IMMEDIATE')
                previous = database.execute('SELECT fingerprint,state,response FROM gateway_receipts WHERE event_id=?',
                                            (event_id,)).fetchone()
                if previous:
                    if previous[0] != fingerprint:
                        return {'status': 409, 'data': {'detail': 'ID события использован с другим содержимым'}, 'file': None}
                    if previous[1] != 'done':
                        return {'status': 409, 'data': {'detail': 'Результат начатой операции неизвестен; повтор отключён', 'uncertain': True}, 'file': None}
                    cached = json.loads(previous[2])
                    denied=self.cached_learning_authority(client,split.path,cached)
                    if denied is not None:
                        return denied
                    if (cached.get('file') is not None and split.path=='/api/student/clubs/documents/prepare'):
                        current = client.get(PREFIX+'/api/student/clubs')
                        if current.status_code != 200:
                            return {'status':current.status_code,'data':{'detail':'Личный документ больше недоступен'},'file':None}
                        item = next((item for item in current.json().get('documents',[])
                            if item['id']==payload.get('documentId')),None)
                        if item is None:
                            return {'status':404,'data':{'detail':'Личный документ больше недоступен'},'file':None}
                        if item['revision']!=payload.get('revision'):
                            return {'status':409,'data':{'detail':'Начисление изменилось'},'file':None}
                    return cached
                database.execute("INSERT INTO gateway_receipts VALUES (?,?,'started',NULL)", (event_id, fingerprint))
        headers = {'Origin': 'http://localhost:8798' if owner else ORIGIN}
        if write:
            if not owner:
                headers['X-CSRF-Token'] = client.get(PREFIX+'/api/portal-session').json()['csrfToken']
            payload = dict(payload or {})
            # Native schemas decide whether a command/request identifier exists.
            schema = self.owner_native.openapi() if owner else self.native.openapi()
            for template, methods in schema['paths'].items():
                pattern = re.sub(r'\{[^/]+\}', '[^/]+', template)
                if re.fullmatch(pattern, split.path) and method.lower() in methods:
                    fields = methods[method.lower()].get('requestBody', {}).get('content', {}).get('application/json', {}).get('schema', {})
                    if '$ref' in fields:
                        fields = schema['components']['schemas'][fields['$ref'].split('/')[-1]]
                    stable = hashlib.sha256((str(actor_id)+':'+event_id+':'+method+':'+path).encode()).hexdigest()
                    for key in ('requestId', 'commandId'):
                        if key in fields.get('properties', {}) and key not in payload:
                            if key=='requestId' and split.path=='/api/schedule-agent/tasks' and payload.get('proposal') is not None:
                                continue
                            payload[key] = str(uuid5(NAMESPACE_URL, stable)) if fields['properties'][key].get('format') == 'uuid' else 'ifx_'+stable[:48]
                    break
        from server.student_learning import bound_service
        # Owner-side journal import uses the same authoritative personal-learning
        # service to prove publication/group membership. This is a service scope,
        # not an identity override; native owner authentication stays unchanged.
        with bound_service(self.learning):
            if _raw is not None:
                # The native XLSX parser deliberately accepts raw octet-stream only;
                # the source document's advertised MIME remains fingerprint metadata.
                headers['Content-Type'] = 'application/octet-stream'
                response = client.request(method, path if owner else PREFIX+path, content=_raw, headers=headers)
            else:
                response = client.request(method, path if owner else PREFIX+path, json=payload if write else None, headers=headers)
        content_type = response.headers.get('content-type', '')
        result = {'status': response.status_code, 'data': None, 'file': None}
        if 'attachment' in response.headers.get('content-disposition','') and response.content:
            name=response.headers['content-disposition'].split('filename=')[-1].strip('"')
            if not name or '/' in name or '\\' in name or any(ord(c)<32 for c in name):name='platform-export.bin'
            result['file']={'name':name,'contentType':content_type,
                            'base64':base64.b64encode(response.content).decode('ascii')}
        elif 'json' in content_type:
            result['data'] = response.json()
            if (isinstance(result['data'],dict) and result['data'].get('status')=='ready'
                    and isinstance(result['data'].get('pdf'),str)):
                result['file']={'name':result['data'].get('name','club-document.pdf'),
                    'contentType':'application/pdf','base64':result['data'].pop('pdf')}
        elif response.content:
            if content_type.startswith('text/') and 'csv' not in content_type and 'attachment' not in response.headers.get('content-disposition',''):
                result['data'] = {'text': response.text}
            else:
                name = response.headers.get('content-disposition', '').split('filename=')[-1].strip('"')
                if not name or '/' in name or '\\' in name:
                    name = 'platform-document.pdf' if 'pdf' in content_type else 'platform-export.bin'
                result['file'] = {'name': name, 'contentType': content_type,
                                  'base64': base64.b64encode(response.content).decode('ascii')}
        if write:
            with self.journal() as database:
                database.execute("UPDATE gateway_receipts SET state='done',response=? WHERE event_id=? AND state='started'",
                                 (canonical(result), event_id))
        return result

    def close(self):
        self.stack.close()


def main():
    root = Path(sys.argv[1])
    protocol = sys.stdout
    sys.stdout = sys.stderr
    host = None
    def emit(value, rpc_id=None):
        protocol.write(canonical({'rpcId': rpc_id, **value})+'\n'); protocol.flush()
    try:
        provider=configuration(root,sys.argv[2] if len(sys.argv)>2 else None)
        host = Host(root,provider)
        emit({'ok': True, 'value': {'ready': True}})
    except Exception as error:
        import traceback
        frame = traceback.extract_tb(error.__traceback__)[-1]
        emit({'ok': False, 'error': type(error).__name__ + ':' + globals().get('CURRENT_STAGE','startup')
              + ':' + Path(frame.filename).name + ':' + str(frame.lineno)
              + (':' + globals()['DENIED_BASENAME'] if 'DENIED_BASENAME' in globals() else '')})
        return 1
    try:
        for line in sys.stdin:
            rpc_id = None
            try:
                value = json.loads(line)
                rpc_id = value['rpcId']
                if type(rpc_id) is not int or rpc_id < 1:
                    raise ValueError('invalid_rpc_id')
                operation = value['operation']
                if operation == 'close':
                    emit({'ok': True, 'value': None}, rpc_id); break
                if operation == 'profiles': result = host.profiles()
                elif operation == 'schema': result = host.schema()
                elif operation == 'upload': result = host.upload(**value['arguments'])
                elif operation == 'diagnostics': result = host.diagnostics()
                elif operation == 'claim_native_invite': result = host.claim_native_invite(**value['arguments'])
                elif operation == 'authorize': result = host.authorize(**value['arguments'])
                elif operation == 'request': result = host.request(**value['arguments'])
                else: raise ValueError('unsupported_operation')
                emit({'ok': True, 'value': result}, rpc_id)
            except Exception as error:
                import traceback
                frames=traceback.extract_tb(error.__traceback__)[-3:]
                emit({'ok': False, 'error': type(error).__name__+':'+';'.join(
                    Path(frame.filename).name+':'+str(frame.lineno) for frame in frames)}, rpc_id)
    finally:
        host.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
