"""Explicit single-school staff identities. No demo identities or MAX login codes."""
from contextlib import closing,contextmanager
from contextvars import ContextVar
import hashlib,hmac,json,os,re,secrets,sqlite3,time,stat
from pathlib import Path
from uuid import uuid4
from datetime import date,timedelta
from fastapi import HTTPException
STAFF_ROLES={'director','planner','teacher','access_admin'}
ROLES=STAFF_ROLES|{'student'}
PERMISSIONS={
 'director':['schedule.read','schedule.manage','staff.manage','staff.nominate','launch.read','launch.manage','launch.status','publication.read','publication.publish'],
 'planner':['schedule.read','schedule.manage','launch.read','launch.manage','launch.status','publication.read','publication.publish'],
 'teacher':['schedule.own','launch.read','launch.answer_own','launch.status'],
 'access_admin':['staff.manage','launch.status'],
 'student':['learning.own']}
SESSION_TTL=8*3600;INVITE_TTL=48*3600
_BOUND=ContextVar('staff_configuration',default=None)
_REGISTRY={}
@contextmanager
def bound_configuration(config):
 token=_BOUND.set(config)
 try:yield
 finally:_BOUND.reset(token)

def register_configuration(config):
 path,school=config;old=_REGISTRY.get(school)
 if old and old!=path and old.exists():raise RuntimeError('School identity already registered with a different staff DB')
 _REGISTRY[school]=path


def configuration():
 bound=_BOUND.get()
 raw=bound[0]if bound else os.environ.get('SHKOLA_STAFF_DB_PATH','');path=Path(raw);school=bound[1]if bound else os.environ.get('SHKOLA_SCHOOL_ID','')
 if not path.is_absolute() or '..'in path.parts or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',school):raise RuntimeError('Explicit staff DB and school configuration required')
 current=Path(path.anchor)
 for part in path.parts[1:]:
  current=current/part
  if current.is_symlink():raise RuntimeError('Staff DB symlinks forbidden')
 for variable in ('SHKOLA_DB_PATH','AGENT_DB_PATH'):
  value=os.environ.get(variable)
  if value:
   other=Path(value)
   if path.resolve()==other.resolve()or(path.exists()and other.exists()and os.path.samefile(path,other)):raise RuntimeError('Staff DB must be separate from domain and agent')
 if path.exists():
  info=path.stat()
  if not stat.S_ISREG(info.st_mode)or stat.S_IMODE(info.st_mode)!=0o600 or info.st_uid!=os.geteuid()or info.st_nlink!=1:raise RuntimeError('Private owned regular0600 staff DB required')
 return path,school

def connect():
 path,_=configuration();c=sqlite3.connect(path,timeout=10);c.row_factory=sqlite3.Row;c.execute('PRAGMA foreign_keys=ON');return c

_SCHEMA=(
 "CREATE TABLE IF NOT EXISTS staff_meta(school_id TEXT PRIMARY KEY)",
 "CREATE TABLE IF NOT EXISTS staff_members(id TEXT PRIMARY KEY,school_id TEXT NOT NULL,username TEXT UNIQUE NOT NULL,name TEXT NOT NULL,role TEXT NOT NULL,teacher_refs TEXT NOT NULL,calendar_teacher_ids TEXT NOT NULL,password_hash TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,revision INTEGER NOT NULL DEFAULT 1)",
 "CREATE TABLE IF NOT EXISTS staff_nominations(id TEXT PRIMARY KEY,school_id TEXT NOT NULL,username TEXT NOT NULL,name TEXT NOT NULL,role TEXT NOT NULL,teacher_refs TEXT NOT NULL,calendar_teacher_ids TEXT NOT NULL,approved_by TEXT NOT NULL,used INTEGER NOT NULL DEFAULT 0)",
 "CREATE TABLE IF NOT EXISTS staff_invites(id TEXT PRIMARY KEY,nomination_id TEXT NOT NULL,token_hash TEXT UNIQUE NOT NULL,expires REAL NOT NULL,consumed INTEGER NOT NULL DEFAULT 0)",
 "CREATE TABLE IF NOT EXISTS staff_sessions(token_hash TEXT PRIMARY KEY,member_id TEXT,csrf TEXT NOT NULL,expires REAL NOT NULL,revision INTEGER NOT NULL DEFAULT 0)",
 "CREATE TABLE IF NOT EXISTS staff_audit(id INTEGER PRIMARY KEY AUTOINCREMENT,at REAL NOT NULL,actor TEXT NOT NULL,action TEXT NOT NULL,target TEXT NOT NULL)",
 "CREATE TABLE IF NOT EXISTS staff_limits(bucket TEXT PRIMARY KEY,started REAL NOT NULL,count INTEGER NOT NULL)",
 "CREATE TABLE IF NOT EXISTS staff_revocations(id TEXT PRIMARY KEY,member_id TEXT NOT NULL,approved_by TEXT NOT NULL,consumed INTEGER NOT NULL DEFAULT 0)",
 "CREATE TABLE IF NOT EXISTS staff_absences(id TEXT PRIMARY KEY,member_id TEXT NOT NULL,payload TEXT NOT NULL,created REAL NOT NULL)")
