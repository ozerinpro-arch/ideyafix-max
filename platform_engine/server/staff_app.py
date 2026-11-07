"""Separate, unpublished staff frontdoor. Own origin; existing owner gateway unchanged.
Run: uvicorn server.staff_app:create_app --factory --host 127.0.0.1 --port 18828.
Requires explicit SHKOLA_STAFF_ORIGIN, SHKOLA_STAFF_DB_PATH, SHKOLA_SCHOOL_ID,
SHKOLA_UI_DIRECTORY (a staff-mode build), SHKOLA_WORKSPACE_MODE=school.
"""
from contextlib import closing, nullcontext
import hmac,os,re
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import APIRouter,Depends,FastAPI,HTTPException,Request,Response,Query
from pydantic import BaseModel,ConfigDict,Field
from starlette.responses import JSONResponse,FileResponse
from . import staff_access as store
from .staff_policy import enforce
from .portal_app import _build_file,_ASSET_SUFFIXES

class Input(BaseModel):model_config=ConfigDict(extra='forbid',strict=True)
class Login(Input):username:str=Field(min_length=3,max_length=80);password:str=Field(min_length=1,max_length=256)
class Accept(Input):token:str=Field(min_length=43,max_length=128);password:str=Field(min_length=12,max_length=256)
class Nomination(Input):username:str=Field(min_length=3,max_length=80);name:str=Field(min_length=1,max_length=120);role:str=Field(min_length=1,max_length=30);teacherRefs:list[str]=Field(default_factory=list,max_length=20);calendarTeacherIds:list[str]=Field(default_factory=list,max_length=20)
class Invite(Input):nominationId:str=Field(min_length=1,max_length=80)
class Absence(Input):
 lessonIds:list[str]=Field(min_length=1,max_length=100)
 startDate:str=Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
 endDate:str=Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
 note:str=Field(default='',max_length=1000)

_REHEARSAL_COMMON={'/api/school/requests','/api/teacher/replacement-outcomes',
 '/api/school/replacement-outcomes','/api/teacher/preparation/materials'}
_REHEARSAL_READ=_REHEARSAL_COMMON|{
 '/api/school/replacements','/api/school/publications','/api/school/analytics/director',
 '/api/staff/me','/api/portal-session','/api/staff/my-schedule','/api/student/schedule',
 '/api/school/schedule-notifications','/api/staff/schedule-notifications',
 '/api/school/replacement-materials','/api/teacher/replacement-materials'}
_REHEARSAL_WRITE=_REHEARSAL_COMMON|{
 '/api/staff/login','/api/school/schedule-notifications/prepare',
 '/api/teacher/replacement-materials/requests','/api/teacher/replacement-materials/grants'}
_REHEARSAL_LIBRARY=r'/api/teacher/preparation/materials/[^/]+'
_REHEARSAL_GRANT=r'/api/teacher/replacement-materials/grants/share_[a-f0-9]{32}'
_REHEARSAL_MAX={
 ('GET','/api/school/integrations/status'),('GET','/api/school/integrations/max/me'),
 ('POST','/api/school/integrations/max/link'),('POST','/api/school/integrations/max/revoke'),
 ('GET','/api/school/integrations/max/schedule-notices'),('POST','/api/school/integrations/max/schedule-notices/queue'),
}

def _rehearsal_route(method,path):
 if method=='GET':
  return path in _REHEARSAL_READ or any(re.fullmatch(pattern,path)for pattern in (
   r'/api/school/requests/[A-Za-z0-9_-]+',_REHEARSAL_LIBRARY,_REHEARSAL_GRANT))
 if method=='POST':
  return path in _REHEARSAL_WRITE or any(re.fullmatch(pattern,path)for pattern in (
   r'/api/school/replacements/(candidates|preview|apply|replacement_[a-f0-9]{32}/(?:publish-preview|publish))',
   r'/api/school/replacement-outcomes/replacement_[a-f0-9]{32}/(?:preview|decision)',
   r'/api/staff/schedule-notifications/notice_[a-f0-9]{32}/acknowledge',_REHEARSAL_GRANT+'/revoke'))
 return method=='PATCH'and bool(re.fullmatch(_REHEARSAL_LIBRARY,path))

