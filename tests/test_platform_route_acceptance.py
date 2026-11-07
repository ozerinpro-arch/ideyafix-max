"""Exact active-route acceptance; native effects, current roles and receipts."""
import base64
from copy import deepcopy
from datetime import date,timedelta
import hashlib
from pathlib import Path
import re
import tempfile
import time
import unittest
from urllib.parse import urlencode,urlsplit
from uuid import uuid4

from app.platform_runtime import PlatformRuntime
from tests.test_platform_schedule import prepare_published_replacement,synthetic_timetable_document


OBSERVATIONS=[]


class NativeRouteAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir='/private/tmp',prefix='ifx-exact-route-acceptance.')
        self.addCleanup(self.temp.cleanup)
        self.runtime=PlatformRuntime(Path(self.temp.name))
        self.addCleanup(self.runtime.close)
        self.paths=list(self.runtime.schema()['paths'])

    def observe(self,method,path,value):
        actual=urlsplit(path).path
        template=next((p for p in self.paths if re.fullmatch(re.sub(r'\{[^/]+\}','[^/]+',p),actual)),actual)
        data=value.get('data')
        state=data.get('status') if isinstance(data,dict) else None
        OBSERVATIONS.append({'method':method,'path':template,'status':value['status'],
            'state':state,'test':self._testMethodName})

    def call(self,method,path,body=None,*,actor=710005,event=None,status=200):
        result=self.runtime.request(actor,method,path,body,event or (str(uuid4()) if method!='GET' else None))
        self.observe(method,path,result)
        self.assertEqual(result['status'],status,result.get('data'))
        return result

    def data(self,path,actor=710005):
        return self.call('GET',path,actor=actor)['data']

    def write(self,path,body,**kwargs):
        return self.call('POST',path,body,**kwargs)['data']

    def bind_source_lessons(self):
        config=self.data('/api/planning/config');draft=self.data('/api/timetable')
        groups={g['id']:g for g in config['input']['groups']};teachers={t['id']:t for t in config['input']['teachers']}
        for demand in config['input']['demands']:
            matching=[row for row in draft['lessons'] if row['subject']==demand['subject']
                and row['className'] in {groups[g]['className'] for g in demand['groupIds']}
                and set(row['teachers'])=={teachers[t]['name'] for t in demand['teacherIds']}]
            self.assertEqual(len(matching),1)
            demand['sourceLessonIds']=[matching[0]['id']]
        self.write('/api/planning/config',{'expectedRevision':config['revision'],
            'baseDraftRevision':draft['revision'],'input':config['input']})
        return self.data('/api/planning/config'),self.data('/api/timetable')

    def test_native_upload_preview_import_and_historical_dated_guard(self):
        config=self.data('/api/planning/config');draft=self.data('/api/timetable')
        config['input']['confirmed']=False
        self.write('/api/planning/config',{'expectedRevision':config['revision'],
            'baseDraftRevision':draft['revision'],'input':config['input']})
        document=synthetic_timetable_document()
        for operation in ['preview','import']:
            path='/api/timetable/'+operation+'?'+urlencode({'filename':document['name'],'expectedRevision':draft['revision']})
            event='route-upload-'+operation
            first=self.runtime.upload(710005,'POST',path,document,event)
            self.observe('POST',path,first);self.assertEqual(first['status'],200,first.get('data'))
            self.assertEqual(self.runtime.upload(710005,'POST',path,document,event),first)
        imported=self.data('/api/timetable')
        self.assertGreater(imported['revision'],draft['revision']);self.assertTrue(imported['lessons'])
        # An unconfirmed uploaded grid cannot borrow a normative dated basis.
        result=self.call('POST','/api/timetable/dated-draft',{
            'weekStart':'2026-10-26','expectedSourceRevision':imported['revision']},status=409)
        self.assertIsNone(result['file'])
        self.call('POST','/api/timetable/dated-draft',{
            'weekStart':'2026-10-26','expectedSourceRevision':imported['revision']},actor=710004,status=403)

    def test_native_fictional_three_day_draft_cas_replay_source_change_and_publication_block(self):
        draft=self.data('/api/timetable');config=self.data('/api/planning/config')
        config['input']['slots'].append({'id':'fictional_thursday','day':3,'slot':1,'start':'08:00','end':'08:40'})
        config['input']['pins']=[{'eventId':'d1_0','slotId':'fictional_thursday','roomId':'r1'}]
        config=self.write('/api/planning/config',{'expectedRevision':config['revision'],
            'baseDraftRevision':draft['revision'],'input':config['input']})
        run=self.write('/api/planning/runs',{'expectedConfigRevision':config['revision'],
            'expectedDraftRevision':draft['revision'],'mode':'generate','timeLimitSeconds':2})
        deadline=time.monotonic()+20
        while run['status'] in {'queued','running'} and time.monotonic()<deadline:
            time.sleep(.02);run=self.data('/api/planning/runs/'+run['id'])
        self.assertIn(run['status'],{'feasible','optimal'})
        task=self.write('/api/planning/runs/'+run['id']+'/propose',{})
        self.write('/api/schedule-agent/tasks/'+task['id']+'/decision',{'decision':'approve'})
        draft=self.data('/api/timetable');active=self.data('/api/school/publications')['active']
        path='/api/timetable/dated-draft'
        body={'weekStart':'2026-10-26','expectedSourceRevision':draft['revision'],'requestId':'route_dated_positive_0001'}
        self.call('POST',path,{**body,'expectedSourceRevision':draft['revision']-1},status=409)
        first=self.write(path,body,event='route-dated-once')
        value=first['draft']
        self.assertEqual(value['scope'],'historical_carryover');self.assertTrue(value['publicationBlocked'])
        self.assertFalse(value['sourceChanged']);self.assertTrue(value['lessons'])
        self.assertTrue(value['cancelledLessons'])
        self.assertTrue(all(row['date'] in value['teachingDates'] for row in value['lessons']))
        self.assertTrue(all(row['date'] in value['closedDates'] for row in value['cancelledLessons']))
        self.assertIn('вымышлен',value['calendar']['source'].lower())
        self.assertEqual(self.write(path,body,event='route-dated-once'),first)
        self.assertEqual(self.write(path,body,event='route-dated-new-transport-event'),first)
        self.call('POST',path,body,actor=710004,status=403)
        self.assertEqual(self.data('/api/timetable')['revision'],draft['revision'])
        self.assertEqual(self.data('/api/school/publications')['active'],active)
        package=self.data('/api/planning/sources')
        blocked=self.call('POST','/api/planning/experimental-school',{'periodStart':'2026-10-26',
            'expectedDraftRevision':draft['revision'],'expectedPackageHash':package['packageHash'],
            'useWorkingAssumptions':True},status=409)
        self.assertIn('закрыты',blocked['data']['detail'])
        config=self.data('/api/planning/config');config['input']['notes']+=' Проверка привязки основания.'
        self.write('/api/planning/config',{'expectedRevision':config['revision'],
            'baseDraftRevision':draft['revision'],'input':config['input']})
        self.assertTrue(self.data(path)['draft']['sourceChanged'])

    def test_native_from_draft_question_answer_and_mapped_substitution(self):
        config,draft=self.bind_source_lessons()
        preview=self.write('/api/planning/from-draft',{'expectedRevision':config['revision'],
            'expectedDraftRevision':draft['revision'],'periodStart':config['input']['periodStart']})
        self.assertFalse(preview['input']['confirmed']);self.assertEqual(preview['observedDraftRevision'],draft['revision'])
        self.assertEqual(self.data('/api/planning/config')['revision'],config['revision'])
        lesson=next(r for r in draft['lessons'] if r['teachers']==['Марина Демо'])
        substitution={'lessonId':lesson['id'],'expectedDraftRevision':draft['revision'],'configRevision':config['revision']}
        candidates=self.write('/api/planning/substitutions',substitution)
        self.assertTrue(candidates['candidates'],candidates)
        self.assertEqual(self.data('/api/timetable')['revision'],draft['revision'])
        self.call('POST','/api/planning/substitutions',substitution,actor=710004,status=403)
        config['input']['confirmed']=False
        changed=self.write('/api/planning/config',{'expectedRevision':config['revision'],
            'baseDraftRevision':draft['revision'],'input':config['input']})
        questions=self.write('/api/planning/questions',{'expectedConfigRevision':changed['revision']})['questions']
        self.assertTrue(questions)
        path='/api/planning/questions/'+questions[0]['id']+'/answer';body={'text':'Все сведения вымышленные, условия требуют отдельного подтверждения.'}
        answer=self.write(path,body,event='route-question-answer')
        self.assertEqual(answer['answer'],body['text']);self.assertEqual(answer['status'],'answered')
        self.assertEqual(self.write(path,body,event='route-question-answer'),answer)
        self.call('POST',path,body,actor=710004,status=403)
        self.assertTrue(any(q['id']==questions[0]['id'] and q['status']=='answered'
                            for q in self.data('/api/planning/questions')['questions']))

    def test_native_solved_run_manual_adjustment_and_cancel_are_durable(self):
        config=self.data('/api/planning/config');draft=self.data('/api/timetable')
        run=self.write('/api/planning/runs',{'expectedConfigRevision':config['revision'],
            'expectedDraftRevision':draft['revision'],'mode':'generate','timeLimitSeconds':2})
        deadline=time.monotonic()+20
        while run['status'] in {'queued','running'} and time.monotonic()<deadline:
            time.sleep(.02);run=self.data('/api/planning/runs/'+run['id'])
        self.assertIn(run['status'],{'optimal','feasible'})
        body={'expectedConfigRevision':config['revision'],'expectedDraftRevision':draft['revision'],
              'expectedRunHash':run['runHash'],'assignments':run['result']['assignments']}
        path='/api/planning/runs/'+run['id']+'/adjust'
        adjusted=self.write(path,body,event='route-adjust-once')
        self.assertNotEqual(adjusted['id'],run['id']);self.assertTrue(adjusted['result']['metrics']['manualAdjusted'])
        self.assertEqual(self.write(path,body,event='route-adjust-once'),adjusted)
        self.assertEqual(self.data('/api/timetable')['revision'],draft['revision'])
        self.call('POST',path,body,actor=710004,status=403)
        cancel='/api/planning/runs/'+adjusted['id']+'/cancel'
        cancelled=self.write(cancel,{},event='route-cancel-once')
        self.assertEqual(cancelled['status'],'interrupted')
        self.assertEqual(self.write(cancel,{},event='route-cancel-once'),cancelled)
        self.assertEqual(self.data('/api/planning/runs/'+adjusted['id'])['status'],'interrupted')
        self.call('POST',cancel,{},actor=710004,status=403)
        self.call('POST','/api/planning/runs/'+adjusted['id']+'/propose',{},status=409)

    def test_native_student_dated_membership_binding_is_current_scoped_and_replayed(self):
        schedule=self.data('/api/student/schedule',710004)
        lesson=schedule['lessons'][0]
        day=(date.fromisoformat(schedule['periodStart'])+timedelta(days=lesson['day'])).isoformat()
        query={'studentId':'ifx-student-1','publicationVersion':schedule['publicationVersion'],'lessonDate':day}
        path='/api/staff/student-schedule-bindings/dated'
        details=self.data(path+'?'+urlencode(query))
        self.assertTrue(details['ready']);self.assertEqual(details['student']['id'],'ifx-student-1')
        before=details['binding']['revision']
        body={**query,'expectedRevision':before,'groupIds':[]}
        saved=self.write(path,body,event='route-dated-binding-once')
        self.assertGreater(saved['binding']['revision'],before)
        self.assertEqual(self.write(path,body,event='route-dated-binding-once'),saved)
        self.assertEqual(self.data(path+'?'+urlencode(query))['binding'],saved['binding'])
        self.call('GET',path+'?'+urlencode(query),actor=710004,status=403)
        self.call('POST',path,body,actor=710003,status=403)

    def test_native_teacher_legacy_absence_is_owned_and_durably_stored(self):
        own=self.data('/api/staff/my-schedule',710003)
        self.assertTrue(own['lessons']);lesson=own['lessons'][0]
        published=self.data('/api/school/publications')['active']
        day=(date.fromisoformat(published['periodStart'])+timedelta(days=lesson['day'])).isoformat()
        body={'lessonIds':[lesson['id']],'startDate':day,'endDate':day,'note':'Вымышленная причина отсутствия'}
        response=self.write('/api/staff/my-absence',body,actor=710003,event='route-legacy-absence-once')
        self.assertEqual(self.write('/api/staff/my-absence',body,actor=710003,event='route-legacy-absence-once'),response)
        absences=self.data('/api/staff/absences')
        self.assertIn('Вымышленная причина отсутствия',str(absences))
        self.assertEqual(str(absences).count('Вымышленная причина отсутствия'),1)
        self.call('POST','/api/staff/my-absence',body,actor=710004,status=403)

    def test_native_templates_header_ai_off_and_missing_legacy_outbox_refusal(self):
        for kind,actor in [('students',710008),('lessonProgress',710005)]:
            response=self.call('GET','/api/school/data-exchange/template/'+kind,actor=actor)
            self.assertIsNotNone(response['file'])
            self.assertTrue(base64.b64decode(response['file']['base64']))
        self.call('GET','/api/school/data-exchange/template/students',actor=710004,status=403)
        before=hashlib.sha256((self.runtime.root/'domain.sqlite3').read_bytes()).hexdigest()
        analysis=self.write('/api/school/data-exchange/analyze-headers',{'kind':'students','headers':['Имя','Класс']})
        self.assertEqual(analysis['status'],'unavailable');self.assertEqual(analysis['mapping'],{})
        self.assertEqual(hashlib.sha256((self.runtime.root/'domain.sqlite3').read_bytes()).hexdigest(),before)
        self.call('POST','/api/school/data-exchange/analyze-headers',{'kind':'students','headers':['Имя']},actor=710004,status=403)
        self.assertEqual(self.data('/api/launch/outbox')['items'],[])
        self.call('POST','/api/launch/outbox/no-synthetic-legacy-item/dry-run',{'expectedRevision':0},status=404)
        self.call('POST','/api/launch/outbox/no-synthetic-legacy-item/dry-run',{'expectedRevision':0},actor=710004,status=403)

    def test_native_manager_replacement_outcome_report_stored_and_replayed(self):
        fixture=prepare_published_replacement(self.runtime)
        own=self.data('/api/teacher/replacement-outcomes',710001)
        target=next(row['target'] for row in own['items'] if row['target']['replacementId']==fixture['record']['id'])
        path='/api/school/replacement-outcomes'
        body={'expectedRevision':0,'target':target,'kind':'not_conducted','reason':'Вымышленное занятие: менеджер сообщил о непроведении'}
        saved=self.write(path,body,event='route-manager-outcome-once')
        self.assertEqual(self.write(path,body,event='route-manager-outcome-once'),saved)
        current=self.data(path)
        self.assertIn('Вымышленное занятие',str(current))
        reported=next(row['outcome'] for row in current['items'] if row['target']['replacementId']==fixture['record']['id'])
        self.assertEqual(reported['pending']['kind'],'not_conducted')
        self.call('POST',path,body,actor=710004,status=403)

    def test_native_deterministic_agent_clarification_reply_and_rule_archive(self):
        task=self.write('/api/schedule-agent/tasks',{
            'message':'Преподаватель недоступен по понедельникам','ruleScope':'persistent'},event='route-standing-rule-create')
        self.assertEqual(task['status'],'needs_clarification');self.assertTrue(task['replyAllowed'])
        path='/api/schedule-agent/tasks/'+task['id']+'/reply'
        body={'message':'Марина Демо недоступна по понедельникам','expectedTurn':task['turn']}
        clarified=self.write(path,body,event='route-standing-rule-reply')
        self.assertEqual(clarified['status'],'awaiting_approval');self.assertEqual(clarified['turn'],task['turn']+1)
        self.assertEqual(self.write(path,body,event='route-standing-rule-reply'),clarified)
        self.call('POST',path,body,actor=710004,status=403)
        accepted=self.write('/api/schedule-agent/tasks/'+task['id']+'/decision',{'decision':'approve'})
        self.assertTrue(accepted['ruleApplied'])
        collection=self.data('/api/schedule-agent/rules');identifier=accepted['ruleReceipt']['ruleIds'][0]
        entry=next(row for row in collection['rules'] if row['id']==identifier)
        self.assertFalse(entry['archived'])
        path='/api/schedule-agent/rules/'+identifier+'/archive';body={'expectedRevision':collection['revision']}
        archived=self.write(path,body,event='route-standing-rule-archive')
        self.assertTrue(next(row for row in archived['rules'] if row['id']==identifier)['archived'])
        self.assertEqual(self.write(path,body,event='route-standing-rule-archive'),archived)
        self.call('POST',path,body,actor=710004,status=403)

    def test_native_request_linked_replacement_candidates_are_preview_only(self):
        self.bind_source_lessons()
        context=self.data('/api/school/replacements')
        lesson=next(row for row in context['lessons'] if row['teachers']==['Марина Демо'])
        marker='Вымышленная недоступность для точного подбора замены'
        self.write('/api/school/requests',{'kind':'unavailability','date':lesson['date'],
            'lessonIds':[lesson['id']],'text':marker},actor=710003)
        request=next(row for row in self.data('/api/school/requests')['requests'] if row['text']==marker)
        body={'date':lesson['date'],'lessonId':lesson['id'],'expectedActiveVersion':context['activeVersion'],
              'expectedDraftRevision':context['draftRevision'],'schoolRequestId':request['id'],
              'expectedSchoolRequestRevision':request['revision']}
        candidates=self.write('/api/school/replacements/candidates',body)
        self.assertTrue(candidates['candidates'],candidates)
        self.assertIn('Лера Демо',str(candidates['candidates']))
        self.assertEqual(self.data('/api/timetable')['revision'],context['draftRevision'])
        self.call('POST','/api/school/replacements/candidates',body,actor=710004,status=403)


if __name__=='__main__':
    unittest.main()