_SCHEMA_COLUMNS={
 'staff_meta':['school_id'],'staff_members':['id','school_id','username','name','role','teacher_refs','calendar_teacher_ids','password_hash','enabled','revision'],
 'staff_nominations':['id','school_id','username','name','role','teacher_refs','calendar_teacher_ids','approved_by','used'],
 'staff_invites':['id','nomination_id','token_hash','expires','consumed'],'staff_sessions':['token_hash','member_id','csrf','expires','revision'],
 'staff_audit':['id','at','actor','action','target'],'staff_limits':['bucket','started','count'],
 'staff_revocations':['id','member_id','approved_by','consumed'],'staff_absences':['id','member_id','payload','created']}

def _validate_existing(c,school):
 if c.execute("SELECT 1 FROM sqlite_master WHERE type IN ('view','trigger') LIMIT 1").fetchone():raise RuntimeError('Unexpected staff schema executable objects')
 tables={r[0]for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
 if 'staff_meta'not in tables or not tables<=set(_SCHEMA_COLUMNS)|{'sqlite_sequence'}:raise RuntimeError('Existing file is not an exclusively staff DB')
 for name in tables-{'sqlite_sequence'}:
  columns=[r[1]for r in c.execute('PRAGMA table_info('+name+')')]
  if columns!=_SCHEMA_COLUMNS[name]:raise RuntimeError('Unexpected staff schema')
 if [r[0]for r in c.execute('SELECT school_id FROM staff_meta')]!=[school]:raise RuntimeError('Staff DB belongs to another school')
 for name in ('staff_members','staff_nominations'):
  if name in tables and c.execute('SELECT 1 FROM '+name+' WHERE school_id<>? LIMIT 1',(school,)).fetchone():raise RuntimeError('Foreign school membership in staff DB')

def initialize():
 path,school=configuration();path.parent.mkdir(parents=True,exist_ok=True)
 parent=path.parent.stat()
 if parent.st_uid!=os.geteuid()or parent.st_mode&0o022:raise RuntimeError('Private owned staff DB directory required')
 new=not path.exists()
 if new:
  fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600);os.close(fd)
 else:
  try:
   with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True))as c:_validate_existing(c,school)
  except sqlite3.Error:raise RuntimeError('Invalid existing staff DB')from None
 with closing(connect())as c:
  try:
   c.execute('BEGIN IMMEDIATE')
   if not new:_validate_existing(c,school)
   for statement in _SCHEMA:c.execute(statement)
   c.execute('INSERT OR IGNORE INTO staff_meta VALUES(?)',(school,));c.commit()
  except BaseException:c.rollback();raise

def digest(value):return hashlib.sha256(value.encode()).hexdigest()
def _current_actor(c,actor):
 row=c.execute('SELECT * FROM staff_members WHERE id=? AND school_id=?',(actor.get('membershipId'),actor.get('schoolId'))).fetchone()
 if not row or not row['enabled']or row['revision']!=actor.get('membershipRevision')or row['role']!=actor.get('staffRole')or 'staff:'+row['id']!=actor.get('userId'):raise HTTPException(401,'Доступ изменился')
 return row