def _school_postgres_route(method,path):
 if (method,path)in _REHEARSAL_MAX:return True
 if method in{'GET','HEAD'}and(path in{'/','/index.html','/favicon.svg'}or path.startswith('/assets/')):return True
 if method=='GET'and path in{'/api/timetable','/api/launch/respondents','/api/teacher/preparation',
  '/api/school/integrations/max/schedule-notices'}:return True
 if method=='POST'and path in{'/api/staff/accept','/api/staff/logout'}:return True
 if method=='GET'and path in{'/api/staff/student-access','/api/teacher/learning','/api/student/learning',
  '/api/student/clubs','/api/teacher/consultations','/api/student/consultations',
  '/api/school/data-exchange/catalog','/api/school/data-exchange/lesson-progress-references',
  '/api/school/data-exchange/template/lessonProgress'}:return True
 if method in{'GET','POST'}and path in{'/api/staff/student-schedule-bindings',
  '/api/staff/student-schedule-bindings/dated'}:return True
 if method=='POST'and path in{'/api/staff/student-access/invitations','/api/teacher/learning/assignments',
  '/api/student/learning/practice','/api/student/clubs/documents/prepare','/api/teacher/consultations',
  '/api/school/data-exchange/preview','/api/school/data-exchange/apply','/api/school/data-exchange/export'}:return True
 if method=='GET'and any(re.fullmatch(pattern,path)for pattern in(
  r'/api/(teacher|student|director)/lesson-progress',r'/api/(teacher|student)/learning/assignments/[^/]+')):return True
 if method=='POST'and any(re.fullmatch(pattern,path)for pattern in(
  r'/api/(teacher|student)/consultations/[a-f0-9]{32}/decision',
  r'/api/(teacher|student)/learning/assignments/[^/]+/messages',
  r'/api/student/learning/assignments/[^/]+/(draft|submit)',
  r'/api/teacher/learning/assignments/[^/]+/review')):return True
 return _rehearsal_route(method,path)

def create_app(*,replacement_storage=None,replacement_notice_settings=None,replacement_max_resources=None,school_storage=None,school_max_resources=None):
 gate=None
 if school_storage is not None or os.environ.get('SHKOLA_DOMAIN_BACKEND','').strip()=='postgresql':
  from .school_runtime_gate import RuntimeGate
  gate=RuntimeGate.for_staff(store.configuration()[0])
 with gate.shared()if gate is not None else nullcontext():
  return _create_app(replacement_storage=replacement_storage,replacement_notice_settings=replacement_notice_settings,
   replacement_max_resources=replacement_max_resources,school_storage=school_storage,school_max_resources=school_max_resources,_runtime_gate=gate)

