"""Pure read-only candidate projection. Never recover workers or materialize storage."""
from contextlib import closing
from copy import deepcopy
import json,sqlite3
from fastapi import APIRouter,Depends,HTTPException,Path as APIPath,Query
from . import db,planning,school_publications as pub
from .request_planning import canonical_manager
from .school_requests import require_bound_school
from .auth import require_roles

NOTICE='Это предварительный вариант на общей сетке. Черновик и опубликованное расписание не изменены; сохранение требует отдельного согласования.'
def _actor(profile):
 manager=canonical_manager(profile);require_bound_school(manager['schoolId']);pub._actor(manager,'schedule.manage')
 return manager,planning._actor(profile)
def _lesson(row):return {k:deepcopy(row[k])for k in planning.LESSON_FIELDS}
def _has_domain_pair(plan,demand):
 """Recheck the solver's exact single-demand domain predicate, no search or
 invented assignment. Cross-event/weekly failures are deliberately excluded."""
 slots={s['id']:s for s in plan['slots']};groups={g['id']:g for g in plan['groups']};teachers={t['id']:t for t in plan['teachers']};rooms={r['id']:r for r in plan['rooms']}
 overlap=lambda a,b:a['day']==b['day']and a['start']<b['end']and b['start']<a['end']
 affected=set()
 for ident in demand['groupIds']:
  first=groups[ident]
  affected.update(g['id']for g in groups.values()if planning.canonical(g['className'])==planning.canonical(first['className'])and not(first['id']!=g['id']and first['kind']==g['kind']=='subgroup'and first['partition']and first['partition']==g['partition']))
 def blocked(entity,slot):return any(overlap(slot,slots[x])for x in entity['unavailableSlotIds'])
 size=sum(groups[x]['size']for x in demand['groupIds']);tags={planning.canonical(x)for x in demand['requiredTags']}
 for ident in demand['allowedSlotIds']or slots:
  slot=slots[ident]
  if any(blocked(groups[x],slot)for x in affected):continue
  unavailable=False
  for ident in demand['teacherIds']:
   teacher=teachers[ident];windows=teacher.get('workWindows',[])
   if blocked(teacher,slot)or windows and not any(w['day']==slot['day']and w['start']<=slot['start']and slot['end']<=w['end']for w in windows):unavailable=True;break
  if unavailable:continue
  for ident in demand['roomIds']:
   room=rooms[ident]
   if room['capacity']>=size and tags<={planning.canonical(x)for x in room['tags']}and not blocked(room,slot):return True
 return False

def _unplaced(snapshot,result,base):
 """Only exact local infeasibility proofs; global failures do not name culprits."""
 if not result or result.get('status')!='infeasible'or result.get('assignments'):return []
 groups={g['id']:g for g in snapshot['groups']};teachers={t['id']:t for t in snapshot['teachers']};by_id={r['id']:r for r in base['lessons']};cards=[]
 demands={d['id']:d for d in snapshot['demands']}
 for proof in result.get('diagnostics',[]):
  if proof.get('code')not in {'empty_domain','qualification'}:continue
  targets=[identifier for identifier in proof.get('entityIds',[])if identifier in demands]
  if len(targets)!=1:continue
  demand=demands[targets[0]]
  if proof['code']=='empty_domain'and _has_domain_pair(snapshot,demand):continue
  if proof['code']=='qualification'and all(planning.canonical(demand['subject'])in{planning.canonical(s)for s in teachers[x]['subjects']}for x in demand['teacherIds']):continue
  classes=sorted({groups[x]['className']for x in demand['groupIds']});names=[teachers[x]['name']for x in demand['teacherIds']];source_ids=demand.get('sourceLessonIds',[])
  exact=(len(classes)==1 and len(source_ids)==demand['weeklyCount']and len(set(source_ids))==len(source_ids)and all(identifier in by_id and by_id[identifier]['className']==classes[0]and planning.canonical(by_id[identifier]['subject'])==planning.canonical(demand['subject'])and set(by_id[identifier]['teachers'])==set(names)for identifier in source_ids))
  # A source used by another demand has no unique event mapping.
  if exact:exact=all(sum(identifier in d.get('sourceLessonIds',[])for d in demands.values())==1 for identifier in source_ids)
  for occurrence in range(demand['weeklyCount']):
   ident=source_ids[occurrence]if exact else None
   cards.append({'eventId':demand['id']+'_'+str(occurrence),'demandId':demand['id'],'subject':demand['subject'],'classNames':classes,'teachers':names,'sourceLessonIds':[ident]if ident else[],'before':_lesson(by_id[ident])if ident else None,'reason':{'code':proof['code'],'message':proof['message']},'certainty':'proven_'+proof['code']})
 return cards