def audit(c,actor,action,target):c.execute('INSERT INTO staff_audit(at,actor,action,target) VALUES(?,?,?,?)',(time.time(),actor,action,target))
def password_hash(password):
 if not isinstance(password,str)or not 12<=len(password)<=256:raise HTTPException(422,'Пароль должен содержать от12 до256 символов')
 salt=secrets.token_hex(16);value=hashlib.scrypt(password.encode(),salt=bytes.fromhex(salt),n=16384,r=8,p=1).hex();return salt+'$'+value

def password_matches(password,encoded):
 try:
  salt,value=encoded.split('$');actual=hashlib.scrypt(password.encode(),salt=bytes.fromhex(salt),n=16384,r=8,p=1).hex();return hmac.compare_digest(value,actual)
 except(ValueError,TypeError,AttributeError):return False

def limited(kind,key,limit=20):
 with closing(connect())as c:
  c.execute('BEGIN IMMEDIATE');bucket=digest(kind+':'+key);now=time.time();r=c.execute('SELECT * FROM staff_limits WHERE bucket=?',(bucket,)).fetchone()
  count=r['count']if r and r['started']>now-3600 else 0;start=r['started']if count else now
  if count>=limit:c.rollback();raise HTTPException(429,'Слишком много попыток. Повторите позже')
  c.execute('INSERT OR REPLACE INTO staff_limits VALUES(?,?,?)',(bucket,start,count+1));c.execute('DELETE FROM staff_limits WHERE started<?',(now-86400,));c.commit()

def profile(row):
 refs=json.loads(row['teacher_refs']);role=row['role'];return {'role':role,'staffRole':role,'schoolId':row['school_id'],'userId':'staff:'+row['id'],'membershipId':row['id'],'membershipRevision':row['revision'],'memberRevision':row['revision'],'membershipVerified':True,'teacherRefs':refs,'calendarTeacherIds':json.loads(row['calendar_teacher_ids']),'permissions':PERMISSIONS[role].copy(),'name':row['name'],'title':{'director':'Директор','planner':'Завуч','teacher':'Учитель','access_admin':'Администратор доступа','student':'Ученик'}[role],'initials':''.join(x[0]for x in row['name'].split()[:2]).upper()}

def membership_current(actor):
 if not isinstance(actor,dict)or not actor.get('membershipVerified'):return False
 resolved=resolve_membership(actor.get('schoolId'),actor.get('membershipId'))
 return bool(resolved and resolved['userId']==actor.get('userId')and resolved['membershipRevision']==actor.get('membershipRevision')and resolved['staffRole']==actor.get('staffRole',actor.get('role')))

def membership_status(actor):
 """Read-only authority verdict; absence of a local authority is not revocation."""
 if not isinstance(actor,dict)or actor.get('membershipVerified')is not True:return 'invalid'
 school=actor.get('schoolId');path=_REGISTRY.get(school)
 if not path or not path.is_file():return 'unverifiable'
 try:
  with bound_configuration((path,school)):
   safe_path,_=configuration()
   with closing(sqlite3.connect(safe_path.as_uri()+'?mode=ro',uri=True))as c:
    c.row_factory=sqlite3.Row
    _validate_existing(c,school)
    row=c.execute('SELECT * FROM staff_members WHERE id=? AND school_id=?',(actor.get('membershipId'),school)).fetchone()
   if not row or not row['enabled']:return 'invalid'
   expected=profile(row)
   fields=('userId','schoolId','membershipId','membershipRevision','role','staffRole','teacherRefs','calendarTeacherIds','permissions')
   return 'current'if all(actor.get(k)==expected.get(k)for k in fields)else'invalid'
 except(RuntimeError,OSError,sqlite3.Error,ValueError,TypeError,KeyError):return 'unverifiable'

def get_session(token):
 if not token or len(token)>128:return None
 with closing(connect())as c:
  s=c.execute('SELECT * FROM staff_sessions WHERE token_hash=? AND expires>?',(digest(token),time.time())).fetchone()
  if not s:return None
  if not s['member_id']:return {'csrf':s['csrf'],'profile':None}
  row=c.execute('SELECT * FROM staff_members WHERE id=?',(s['member_id'],)).fetchone()
  if not row or not row['enabled']or row['revision']!=s['revision']or row['school_id']!=configuration()[1]or row['role']not in ROLES:return None
  return {'csrf':s['csrf'],'profile':profile(row)}