def _create_app(*,replacement_storage=None,replacement_notice_settings=None,replacement_max_resources=None,school_storage=None,school_max_resources=None,_runtime_gate=None):
 from .school_postgres import SchoolPostgres,SchoolPostgresSettings
 if school_storage is not None and any(value is not None for value in(
  replacement_storage,replacement_notice_settings,replacement_max_resources)):
  raise ValueError('School PostgreSQL profile cannot use rehearsal resources')
 configured=SchoolPostgresSettings.from_environment(os.environ.get('SHKOLA_SCHOOL_ID',''))
 if configured is not None:
  if any(value is not None for value in(replacement_storage,replacement_notice_settings,replacement_max_resources)):
   raise ValueError('School PostgreSQL profile cannot use rehearsal resources')
  if school_storage is not None and school_storage.settings!=configured:
   raise ValueError('School PostgreSQL settings differ from the selected storage')
  school_storage=school_storage or SchoolPostgres(configured)
 working_profile=school_storage is not None
 if school_max_resources is not None and not working_profile:
  raise ValueError('School MAX resources require the selected school PostgreSQL profile')
 if working_profile:
  if type(school_storage)is not SchoolPostgres:raise ValueError('Expected school PostgreSQL storage')
  replacement_storage=school_storage
 if replacement_max_resources is not None and replacement_storage is None:
  raise ValueError('Explicit MAX rehearsal requires PostgreSQL domain storage')
 os.environ['PYTHON_DOTENV_DISABLED']='1'
 from . import auth,core
 if os.environ.get('SHKOLA_WORKSPACE_MODE')!='school':raise RuntimeError('Staff requires explicit school workspace')
 origin=os.environ.get('SHKOLA_STAFF_ORIGIN','');parsed=urlsplit(origin)
 if not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:raise RuntimeError('Explicit canonical staff origin required')
 secure=parsed.scheme=='https'
 base=os.environ.get('SHKOLA_STAFF_BASE_PATH','/client/school')
 if len(base)>120 or not re.fullmatch(r'/[A-Za-z0-9][A-Za-z0-9_-]*(?:/[A-Za-z0-9][A-Za-z0-9_-]*)*',base):raise RuntimeError('Explicit canonical staff base path required')
 if working_profile:
  if not secure and not(parsed.scheme=='http'and parsed.hostname=='127.0.0.1'):
   raise RuntimeError('School PostgreSQL requires HTTPS or private IPv4 loopback')
 elif os.environ.get('SHKOLA_LOCAL_RUNTIME_MODE')=='persistent':
  if parsed.scheme!='http'or parsed.hostname!='127.0.0.1':raise RuntimeError('Private local school requires explicit HTTP IPv4 loopback origin')
  from .local_runtime_storage import private_local_database_paths
  private_local_database_paths()
 elif not secure:
  if parsed.scheme!='http'or parsed.hostname not in{'localhost','127.0.0.1'}:raise RuntimeError('HTTP staff allowed only in isolated loopback tests')
  from .test_isolation import isolated_database_paths
  isolated_database_paths()
  staff_path,_=store.configuration()
  if not staff_path.resolve().is_relative_to(Path('/tmp').resolve())and not staff_path.resolve().is_relative_to(Path(os.environ.get('TMPDIR','/tmp')).resolve()):raise RuntimeError('Synthetic staff DB required for HTTP')
 ui=Path(os.environ.get('SHKOLA_UI_DIRECTORY',''))
 if not ui.is_absolute()or not(ui/'index.html').is_file():raise RuntimeError('Explicit staff UI build required')
 domain_config=(os.environ.get('SHKOLA_DB_PATH',''),os.environ.get('AGENT_DB_PATH',''))
 staff_config=store.configuration()
 from . import club_payment_reconciliation
 club_rehearsal=replacement_storage is None and club_payment_reconciliation.preview_enabled(staff_config[1],parsed.hostname)
 from .notice_jobs import DocumentJobs,NoticeJobs,NoticeJobSettings,bind_replacement_notice_settings
 notice_settings=None
 if working_profile:
  replacement_storage.validate_staff(staff_config[1],parsed.scheme,parsed.hostname,staff_config[0])
  from .school_notice_jobs import SchoolNoticeSettings
  notice_settings=SchoolNoticeSettings.from_environment(staff_config[1],storage=replacement_storage)
 elif replacement_storage is not None:
  from .replacement_postgres import ReplacementPostgres
  if type(replacement_storage)is not ReplacementPostgres:raise ValueError('Expected isolated PostgreSQL rehearsal storage')
  replacement_storage.validate_staff(staff_config[1],parsed.scheme,parsed.hostname,staff_config[0])
  if replacement_notice_settings is not None:
   notice_settings=bind_replacement_notice_settings(replacement_notice_settings,replacement_storage)
   replacement_storage=replacement_storage.bind_queue_identity(notice_settings.replacement_domain)
 elif replacement_notice_settings is not None:
  raise ValueError('Explicit rehearsal queue requires PostgreSQL domain storage')
 else:
  notice_settings=NoticeJobSettings.from_environment(staff_config[1])
 store.initialize();store.register_configuration(staff_config)
 from .integration_settings import IntegrationSettings
 from .school_integrations import IntegrationMiddleware,IntegrationResources
 if working_profile:
  from .school_max import SchoolMaxResources,resources_for_school
  if school_max_resources is not None:
   if (type(school_max_resources)is not SchoolMaxResources
       or school_max_resources.replacement_storage!=school_storage
       or school_max_resources.settings.school_id!=staff_config[1]):
    raise ValueError('MAX resources differ from the selected school PostgreSQL profile')
   resources=school_max_resources
  else:resources=resources_for_school(school_storage)
 elif replacement_max_resources is not None:
  if (type(replacement_max_resources)is not IntegrationResources
      or replacement_max_resources.replacement_storage is None
      or (Path(replacement_max_resources.settings.staff_db),replacement_max_resources.settings.school_id)!=staff_config
      or replacement_max_resources.domain_binding!=domain_config):
   raise ValueError('Expected MAX resources for this isolated PostgreSQL staff profile')
  resources=replacement_max_resources
  replacement_storage=replacement_storage.bind_queue_identity(resources.max_runtime.replacement_domain)
  if replacement_storage!=resources.replacement_storage:
   raise ValueError('MAX and staff require the same PostgreSQL source')
  replacement_storage.queue_identity()
 else:
  resources=IntegrationResources(IntegrationSettings.from_environment(staff_config[1],
   env={} if replacement_storage is not None else None))
 def own_projection(profile,reader):
  if replacement_storage is None:return reader(profile)
  with closing(replacement_storage.read_connection())as connection:
   connection.execute('BEGIN')
   return reader(profile,connection=connection)
 def teacher_schedule(profile):return own_projection(profile,store.own_timetable)
 def domain_read(reader,*args,**kwargs):
  with closing(replacement_storage.read_connection())as connection:
   connection.execute('BEGIN')
   return reader(*args,**kwargs,connection=connection)
 def public_profile(profile):
  return {**profile,'workspaceProfile':'school-postgres'}if working_profile else profile
 # Personal teacher material storage is separate from identities and school data.
 # Reuse the explicitly selected provider/model and both existing request gates.
 from .teacher_preparation import TeacherPreparationService,CreateMaterial,UpdateMaterial,GenerateMaterial,make_generator
 from .school_publications import read_active_snapshot
 from .school_calendar import effective_calendar_snapshot
 from .schedule_agent import ModelConfiguration
 teacher_configuration=ModelConfiguration(resources.settings.ai_key,resources.settings.ai_model,
  bool(resources.settings.request_ai_requested and resources.manager_ai_enabled and not resources.settings.errors))
 secret_paths=()if replacement_storage is not None else tuple(os.environ.get(k,'')for k in
  ('NANOGPT_API_KEY_FILE','SHKOLA_MAX_BOT_TOKEN_FILE','SHKOLA_MAX_WEBHOOK_SECRET_FILE'))
 teacher_preparation=TeacherPreparationService(staff_config[1],staff_config[0].parent/'teacher-preparation.sqlite3',
  generator=make_generator(teacher_configuration),
  schedule_reader=teacher_schedule if replacement_storage is not None else None,
  publication_reader=(lambda school:domain_read(read_active_snapshot,school))if replacement_storage is not None else None,
  calendar_reader=(lambda week,**kwargs:domain_read(effective_calendar_snapshot,week,**kwargs))if replacement_storage is not None else None,
  forbidden=tuple(p for p in (staff_config[0],*domain_config,resources.settings.ai_db,resources.settings.max_db,
   *secret_paths)if p))
 from .student_learning import StudentLearningService,InvitationInput,AssignmentInput as LearningAssignment,MessageInput as LearningMessage,PracticeInput,DraftInput,ReviewInput,bound_service as bound_learning
 learning=StudentLearningService(staff_config[1],staff_config[0].parent/'student-learning.sqlite3',generator=make_generator(teacher_configuration),
  forbidden=(*teacher_preparation.forbidden,teacher_preparation.path),domain_storage=school_storage)
 from .student_schedule import StudentScheduleService,ScheduleBindingInput
 student_schedule=StudentScheduleService(learning)
 from .dated_student_groups import DatedGroupService,DatedGroupInput
 dated_groups=DatedGroupService(learning)
 from .launch_identity import install
 install()
 cookie='__Host-shkola_staff'if secure else'shkola_staff_test';pre='__Host-shkola_staff_pre'if secure else'shkola_staff_pre_test'
 app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
 app.add_exception_handler(core.RequestValidationError,core.safe_validation_error)
 marker=object()
 def actor(request:Request):
  if request.scope.get('_staff_marker')is not marker:raise HTTPException(401,'Войдите в школьный кабинет')
  profile=request.scope['staff_profile'].copy();expected_owner=request.headers.get('x-learning-owner')
  # An optional UI precondition never grants authority; the verified session owns it.
  if expected_owner is not None and expected_owner!=profile.get('userId'):raise HTTPException(401,'Сеанс изменился. Войдите в свой школьный кабинет')
  return profile
 def require_current_session(request,profile):
  session=store.get_session(request.cookies.get(cookie,''))
  if not session or session.get('profile')!=profile:raise HTTPException(401,'Сеанс изменился. Войдите в свой школьный кабинет')
 def checked_learning_result(request,profile,result):
  require_current_session(request,profile);return result
 app.dependency_overrides[auth.session_profile]=actor
 def teacher_actor(profile=Depends(actor)):
  if profile.get('staffRole')!='teacher'or profile.get('schoolId')!=staff_config[1]or not store.membership_current(profile):
   raise HTTPException(403,'Личные материалы доступны только действующему учителю')
  return profile
 @app.get('/api/teacher/preparation')
 def teacher_dashboard(profile=Depends(teacher_actor)):return teacher_preparation.dashboard(profile)
 @app.get('/api/teacher/preparation/materials')
 def teacher_materials(className:str|None=None,profile=Depends(teacher_actor)):return teacher_preparation.list_materials(profile,class_name=className)
 @app.post('/api/teacher/preparation/materials')
 def teacher_create_material(body:CreateMaterial,profile=Depends(teacher_actor)):return teacher_preparation.create_material(profile,body.model_dump())
 @app.get('/api/teacher/preparation/materials/{material_id}')
 def teacher_material(material_id:str,profile=Depends(teacher_actor)):return teacher_preparation.get_material(profile,material_id)
 @app.patch('/api/teacher/preparation/materials/{material_id}')
 def teacher_update_material(material_id:str,body:UpdateMaterial,profile=Depends(teacher_actor)):return teacher_preparation.update_material(profile,material_id,body.model_dump(exclude_unset=True))
 @app.post('/api/teacher/preparation/generate')
 def teacher_generate_material(body:GenerateMaterial,profile=Depends(teacher_actor)):
  data=body.model_dump();recovered=teacher_preparation.recover_generation(profile,data)
  if recovered is not None:return recovered
  store.limited('teacher-preparation-generate',profile['membershipId'],20)
  return teacher_preparation.generate(profile,data)
 @app.get('/api/staff/student-access')
 def student_access(request:Request,profile=Depends(actor)):return checked_learning_result(request,profile,learning.access_list(profile))
 @app.get('/api/student/schedule')
 def student_own_schedule(request:Request,profile=Depends(actor)):
  if request.query_params:raise HTTPException(422,'Личное расписание не принимает фильтры ученика, класса или группы')
  return checked_learning_result(request,profile,own_projection(profile,student_schedule.own))
 from .school_clubs import student_overview
 from .club_documents import ClubDocuments,DocumentInput,preview_enabled as club_document_preview
 @app.get('/api/student/clubs')
 def student_clubs(request:Request,profile=Depends(actor)):
  if request.query_params:raise HTTPException(422,'Личный список кружков не принимает фильтры')
  with learning.domain_scope()as connection:
   result=student_overview(learning,profile,connection=connection)
   if club_documents is not None:result['documents']=club_documents.listing(profile,connection=connection)
  if club_rehearsal:
   result.update(club_payment_reconciliation.student_receipts(learning,profile))
  return checked_learning_result(request,profile,result)
 @app.post('/api/student/clubs/documents/prepare')
 def prepare_club_document(body:DocumentInput,request:Request,profile=Depends(actor)):
  if club_documents is None:raise HTTPException(404,'Учебные документы недоступны')
  if request.query_params:raise HTTPException(422,'Учебный документ не принимает параметры запроса')
  code,result=club_documents.submit(body,profile,before_return=lambda:require_current_session(request,profile))
  return JSONResponse(result,status_code=code)
 @app.get('/api/staff/student-schedule-bindings')
 def student_schedule_binding(request:Request,studentId:str=Query(min_length=1,max_length=80),profile=Depends(actor)):
  return checked_learning_result(request,profile,student_schedule.details(profile,studentId))
 @app.post('/api/staff/student-schedule-bindings')
 def student_schedule_save(body:ScheduleBindingInput,request:Request,profile=Depends(actor)):
  return checked_learning_result(request,profile,student_schedule.save(profile,body.model_dump(),before_return=lambda:require_current_session(request,profile)))
 @app.get('/api/staff/student-schedule-bindings/dated')
 def dated_group_details(request:Request,studentId:str=Query(min_length=1,max_length=80),publicationVersion:int=Query(ge=1,le=2**31-1),lessonDate:str=Query(pattern=r'^\d{4}-\d{2}-\d{2}$'),profile=Depends(actor)):
  if set(request.query_params)!={'studentId','publicationVersion','lessonDate'}or len(request.query_params.multi_items())!=3:raise HTTPException(422,'Укажите только ученика, публикацию и дату урока')
  result=dated_groups.details(profile,studentId,publicationVersion,lessonDate)
  require_current_session(request,profile)
  return result
 @app.post('/api/staff/student-schedule-bindings/dated')
 def dated_group_save(request:Request,body:DatedGroupInput,profile=Depends(actor)):
  if request.query_params:raise HTTPException(422,'Подтверждение состава не принимает параметры запроса')
  result=dated_groups.save(profile,body.model_dump(),before_return=lambda:require_current_session(request,profile))
  require_current_session(request,profile)
  return result
 @app.post('/api/staff/student-access/invitations')
 def student_invitation(body:InvitationInput,request:Request,profile=Depends(actor)):
  value=learning.invite_student(profile,body.model_dump(),before_return=lambda:require_current_session(request,profile))
  path=value['invite']['path'];return checked_learning_result(request,profile,{**value,'invite':{**value['invite'],'path':base+'/#invite='+path.split('#invite=',1)[1]}})
 @app.get('/api/teacher/learning')
 def teacher_learning(request:Request,profile=Depends(actor)):return checked_learning_result(request,profile,learning.teacher_dashboard(profile))
 @app.post('/api/teacher/learning/assignments')
 def create_learning_assignment(body:LearningAssignment,request:Request,profile=Depends(actor)):
  data=body.model_dump();old=learning.recover(profile,data,'assignment')
  if old is not None:return checked_learning_result(request,profile,old)
  return checked_learning_result(request,profile,learning.execute(profile,data,before_return=lambda:require_current_session(request,profile)))
 @app.get('/api/teacher/learning/assignments/{assignment_id}')
 @app.get('/api/student/learning/assignments/{assignment_id}')
 def learning_assignment(assignment_id:str,request:Request,profile=Depends(actor)):return checked_learning_result(request,profile,learning.detail(profile,assignment_id))
 @app.post('/api/teacher/learning/assignments/{assignment_id}/messages')
 @app.post('/api/student/learning/assignments/{assignment_id}/messages')
 def learning_message(assignment_id:str,body:LearningMessage,request:Request,profile=Depends(actor)):
  data=body.model_dump();old=learning.recover(profile,data,'message:'+assignment_id)
  if old is not None:return checked_learning_result(request,profile,old)
  if data['askAi']:store.limited('student-learning-tutor',profile['membershipId'],20)
  return checked_learning_result(request,profile,learning.execute(profile,data,assignment_id=assignment_id,before_return=lambda:require_current_session(request,profile)))
 @app.post('/api/student/learning/assignments/{assignment_id}/draft')
 def learning_draft(assignment_id:str,body:DraftInput,request:Request,profile=Depends(actor)):
  return checked_learning_result(request,profile,learning.execute_work(profile,body.model_dump(),assignment_id,'draft',before_return=lambda:require_current_session(request,profile)))
 @app.post('/api/student/learning/assignments/{assignment_id}/submit')
 def learning_submit(assignment_id:str,body:DraftInput,request:Request,profile=Depends(actor)):
  return checked_learning_result(request,profile,learning.execute_work(profile,body.model_dump(),assignment_id,'submit',before_return=lambda:require_current_session(request,profile)))
 @app.post('/api/teacher/learning/assignments/{assignment_id}/review')
 def learning_review(assignment_id:str,body:ReviewInput,request:Request,profile=Depends(actor)):
  return checked_learning_result(request,profile,learning.execute_work(profile,body.model_dump(),assignment_id,'review',before_return=lambda:require_current_session(request,profile)))
 @app.post('/api/student/learning/practice')
 def student_practice(body:PracticeInput,request:Request,profile=Depends(actor)):
  data=body.model_dump();old=learning.recover(profile,data,'practice')
  if old is not None:return checked_learning_result(request,profile,old)
  store.limited('student-learning-tutor',profile['membershipId'],20)
  return checked_learning_result(request,profile,learning.execute(profile,data,practice=True,before_return=lambda:require_current_session(request,profile)))
 @app.get('/api/student/learning')
 def student_learning(request:Request,profile=Depends(actor)):return checked_learning_result(request,profile,learning.student_dashboard(profile))
 def cookie_set(response,name,value,ttl):response.set_cookie(name,value,max_age=ttl,secure=secure,httponly=True,samesite='strict',path='/')
 @app.get('/api/portal-session')
 def portal_session(request:Request,response:Response):
  session=request.scope.get('_staff_session')
  if not session:
   store.limited('preauth',request.client.host if request.client else'unknown',100)
   token,csrf=store.new_session();cookie_set(response,pre,token,600)
  else:csrf=session['csrf']
  return {'csrfToken':csrf,'staff':True}
 @app.post('/api/staff/login')
 def signin(body:Login,request:Request,response:Response):
  address=request.client.host if request.client else'unknown'
  store.limited('login-ip',address,100)
  store.limited('login',address+':'+body.username.lower())
  token,profile=store.login(body.username,body.password,request.scope['_staff_session']['csrf']);cookie_set(response,cookie,token,store.SESSION_TTL)
  return {'profile':public_profile(profile)}
 @app.post('/api/staff/accept')
 def accept(body:Accept,request:Request,response:Response):
  store.limited('accept',request.client.host if request.client else'unknown',50)
  with learning.domain_scope()as connection:
   token,profile=store.accept(body.token,body.password,request.scope['_staff_session']['csrf'],learning_connection=connection)
  cookie_set(response,cookie,token,store.SESSION_TTL)
  return {'profile':public_profile(profile)}
 @app.get('/api/staff/me')
 def me(profile=Depends(actor)):return {'profile':public_profile(profile)}
 @app.post('/api/staff/logout')
 def logout(request:Request,response:Response,profile=Depends(actor)):
  store.destroy_session(request.cookies.get(cookie,''));response.delete_cookie(cookie,path='/',secure=secure,httponly=True,samesite='strict');return {'loggedOut':True}
 @app.get('/api/staff/directory')
 def directory(profile=Depends(actor)):return store.directory(profile)
 @app.post('/api/staff/nominations')
 def nominate(body:Nomination,profile=Depends(actor)):return store.nomination(profile,body.model_dump())
 @app.post('/api/staff/invites')
 def invitation(body:Invite,profile=Depends(actor)):
  result=store.invite(profile,body.nominationId)
  return {**result,'path':base+'/#invite='+result['token']}
 @app.post('/api/staff/members/{member_id}/revoke')
 def revoke(member_id:str,profile=Depends(actor)):return store.revoke(profile,member_id)
 @app.post('/api/staff/members/{member_id}/approve-revoke')
 def approve_revoke(member_id:str,profile=Depends(actor)):return store.approve_revocation(profile,member_id)
 @app.post('/api/staff/revocations/{approval_id}/execute')
 def execute_revoke(approval_id:str,profile=Depends(actor)):return store.execute_revocation(profile,approval_id)
 @app.get('/api/staff/my-schedule')
 def own_schedule(profile=Depends(actor)):return teacher_schedule(profile)
 @app.get('/api/staff/absences')
 def absence_requests(profile=Depends(actor)):return store.list_absences(profile)
 @app.post('/api/staff/my-absence')
 def absence(body:Absence,profile=Depends(actor)):return store.create_absence(profile,body.model_dump())
 # Real core routers are reused. Teacher timetable has an explicit projection and never reaches the full route.
 routes=[r for r in core.app.router.routes if not getattr(r,'path','').startswith(('/api/demo','/api/health','/api/state'))]
 if working_profile:
  from . import timetable
  routes=[r for r in routes if getattr(r,'path','')!='/api/timetable'and not getattr(r,'path','').startswith('/api/school/data-exchange')]
  from .school_lesson_progress_postgres import router_for as postgres_lesson_journal_router
  app.include_router(postgres_lesson_journal_router(school_storage,actor,require_current_session))
  @app.get('/api/timetable')
  def school_timetable(profile=Depends(actor)):
   return teacher_schedule(profile)if profile.get('staffRole')=='teacher'else own_projection(profile,
    lambda _profile,connection=None:timetable.read_draft(connection=connection))
 if replacement_storage is not None:
  from . import director_analytics,school_publications,school_replacements,school_requests
  routes=[r for r in routes if not getattr(r,'path','').startswith((
   '/api/school/replacements','/api/school/publications','/api/school/analytics/director','/api/school/requests'))]
  app.include_router(school_replacements.router_for(replacement_storage))
  app.include_router(director_analytics.router_for(replacement_storage))
  app.include_router(school_requests.router_for(replacement_storage))
  @app.get('/api/school/publications')
  def rehearsal_publications(profile=Depends(school_publications._reader)):
   return school_publications.publication_status(profile,storage=replacement_storage)
 app.include_router(APIRouter(routes=routes))
 from .schedule_week_preview import router as week_preview_router
 app.include_router(week_preview_router,prefix='/api/planning/experimental-school')
 from .teacher_preferences import router as teacher_preferences_router
 app.include_router(teacher_preferences_router)
 from .schedule_notifications import router_for as schedule_notifications_router,service_for as notification_service
 club_documents=ClubDocuments(learning,DocumentJobs(notice_settings))if(
  (replacement_storage is None or working_profile)and club_document_preview(staff_config[1],notice_settings))else None
 app.include_router(schedule_notifications_router(notification_service(student_schedule,storage=replacement_storage),
  jobs=NoticeJobs(notice_settings)if notice_settings else None))
 from .consultations import router_for as consultations_router
 app.include_router(consultations_router(learning,student_schedule,require_current_session))
 if replacement_storage is None or working_profile:
  from .lesson_progress_views import router_for as lesson_progress_router
  app.include_router(lesson_progress_router(learning,actor,require_current_session))
 if club_rehearsal:app.include_router(club_payment_reconciliation.router_for(learning,actor,require_current_session))
 from .replacement_materials import ReplacementMaterialService,create_router as replacement_materials_router
 replacement_materials=ReplacementMaterialService(staff_config[1],staff_config[0].parent/'replacement-materials.sqlite3',
  teacher_preparation,forbidden=(learning.path,),storage=replacement_storage)
 app.include_router(replacement_materials_router(replacement_materials))
 from .replacement_outcomes import router_for as replacement_outcomes_router
 app.include_router(replacement_outcomes_router(replacement_storage))
 @app.get('/api/health')
 def health():return {'status':'ok','staff':True}
 @app.api_route('/api/{path:path}',methods=['GET','POST','PATCH','PUT','DELETE','HEAD','OPTIONS'])
 def unknown_api(path:str):raise HTTPException(404,'Не найдено')
 @app.get('/{path:path}')
 def ui_files(path:str):
  if path in{'','index.html'}:target=_build_file(ui.resolve(),'index.html')
  elif path=='favicon.svg':target=_build_file(ui.resolve(),path)
  elif path.startswith('assets/')and Path(path).suffix.lower()in _ASSET_SUFFIXES:target=_build_file(ui.resolve(),path)
  else:raise HTTPException(404,'Не найдено')
  return FileResponse(target)
 @app.head('/')
 @app.head('/index.html')
 def ui_entry_head():return FileResponse(_build_file(ui.resolve(),'index.html'))

 class Frontdoor:
  async def __call__(self,scope,receive,send):
   from .school_requests import bound_school
   try:
    with _runtime_gate.shared()if _runtime_gate is not None and scope['type']=='http'else nullcontext():
     if working_profile and scope['type']=='http':
      from .school_restore import require_activation
      require_activation(staff_config[0],staff_config[1],replacement_storage.settings.expected_domain,
       origin,replacement_storage.settings.activation_id)
     with store.bound_configuration(staff_config):
      with bound_school(staff_config[1]):
       with bound_learning(learning):await self.handle(scope,receive,send)
   except HTTPException as error:
    if scope['type']!='http':raise
    await JSONResponse({'detail':error.detail},error.status_code,headers={
     'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})(scope,receive,send)
  async def handle(self,scope,receive,send):
   if scope['type']!='http':
    if scope['type']=='lifespan':return await app(scope,receive,send)
    if scope['type']=='websocket':await send({'type':'websocket.close','code':1008})
    return
   original_send=send
   async def safe_send(message):
    if message['type']=='http.response.start':
     additions={b'cache-control':b'no-store',b'x-content-type-options':b'nosniff',b'referrer-policy':b'no-referrer',b'content-security-policy':b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"}
     message={**message,'headers':[(k,v)for k,v in message.get('headers',[])if k.lower()not in additions]+list(additions.items())}
    await original_send(message)
   send=safe_send
   if domain_config!=(os.environ.get('SHKOLA_DB_PATH',''),os.environ.get('AGENT_DB_PATH','')):return await JSONResponse({'detail':'Настройка хранилища изменилась; нужен новый запуск'},503)(scope,receive,send)
   request=Request(scope,receive);path=scope['path'];prefix=base
   if not path.startswith(prefix+'/'):return await JSONResponse({'detail':'Не найдено'},404)(scope,receive,send)
   inner_path=path[len(prefix):];headers={}
   for k,v in scope['headers']:
    key=k.lower()
    if key in headers and key in{b'host',b'origin',b'cookie',b'x-csrf-token',b'content-length'}:return await JSONResponse({'detail':'Неоднозначный запрос'},400)(scope,receive,send)
    headers[key]=v
   cookie_keys=[part.strip().split('=',1)[0]for part in headers.get(b'cookie',b'').decode('latin1').split(';')if '='in part]
   if any(cookie_keys.count(key)>1 for key in(cookie,pre)):return await JSONResponse({'detail':'Неоднозначная сессия'},400)(scope,receive,send)
   if headers.get(b'host',b'').decode()!=parsed.netloc:return await JSONResponse({'detail':'Неверный адрес'},400)(scope,receive,send)
   # Only the public sign-in document may be reached by an external link.
   # API, mutations, embeddings and requests with Origin keep the same fence.
   document_entry=(scope['method']in{'GET','HEAD'}and inner_path in{'/','/index.html'}
    and headers.get(b'sec-fetch-mode')==b'navigate'and headers.get(b'sec-fetch-dest')==b'document'and b'origin'not in headers)
   if (headers.get(b'origin')and headers[b'origin'].decode()!=origin)or(headers.get(b'sec-fetch-site')==b'cross-site'and not document_entry):return await JSONResponse({'detail':'Внешний источник запрещён'},403)(scope,receive,send)
   if len(scope['query_string'])>4096 or '..'in inner_path.split('/')or '%'in inner_path or '\\'in inner_path or '//'in inner_path:return await JSONResponse({'detail':'Не найдено'},404)(scope,receive,send)
   teacher_material_write=(scope['method']=='POST'and inner_path=='/api/teacher/preparation/materials')or(
    scope['method']=='PATCH'and re.fullmatch(r'/api/teacher/preparation/materials/[^/]+',inner_path))
   learning_assignment_write=scope['method']=='POST'and inner_path=='/api/teacher/learning/assignments'
   limit=256*1024 if teacher_material_write else 128*1024 if learning_assignment_write else core.request_body_limit(scope['method'],inner_path)
   try:length=int(headers.get(b'content-length',b'0'))
   except ValueError:length=-1
   if length<0 or length>limit:return await JSONResponse({'detail':'Тело запроса превышает допустимый размер или имеет неверную длину'},413)(scope,receive,send)
   body=b''
   while True:
    message=await receive()
    if message['type']=='http.disconnect':return
    body+=message.get('body',b'')
    if len(body)>limit:return await JSONResponse({'detail':'Слишком большой запрос'},413)(scope,receive,send)
    if not message.get('more_body'):break
   session=store.get_session(request.cookies.get(cookie,''));session=session or store.get_session(request.cookies.get(pre,''));profile=session.get('profile')if session else None
   public=inner_path=='/api/portal-session'or inner_path in{'/api/staff/login','/api/staff/accept'}
   try:
    if inner_path.startswith('/api/')and profile and profile.get('staffRole')=='student':enforce(profile,scope['method'],inner_path)
    if inner_path.startswith('/api/')and not public:
     if not profile:raise HTTPException(401,'Войдите в школьный кабинет')
     enforce(profile,scope['method'],inner_path)
    if scope['method']not in{'GET','HEAD'}:
     if headers.get(b'origin',b'').decode()!=origin or not session or not hmac.compare_digest(headers.get(b'x-csrf-token',b''),session['csrf'].encode()):raise HTTPException(403,'Обновите страницу для подтверждения безопасности')
    if (working_profile and inner_path.startswith('/api/')and profile and profile.get('staffRole')not in{'director','planner','teacher','student'}
        and inner_path not in{'/api/portal-session','/api/staff/me','/api/staff/logout'}):
     raise HTTPException(403,'Школьный кабинет ещё не подключён для этой роли')
    if working_profile and not _school_postgres_route(scope['method'],inner_path):
     raise HTTPException(404,'Маршрут недоступен в выбранном профиле школы')
    if (replacement_storage is not None and not working_profile and not _rehearsal_route(scope['method'],inner_path)
        and not (replacement_max_resources is not None and (scope['method'],inner_path)in _REHEARSAL_MAX)):
     raise HTTPException(404,'Маршрут недоступен на стенде замены')
    if profile and profile['staffRole']=='teacher'and inner_path=='/api/timetable'and scope['method']=='GET':return await JSONResponse(teacher_schedule(profile))(scope,receive,send)
   except HTTPException as e:return await JSONResponse({'detail':e.detail},e.status_code)(scope,receive,send)
   inner={**scope,'path':inner_path,'raw_path':inner_path.encode(),'_staff_marker':marker,'staff_identity_verified':bool(profile),'staff_profile':profile,'_staff_session':session,'headers':[(k,v)for k,v in scope['headers']if k.lower()not in{b'host',b'origin',b'x-school-bridge',b'authorization'}]+[(b'host',b'localhost:8798'),(b'origin',b'http://localhost:8798')]}
   if inner_path in {'/api/school/integrations/max/schedule-notices','/api/school/integrations/max/schedule-notices/queue',
    '/api/school/integrations/max/me','/api/school/integrations/max/link','/api/school/integrations/max/revoke'}:
    inner['_staff_require_current_session']=require_current_session
   delivered=False
   async def replay():
    nonlocal delivered
    if not delivered:delivered=True;return {'type':'http.request','body':body,'more_body':False}
    return await receive()
   async def secured(message):
    if message['type']=='http.response.start':message={**message,'headers':[*message.get('headers',[]),(b'cache-control',b'no-store'),(b'x-content-type-options',b'nosniff'),(b'referrer-policy',b'no-referrer'),(b'content-security-policy',b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")]}
    await send(message)
   await app(inner,replay,secured)
 return IntegrationMiddleware(Frontdoor(),resources)