def project(run_id,expected_revision,profile):
 manager,actor=_actor(profile);path=db.database_path()
 if not path.is_file():raise HTTPException(404,'Расчёт не найден')
 with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=15))as c:
  c.row_factory=sqlite3.Row;c.execute('BEGIN')
  if not pub._exists(c,'planning_runs')or not pub._exists(c,'planning_config'):raise HTTPException(404,'Расчёт не найден')
  row=planning.PlanningService._row(c,run_id,actor)
  try:
   snapshot=json.loads(row['input_json']);base=json.loads(row['base_snapshot_json']);result=json.loads(row['result_json'])if row['result_json']else None
   binding=base.get('calendarBinding')
   if not isinstance(binding,dict)or binding.get('schoolId')!=manager['schoolId']:raise HTTPException(404,'Расчёт не найден в этой школе')
   if row['status']in {'approved','rejected','stale'}or row['cancel_requested']:raise HTTPException(409,'Выберите текущий неподтверждённый кандидат; этот расчёт уже изменён или завершён')
   config=planning.PlanningService._config(c);current=planning.PlanningService._draft_snapshot(c)
   if(config['revision']!=row['config_revision']or expected_revision!=row['draft_revision']or current['revision']!=expected_revision or base['revision']!=expected_revision or current['lessons']!=base['lessons']):raise HTTPException(409,'Нормативный план или исходный черновик изменились; нужен новый расчёт')
   if planning._hash(snapshot)!=row['input_hash']or planning._hash(base)!=row['base_snapshot_hash']:raise HTTPException(409,'Исходные доказательства расчёта изменены')
   service=planning.PlanningService;service._assert_run_calendar(c,row)
   effect=service._calendar_effect(c,manager,{**config['input'],'periodStart':snapshot['periodStart']})
   if effect is None or not effect['ready']:raise HTTPException(409,'Календарь или условия расчёта больше не подтверждены')
   effective=effect['effectiveInput']
   effective['baseline']=snapshot['baseline']
   if base.get('requestBinding'):
    from .request_planning import reapply_effect
    effective=reapply_effect(c,base['requestBinding'],effective,run_id=run_id)
   if planning._hash(effective)!=row['input_hash']:raise HTTPException(409,'Сохранённый расчёт не соответствует текущим школьным условиям')
   proposed=None;changes=[];available=row['status']in planning.CANDIDATES
   if available:
    if not result or result.get('status')not in planning.CANDIDATES or result.get('validation',{}).get('valid')is not True:raise HTTPException(409,'Кандидат не имеет подтверждённой проверки')
    materialized=service._verified_materialization(row);proposed=[_lesson(r)for r in materialized]
    saved=json.loads(row['draft_diff_json']);recomputed,summary=service._draft_diff(base,materialized)
    if saved!=recomputed or summary!=json.loads(row['change_summary_json']):raise HTTPException(409,'Разница кандидата изменена')
    changes=[{**deepcopy(change),'kind':'added'if change['before']is None else'removed'if change['after']is None else'changed'}for change in saved]
   diagnostics=[{'code':d['code'],'message':d['message'],'entityIds':deepcopy(d.get('entityIds',[]))}for d in(result or{}).get('diagnostics',[])]
   # Only the solver's terminal status may supply a demand-scoped proof.
   unplaced=_unplaced(snapshot,result,base)if row['status']=='infeasible'else[]
   _actor(profile)
   return {'runId':run_id,'status':row['status'],'periodStart':snapshot['periodStart'],'baseDraftRevision':base['revision'],'currentDraftRevision':current['revision'],'candidateAvailable':available,'baseLessons':[_lesson(r)for r in base['lessons']],'proposedLessons':proposed,'changes':changes,'unplacedCards':unplaced,'diagnostics':diagnostics,'notice':NOTICE+(' Индивидуальные причины отсутствия расписания не определены.'if not available and not unplaced else'')}
  except (ValueError,KeyError,TypeError,sqlite3.Error):raise HTTPException(409,'Сохранённые сведения расчёта требуют проверки; расписание не изменено')from None

router=APIRouter(prefix='/api/planning',tags=['read-only candidate grid'],route_class=planning.SafePlanningRoute)
@router.get('/runs/{run_id}/projection')
def route(run_id:str=APIPath(min_length=1,max_length=80,pattern=r'^run_[A-Za-z0-9_-]+$'),expectedDraftRevision:int=Query(ge=0,le=2**31-1),profile=Depends(require_roles('admin','director'))):return project(run_id,expectedDraftRevision,profile)