def new_session(member=None,csrf=None):
 token=secrets.token_urlsafe(32);csrf=csrf or secrets.token_urlsafe(32)
 with closing(connect())as c:
  c.execute('BEGIN IMMEDIATE')
  if member:
   fresh=c.execute('SELECT * FROM staff_members WHERE id=?',(member['id'],)).fetchone()
   if not fresh or not fresh['enabled']or fresh['revision']!=member['revision']:raise HTTPException(401,'Доступ изменился')
  c.execute('DELETE FROM staff_sessions WHERE expires<?',(time.time(),))
  if c.execute('SELECT COUNT(*) FROM staff_sessions').fetchone()[0]>=2000:raise HTTPException(429,'Повторите позже')
  c.execute('INSERT INTO staff_sessions VALUES(?,?,?,?,?)',(digest(token),member['id']if member else None,csrf,time.time()+(SESSION_TTL if member else 600),member['revision']if member else 0));c.commit()
 return token,csrf

def destroy_session(token):
 with closing(connect())as c:
  row=c.execute('SELECT member_id FROM staff_sessions WHERE token_hash=?',(digest(token),)).fetchone()
  c.execute('DELETE FROM staff_sessions WHERE token_hash=?',(digest(token),))
  if row and row['member_id']:audit(c,row['member_id'],'logout',row['member_id'])
  c.commit()

def login(username,password,csrf):
 username=username.strip().lower()
 with closing(connect())as c:row=c.execute('SELECT * FROM staff_members WHERE username=?',(username,)).fetchone()
 # Always run scrypt, including nonexistent identities.
 encoded=row['password_hash']if row else '00'*16+'$'+'00'*64
 valid=password_matches(password,encoded)
 if not row or not row['enabled']or not valid:raise HTTPException(401,'Неверный логин или пароль')
 token,csrf=new_session(row)
 with closing(connect())as c:audit(c,row['id'],'login',row['id']);c.commit()
 return token,profile(row)

def nomination(actor,body):
 if actor['staffRole']!='director':raise HTTPException(403,'Только директор утверждает назначение')
 username=body['username'].strip().lower();name=body['name'].strip();role=body['role'];refs=body.get('teacherRefs',[]);calendar_ids=body.get('calendarTeacherIds',[])
 if not re.fullmatch(r'[a-z0-9][a-z0-9_.-]{2,79}',username)or not name or len(name)>120 or role not in STAFF_ROLES or len(refs)>20 or any(not isinstance(x,str)or not x.strip()or len(x)>160 for x in refs):raise HTTPException(422,'Проверьте сведения назначения')
 if len(calendar_ids)>20 or any(not isinstance(x,str)or not x or len(x)>160 for x in calendar_ids):raise HTTPException(422,'Проверьте нормативные идентификаторы педагога')
 if role!='teacher'and(refs or calendar_ids):raise HTTPException(422,'Привязка педагога доступна только учителю')
 if role=='teacher':
  if not refs:raise HTTPException(422,'Нужна явная привязка педагога')
  from .timetable import read_draft
  known={x for l in read_draft()['lessons']for x in l['teachers']}
  if not set(refs)<=known:raise HTTPException(422,'Привязка не подтверждена расписанием')
  validate_calendar_bindings(calendar_ids)
 identifier=str(uuid4())
 with closing(connect())as c:
  c.execute('BEGIN IMMEDIATE');_current_actor(c,actor)
  if c.execute('SELECT 1 FROM staff_members WHERE username=?',(username,)).fetchone():raise HTTPException(409,'Логин уже существует')
  c.execute('INSERT INTO staff_nominations VALUES(?,?,?,?,?,?,?,?,0)',(identifier,actor['schoolId'],username,name,role,json.dumps(list(dict.fromkeys(refs))),json.dumps(list(dict.fromkeys(calendar_ids))),actor['membershipId']));audit(c,actor['membershipId'],'nominate',identifier);c.commit()
 return {'id':identifier,'username':username,'name':name,'role':role,'teacherRefs':refs,'calendarTeacherIds':calendar_ids}

def create_student_nomination(c,actor,username,name,identifier):
 """Internal only: caller atomically persists the mandatory learning binding."""
 if actor.get('staffRole')!='director':raise HTTPException(403,'Только директор подтверждает ученика')
 _current_actor(c,actor)
 if not re.fullmatch(r'[a-z0-9][a-z0-9_.-]{2,79}',username)or not name or len(name)>120:raise HTTPException(422,'Проверьте логин и имя ученика')
 if c.execute('SELECT 1 FROM staff_members WHERE username=?',(username,)).fetchone()or c.execute('SELECT 1 FROM staff_nominations WHERE username=? AND used=0',(username,)).fetchone():raise HTTPException(409,'Логин уже занят или назначен')
 c.execute('INSERT INTO staff_nominations VALUES(?,?,?,?,?,?,?,?,0)',(identifier,actor['schoolId'],username,name,'student','[]','[]',actor['membershipId']));audit(c,actor['membershipId'],'nominate-student',identifier)

def invite_in_connection(c,actor,nomination_id):
 if actor['staffRole']not in{'director','access_admin'}:raise HTTPException(403,'Нет права приглашать')
 _current_actor(c,actor)
 row=c.execute('SELECT * FROM staff_nominations WHERE id=? AND school_id=?',(nomination_id,actor['schoolId'])).fetchone()
 if not row or row['used']:raise HTTPException(409,'Назначение недоступно')
 if row['role']=='student'and actor['staffRole']!='director':raise HTTPException(403,'Приглашение ученика требует директора')
 token=secrets.token_urlsafe(32);identifier=str(uuid4());expires=time.time()+INVITE_TTL
 c.execute('UPDATE staff_invites SET consumed=1 WHERE nomination_id=?',(nomination_id,));c.execute('INSERT INTO staff_invites VALUES(?,?,?,?,0)',(identifier,nomination_id,digest(token),expires));audit(c,actor['membershipId'],'invite',identifier)
 return {'id':identifier,'token':token,'expiresAt':expires,'path':'/client/school/#invite='+token}

def invite(actor,nomination_id):
 with closing(connect())as c:
  c.execute('BEGIN IMMEDIATE');result=invite_in_connection(c,actor,nomination_id);c.commit()
 return result

def accept(token,password,csrf,*,learning_connection=None):
 hashed=password_hash(password)
 with closing(connect())as c:
  c.execute('BEGIN IMMEDIATE');r=c.execute('SELECT n.*,i.id AS invite_id FROM staff_invites i JOIN staff_nominations n ON n.id=i.nomination_id WHERE i.token_hash=? AND i.consumed=0 AND i.expires>? AND n.used=0',(digest(token),time.time())).fetchone()
  if not r:raise HTTPException(400,'Приглашение недействительно или истекло')
  if r['role']=='student':
   from .student_learning import configured_service
   if not configured_service().validate_nomination(r['school_id'],r['id'],r['username'],connection=learning_connection):raise HTTPException(403,'Личная привязка ученика не подтверждена')
  if r['role']=='teacher':validate_calendar_bindings(json.loads(r['calendar_teacher_ids']),connection=learning_connection)
  approver=c.execute('SELECT role,enabled FROM staff_members WHERE id=?',(r['approved_by'],)).fetchone()
  if not approver or not approver['enabled']or approver['role']!='director':raise HTTPException(403,'Назначение больше не подтверждено')
  identifier=str(uuid4())
  try:c.execute('INSERT INTO staff_members VALUES(?,?,?,?,?,?,?,?,1,1)',(identifier,r['school_id'],r['username'],r['name'],r['role'],r['teacher_refs'],r['calendar_teacher_ids'],hashed))
  except sqlite3.IntegrityError:raise HTTPException(409,'Логин уже занят')from None
  if r['role']=='student':configured_service().activate_nomination(c,r['school_id'],r['id'],r['username'],identifier,connection=learning_connection)
  c.execute('UPDATE staff_invites SET consumed=1 WHERE id=?',(r['invite_id'],));c.execute('UPDATE staff_nominations SET used=1 WHERE id=?',(r['id'],));audit(c,identifier,'accept',r['invite_id']);c.commit();row=c.execute('SELECT * FROM staff_members WHERE id=?',(identifier,)).fetchone()
 t,_=new_session(row);return t,profile(row)

def revoke(actor,member_id):
 # Access administrators may execute only a separately approved revocation nomination.
 if actor['staffRole']!='director':raise HTTPException(403,'Отзыв требует решения директора')
 if member_id==actor['membershipId']:raise HTTPException(409,'Собственный доступ нельзя отозвать этим действием')
 with closing(connect())as c:
  c.execute('BEGIN IMMEDIATE');_current_actor(c,actor);r=c.execute('SELECT * FROM staff_members WHERE id=? AND school_id=?',(member_id,actor['schoolId'])).fetchone()
  if not r:raise HTTPException(404,'Сотрудник не найден')
  if r['role']=='director'and c.execute("SELECT COUNT(*) FROM staff_members WHERE role='director' AND enabled=1").fetchone()[0]<=1:raise HTTPException(409,'Нельзя отозвать последнего директора')
  c.execute('UPDATE staff_members SET enabled=0,revision=revision+1 WHERE id=?',(member_id,));c.execute('DELETE FROM staff_sessions WHERE member_id=?',(member_id,));audit(c,actor['membershipId'],'revoke',member_id);c.commit()
 return {'revoked':True,'membershipId':member_id}

def bootstrap_director(username,name,password):
 """Explicit operator-only call; never executed by app startup or HTTP."""
 initialize();hashed=password_hash(password);identifier=str(uuid4());school=configuration()[1]
 with closing(connect())as c:
  c.execute('BEGIN IMMEDIATE')
  if c.execute('SELECT COUNT(*) FROM staff_members').fetchone()[0]:raise ValueError('Bootstrap allowed only on empty staff DB')
  if not re.fullmatch(r'[a-z0-9][a-z0-9_.-]{2,79}',username):raise ValueError('Invalid username')
  c.execute('INSERT INTO staff_members VALUES(?,?,?,?,?,?,?,?,1,1)',(identifier,school,username,name,'director','[]','[]',hashed));audit(c,'operator','bootstrap',identifier);c.commit()
 return identifier

def resolve_membership(school_id,membership_id):
 path=_REGISTRY.get(school_id)
 if not path or not path.is_file():return None
 try:
  with bound_configuration((path,school_id)):
   with closing(connect())as c:r=c.execute('SELECT * FROM staff_members WHERE id=? AND school_id=? AND enabled=1',(membership_id,school_id)).fetchone()
   return profile(r)if r else None
 except(RuntimeError,OSError,sqlite3.Error):return None

def list_respondents(school_id):
 """Host-only bounded read; callers expose only minimum question-routing fields."""
 path=_REGISTRY.get(school_id)
 if not path or not path.is_file():return []
 try:
  with bound_configuration((path,school_id)):
   safe_path,_=configuration()
   with closing(sqlite3.connect(safe_path.as_uri()+'?mode=ro',uri=True))as c:
    c.row_factory=sqlite3.Row
    rows=c.execute('SELECT * FROM staff_members WHERE school_id=? AND enabled=1 ORDER BY name,id LIMIT 500',(school_id,)).fetchall()
   return [profile(row)for row in rows]
 except(RuntimeError,OSError,sqlite3.Error):return []

def directory(actor):
 if actor['staffRole']not in{'director','access_admin'}:raise HTTPException(403,'Доступ к сотрудникам закрыт')
 with closing(connect())as c:
  members=[{'id':r['id'],'username':r['username'],'name':r['name'],'role':r['role'],'enabled':bool(r['enabled']),'revision':r['revision'],'teacherRefs':json.loads(r['teacher_refs']),'calendarTeacherIds':json.loads(r['calendar_teacher_ids'])}for r in c.execute('SELECT * FROM staff_members WHERE school_id=?',(actor['schoolId'],))]
  nominations=[{'id':r['id'],'username':r['username'],'name':r['name'],'role':r['role'],'used':bool(r['used'])}for r in c.execute('SELECT * FROM staff_nominations WHERE school_id=?',(actor['schoolId'],))]
  revocations=[dict(r)for r in c.execute('SELECT r.id,r.member_id AS memberId,m.name FROM staff_revocations r JOIN staff_members m ON m.id=r.member_id WHERE r.consumed=0 AND m.school_id=?',(actor['schoolId'],))]
 return {'members':members,'nominations':nominations,'revocations':revocations,'roles':sorted(STAFF_ROLES)}

def own_timetable(actor,*,connection=None):
 if actor['staffRole']!='teacher':raise HTTPException(403,'Только собственное расписание учителя')
 from .school_publications import read_active_snapshot,teacher_snapshot_lessons
 # Teacher-facing reads never consult the mutable draft, including before first publication.
 published=read_active_snapshot(actor['schoolId'],connection);refs=actor.get('teacherRefs',[])
 base={'source':'publication','publicationState':'published'if published else'required','publicationVersion':published['version']if published else None,'periodStart':published.get('periodStart')if published else None,'revision':published['draftRevision']if published else 0,'lessons':[],'bindingVerified':False,'bindingReady':False,'blockers':[],'issues':[]}
 if not published:
  base['blockers']=['publication_required'];return base
 try:
  start=date.fromisoformat(published['periodStart'])
  if start.weekday()!=0:raise ValueError()
 except(ValueError,KeyError,TypeError):raise HTTPException(409,'Не подтверждён период опубликованного расписания')from None
 known={t for lesson in published['lessons']for t in lesson.get('teachers',[])}
 ready=bool(refs)and set(refs)<=known
 base.update(bindingVerified=ready,bindingReady=ready,blockers=[]if ready else['teacher_binding_required'])
 if published.get('mode')=='working_assumptions':
  base.update(mode='working_assumptions',workingAssumptions=True,normativeConfirmed=False,
              experimentalId=published.get('experimentalId'),experimentalRevision=published.get('experimentalRevision'),
              assumptionsSummary={'count':len(published.get('assumptions',[])),
                                  'manualCorrections':published.get('manualCorrections') is True,
                                  'notice':'Опубликованная версия с рабочими предположениями'})
 if ready:
  for lesson in teacher_snapshot_lessons(published,refs):
   base['lessons'].append({**lesson,'date':(start+timedelta(days=lesson['day'])).isoformat()})
 return base

def create_absence(actor,body):
 if actor['staffRole']!='teacher'or not actor.get('teacherRefs'):raise HTTPException(403,'Нет подтверждённой привязки педагога')
 try:
  if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',body.get('startDate',''))or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',body.get('endDate','')):raise ValueError()
  start=date.fromisoformat(body['startDate']);end=date.fromisoformat(body['endDate'])
  if not 1<=(end-start).days+1<=366:raise ValueError()
 except(ValueError,TypeError):raise HTTPException(422,'Нужен конечный период отсутствия до366 дней')from None
 schedule=own_timetable(actor);lesson_ids=body['lessonIds'];own={r['id']for r in schedule['lessons']}
 if not lesson_ids or not set(lesson_ids)<=own:raise HTTPException(403,'Можно сообщить только о собственных занятиях')
 # A request is not an appointment and never changes the timetable.
 identifier=str(uuid4());payload=json.dumps({**body,'memberName':actor['name'],'status':'unconfirmed','publicationVersion':schedule['publicationVersion'],'draftRevision':schedule['revision']},sort_keys=True,ensure_ascii=False)
 with closing(connect())as c:
  c.execute('BEGIN IMMEDIATE');_current_actor(c,actor)
  existing=c.execute('SELECT id FROM staff_absences WHERE member_id=? AND payload=?',(actor['membershipId'],payload)).fetchone()
  if existing:return {'id':existing['id'],'status':'unconfirmed','scheduleChanged':False,'replayed':True}
  c.execute('CREATE TABLE IF NOT EXISTS staff_absences(id TEXT PRIMARY KEY,member_id TEXT NOT NULL,payload TEXT NOT NULL,created REAL NOT NULL)')
  c.execute('INSERT INTO staff_absences VALUES(?,?,?,?)',(identifier,actor['membershipId'],payload,time.time()));audit(c,actor['membershipId'],'own_absence',identifier);c.commit()
 return {'id':identifier,'status':'unconfirmed','scheduleChanged':False}


def approve_revocation(actor,member_id):
 if actor['staffRole']!='director':raise HTTPException(403,'Решение об отзыве принимает директор')
 if member_id==actor['membershipId']:raise HTTPException(409,'Собственный доступ этим действием не отзывается')
 identifier=str(uuid4())
 with closing(connect())as c:
  c.execute('BEGIN IMMEDIATE');_current_actor(c,actor)
  target=c.execute('SELECT * FROM staff_members WHERE id=? AND school_id=? AND enabled=1',(member_id,actor['schoolId'])).fetchone()
  if not target:raise HTTPException(404,'Сотрудник не найден')
  c.execute('INSERT INTO staff_revocations VALUES(?,?,?,0)',(identifier,member_id,actor['membershipId']));audit(c,actor['membershipId'],'approve_revocation',identifier);c.commit()
 return {'id':identifier,'memberId':member_id}

def execute_revocation(actor,approval_id):
 if actor['staffRole']not in{'director','access_admin'}:raise HTTPException(403,'Нет права исполнения отзыва')
 with closing(connect())as c:
  c.execute('BEGIN IMMEDIATE');_current_actor(c,actor)
  row=c.execute('SELECT r.*,m.school_id,m.role,m.enabled FROM staff_revocations r JOIN staff_members m ON m.id=r.member_id WHERE r.id=?',(approval_id,)).fetchone()
  if not row or row['consumed']or row['school_id']!=actor['schoolId']:raise HTTPException(409,'Решение об отзыве недоступно')
  approver=c.execute('SELECT * FROM staff_members WHERE id=?',(row['approved_by'],)).fetchone()
  if not approver or not approver['enabled']or approver['role']!='director':raise HTTPException(403,'Решение больше не подтверждено')
  if row['role']=='director'and c.execute("SELECT COUNT(*) FROM staff_members WHERE role='director'AND enabled=1").fetchone()[0]<=1:raise HTTPException(409,'Нельзя отозвать последнего директора')
  c.execute('UPDATE staff_members SET enabled=0,revision=revision+1 WHERE id=?',(row['member_id'],));c.execute('DELETE FROM staff_sessions WHERE member_id=?',(row['member_id'],));c.execute('UPDATE staff_revocations SET consumed=1 WHERE id=?',(approval_id,));audit(c,actor['membershipId'],'execute_revocation',approval_id);c.commit()
 return {'revoked':True,'membershipId':row['member_id']}


def list_absences(actor):
 if actor['staffRole']not in{'director','planner'}:raise HTTPException(403,'Нет доступа к запросам школы')
 with closing(connect())as c:
  rows=c.execute('SELECT a.id,a.member_id,a.payload,a.created FROM staff_absences a JOIN staff_members m ON m.id=a.member_id WHERE m.school_id=? ORDER BY a.created DESC LIMIT 200',(actor['schoolId'],)).fetchall()
 return {'requests':[{'id':r['id'],'membershipId':r['member_id'],'createdAt':r['created'],**json.loads(r['payload'])}for r in rows],'confirmedFacts':False}


def validate_calendar_bindings(identifiers,*,connection=None):
 if not identifiers:return
 from .db import database_path,table_exists
 try:
  if connection is None:
   path=database_path()
   if not path.exists():raise ValueError()
   with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True))as c:
    exists=c.execute("SELECT 1 FROM sqlite_master WHERE type='table'AND name='planning_config'").fetchone()
    row=c.execute('SELECT input_json FROM planning_config WHERE id=1').fetchone()if exists else None
  else:
   row=connection.execute('SELECT input_json FROM planning_config WHERE id=1').fetchone()if table_exists(connection,'planning_config')else None
  plan=json.loads(row[0])if row else None
  if not isinstance(plan,dict)or plan.get('confirmed')is not True:raise ValueError()
  known={t['id']for t in plan.get('teachers',[])if isinstance(t,dict)and isinstance(t.get('id'),str)}
  if not set(identifiers)<=known:raise ValueError()
 except(ValueError,TypeError,KeyError,sqlite3.Error):raise HTTPException(422,'Нормативная привязка не подтверждена. Сначала заполните и подтвердите план')from None
