"""Actual copied native auth/domain/solver in a separate child; synthetic data only."""
import base64
import hashlib
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from uuid import uuid4
from xml.sax.saxutils import escape
from zipfile import ZipFile
from unittest.mock import patch
import os

from app.platform_runtime import PlatformRuntime


class PlatformRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(dir='/private/tmp', prefix='ideyafix-native-core-tests.')
        cls.root = Path(cls.temp.name)
        cls.runtime = PlatformRuntime(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.runtime.close()
        cls.temp.cleanup()

    def call(self, actor, method, path, payload=None, event=None, expected=200):
        result = self.runtime.request(actor, method, path, payload, event)
        self.assertEqual(result['status'], expected, result['data'])
        return result

    def event(self, actor, path, payload, **kwargs):
        return self.call(actor, 'POST', path, payload, kwargs.pop('event', str(uuid4())), **kwargs)

    def test_staff_session_expiry_renews_the_same_membership_in_place(self):
        actor = 710005
        path = '/api/staff/me'
        before = self.call(actor, 'GET', path)['data']['profile']
        with sqlite3.connect(self.root / 'staff.sqlite3') as database:
            changed = database.execute('UPDATE staff_sessions SET expires=0 WHERE member_id=?',
                                       (before['membershipId'],))
            self.assertGreater(changed.rowcount, 0)

        self.assertEqual(self.runtime.authorize(actor)['status'], 200)
        self.assertEqual(self.call(actor, 'GET', path)['data']['profile'], before)

    def make_document(self, student='ifx-student-1'):
        revision = self.call(710005, 'GET', '/api/school/clubs')['data']['revision']
        value = self.event(710005, '/api/school/clubs/programs', {'expectedRevision':revision,
            'record':{'title':'Вымышленная робототехника '+str(uuid4())[:8], 'teacherName':'Марина Демо', 'feeKopeks':10000}})['data']
        value = self.event(710005, '/api/school/clubs/enrollments', {'expectedRevision':value['revision'],
            'record':{'programId':value['record']['id'],'studentId':student}})['data']
        body = {'expectedRevision':value['revision'],'enrollmentId':value['record']['id'],'period':'2026-11','quantity':1}
        preview = self.event(710005, '/api/school/clubs/charges/preview', body)['data']
        value = self.event(710005, '/api/school/clubs/charges', {**body,'expectedHash':preview['hash']})['data']
        charge = value['record']['id']
        self.event(710005, '/api/school/clubs/charges/'+charge+'/confirm', {'expectedRevision':value['revision']})
        actor = 710007 if student=='ifx-student-3' else 710004
        documents = self.call(actor, 'GET', '/api/student/clubs')['data']['documents']
        document = next(item for item in documents if item['id']==charge)
        return {'documentId':charge,'revision':document['revision']}

    def test_a_native_profiles_auth_schema_and_real_published_schedule(self):
        profiles = self.runtime.profiles()
        self.assertEqual({p['id'] for p in profiles}, set(range(710001,710010)))
        native = [self.call(p['id'],'GET','/api/staff/me')['data']['profile']
                  for p in profiles if p['role']!='platform_admin']
        self.assertEqual(len({p['membershipId'] for p in native}), 8)
        self.assertEqual({p['name'] for p in native}, {p['name'] for p in profiles if p['role']!='platform_admin'})
        self.call(710008,'GET','/api/school/workspace')
        self.assertEqual(self.runtime.authorize(710008)['status'],200)
        self.call(710005,'GET','/api/school/workspace',expected=403)
        self.call(710004,'GET','/api/school/clubs',expected=403)
        own = self.call(710004,'GET','/api/student/schedule')['data']
        self.assertTrue(own['complete']); self.assertEqual(own['blockers'],[])
        self.assertTrue(own['lessons']); self.assertEqual(own['publicationVersion'],1)
        schema = self.runtime.schema()
        self.assertGreaterEqual(len(schema['paths']),193)
        self.assertIn('/api/teacher/learning/assignments',schema['paths'])
        self.assertIn('students',schema['x-record-models']['/api/school/records'])
        self.assertIn('weeks',schema['x-record-models']['/api/school/calendar'])
        self.assertIn('properties',schema['x-settings-model'])
        resolutions=schema['x-source-resolutions-model']
        self.assertIn('properties',resolutions)
        self.assertIn('$defs',resolutions)
        self.assertIn('classMap',resolutions['properties'])
        diagnostics = self.runtime.diagnostics()
        self.assertTrue(diagnostics['loadedModules'])
        self.assertFalse(diagnostics['providerCredentialsPresent'])
        self.assertTrue(diagnostics['networkDisabled'])
        self.assertTrue(all(not Path(row['path']).is_absolute() and '..' not in Path(row['path']).parts
                            for row in diagnostics['loadedModules']))
        self.assertTrue(self.call(710003,'GET','/api/teacher/preferences')['data']['canSubmit'])
        with sqlite3.connect(self.root/'domain.sqlite3') as database:
            self.assertEqual(database.execute("SELECT COUNT(*) FROM planning_runs WHERE status='approved'").fetchone()[0],1)

    def test_b_native_learning_student_isolation_and_teacher_private_materials(self):
        body={'studentId':'ifx-student-1','subject':'Математика','topic':'Доли','gap':'Нужна практика',
              'content':'Нативное задание только для Саши','answers':'НЕ РАСКРЫВАТЬ УЧЕНИКУ','generate':False}
        assignment=self.event(710003,'/api/teacher/learning/assignments',body)['data']['assignment']['id']
        own=self.call(710004,'GET','/api/student/learning/assignments/'+assignment)
        self.assertNotIn('НЕ РАСКРЫВАТЬ',json.dumps(own,ensure_ascii=False))
        self.call(710007,'GET','/api/student/learning/assignments/'+assignment,expected=404)
        self.call(710002,'GET','/api/student/learning/assignments/'+assignment,expected=404)
        material=self.event(710003,'/api/teacher/preparation/materials',{
            'title':'Личный материал Марины','topic':'Доли','className':'ИФ-7А',
            'subject':'Математика','content':'PRIVATE_SYNTHETIC_MARINA','answers':'Только учителю'})['data']['material']['id']
        self.call(710003,'GET','/api/teacher/preparation/materials/'+material)
        self.call(710001,'GET','/api/teacher/preparation/materials/'+material,expected=404)

    def test_c_gateway_replay_conflict_and_restart_do_not_repeat_native_write(self):
        identifier='runtime-restart-'+str(uuid4())
        body={'title':'Один материал после перезапуска','topic':'Идемпотентность',
              'className':'ИФ-7А','subject':'Математика','content':'Один нативный результат'}
        first=self.event(710003,'/api/teacher/preparation/materials',body,event=identifier)
        before=(self.root/'teacher-preparation.sqlite3').read_bytes()
        self.assertEqual(self.event(710003,'/api/teacher/preparation/materials',body,event=identifier),first)
        self.event(710003,'/api/teacher/preparation/materials',{**body,'title':'Конфликт'},event=identifier,expected=409)
        self.assertEqual((self.root/'teacher-preparation.sqlite3').read_bytes(),before)
        self.runtime.close()
        type(self).runtime=PlatformRuntime(self.root)
        self.assertEqual(self.event(710003,'/api/teacher/preparation/materials',body,event=identifier),first)
        self.assertEqual((self.root/'teacher-preparation.sqlite3').read_bytes(),before)
        self.call(710003,'POST','/api/staff/logout',{},'cannot-logout',expected=403)

    def test_d_started_gateway_receipt_is_uncertain_and_never_replayed(self):
        path='/api/teacher/preparation/materials'
        body={'title':'Исходная запись для журнала','topic':'Журнал','content':'Синтетика'}
        event='gateway-source-'+str(uuid4())
        self.event(710003,path,body,event=event)
        with sqlite3.connect(self.root/'gateway.sqlite3') as database:
            fingerprint=database.execute('SELECT fingerprint FROM gateway_receipts WHERE event_id=?',(event,)).fetchone()[0]
            database.execute("INSERT INTO gateway_receipts VALUES (?,?,'started',NULL)",('uncertain-gateway-event',fingerprint))
        before=(self.root/'teacher-preparation.sqlite3').read_bytes()
        result=self.event(710003,path,body,event='uncertain-gateway-event',expected=409)
        self.assertTrue(result['data']['uncertain'])
        self.assertEqual((self.root/'teacher-preparation.sqlite3').read_bytes(),before)

    def test_timeout_after_commit_isolates_next_actor_and_replays_one_write(self):
        teacher, student = 710003, 710004
        path = '/api/teacher/preparation/materials'
        event = 'timed-out-commit-' + str(uuid4())
        body = {'title': 'Материал после тайм-аута', 'topic': 'Доли', 'content': 'Синтетический материал'}
        original_wait = self.runtime._wait
        old_process = self.runtime._process

        def expire_with_reply_waiting(fd, deadline, *, writing=False):
            original_wait(fd, deadline, writing=writing)
            if not writing:
                # The real child wrote the reply, but the caller missed its deadline.
                original_wait(fd, time.monotonic() - 1)

        with patch.object(self.runtime, '_wait', side_effect=expire_with_reply_waiting):
            with self.assertRaisesRegex(RuntimeError, '^platform_runtime_timeout$'):
                self.event(teacher, path, body, event=event)
        self.assertIsNotNone(old_process.poll())
        self.assertIsNone(self.runtime._process)
        with sqlite3.connect(self.root / 'gateway.sqlite3') as database:
            state, response = database.execute('SELECT state,response FROM gateway_receipts WHERE event_id=?',
                                               (event,)).fetchone()
        self.assertEqual(state, 'done')
        committed = json.loads(response)
        material = committed['data']['material']['id']
        pupil = self.runtime.authorize(student)
        self.assertEqual(pupil['data']['profile']['id'], student)
        self.assertNotIn('material', pupil['data'])
        self.assertIsNot(self.runtime._process, old_process)
        self.call(710001, 'GET', path + '/' + material, expected=404)
        before = (self.root / 'teacher-preparation.sqlite3').read_bytes()
        self.assertEqual(self.event(teacher, path, body, event=event), committed)
        self.event(710001, path, body, event=event, expected=409)
        self.assertEqual((self.root / 'teacher-preparation.sqlite3').read_bytes(), before)

    def test_partial_response_cannot_keep_the_channel_open_past_its_deadline(self):
        original_read = os.read
        old_process = self.runtime._process

        def partial_read(fd, size):
            return original_read(fd, size)[:7]

        with patch('app.platform_runtime.RPC_TIMEOUT', 1.5), patch('app.platform_runtime.os.read', side_effect=partial_read):
            with self.assertRaisesRegex(RuntimeError, '^platform_runtime_timeout$'):
                self.runtime.authorize(710003)
        self.assertIsNotNone(old_process.poll())
        self.assertEqual(self.runtime.authorize(710004)['data']['profile']['id'], 710004)

    def test_response_with_a_different_rpc_id_is_rejected(self):
        original_read = os.read
        old_process = self.runtime._process

        def replace_id(fd, size):
            frame = json.loads(original_read(fd, size))
            frame['rpcId'] += 1
            return (json.dumps(frame) + '\n').encode()

        with patch('app.platform_runtime.os.read', side_effect=replace_id):
            with self.assertRaisesRegex(RuntimeError, '^platform_runtime_mismatched_response$'):
                self.runtime.authorize(710003)
        self.assertIsNotNone(old_process.poll())
        self.assertEqual(self.runtime.authorize(710004)['data']['profile']['id'], 710004)

    def test_e_actual_pdf_native_roles_replay_and_cancelled_source(self):
        body=self.make_document()
        event='document-render-'+str(uuid4())
        result=self.event(710004,'/api/student/clubs/documents/prepare',body,event=event)
        raw=base64.b64decode(result['file']['base64'],validate=True)
        self.assertTrue(raw.startswith(b'%PDF-'));self.assertTrue(raw.rstrip().endswith(b'%%EOF'))
        self.assertGreater(len(raw),1000)
        self.assertEqual(result['data']['backend'],'independent-local-document-jobs')
        self.assertEqual(self.event(710004,'/api/student/clubs/documents/prepare',body,event=event),result)
        self.event(710007,'/api/student/clubs/documents/prepare',body,expected=404)
        revision=self.call(710005,'GET','/api/school/clubs')['data']['revision']
        self.event(710005,'/api/school/clubs/charges/'+body['documentId']+'/cancel',{'expectedRevision':revision})
        self.event(710004,'/api/student/clubs/documents/prepare',body,event=event,expected=404)

    def test_f_raw_native_upload_and_actual_csv_file_envelope(self):
        rows=[('Дни','Уроки','ИФ-7А'),('Понедельник','1. 8:00 - 8:40','Математика (101)\nМарина Демо')]
        rendered=['<row r="'+str(i)+'">'+''.join(
            f'<c r="{chr(65+j)}{i}" t="inlineStr"><is><t>{escape(v)}</t></is></c>'
            for j,v in enumerate(cells))+'</row>' for i,cells in enumerate(rows,1)]
        main='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
        relation='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
        buffer=BytesIO()
        with ZipFile(buffer,'w') as archive:
            archive.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
            archive.writestr('xl/workbook.xml',f'<workbook xmlns="{main}" xmlns:r="{relation}"><sheets><sheet name="Расписание" sheetId="1" r:id="rId1"/></sheets></workbook>')
            archive.writestr('xl/_rels/workbook.xml.rels',f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="{relation}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
            archive.writestr('xl/worksheets/sheet1.xml',f'<worksheet xmlns="{main}"><sheetData>{"".join(rendered)}</sheetData></worksheet>')
        document={'name':'synthetic.xlsx','contentType':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                  'base64':base64.b64encode(buffer.getvalue()).decode()}
        result=self.runtime.upload(710006,'POST','/api/timetable/preview',document,'upload-preview-'+str(uuid4()))
        self.assertEqual(result['status'],200,result['data'])
        self.assertEqual(self.runtime.upload(710006,'POST','/api/timetable/preview?filename=wrong.xlsx',document,'wrong-name')['status'],422)
        self.assertEqual(self.runtime.upload(710006,'POST','/api/school/records/students',document,'wrong-upload')['status'],422)
        exported=self.event(710005,'/api/school/data-exchange/export',{'kind':'students','format':'csv','expectedRevision':0})
        self.assertIsNotNone(exported['file']);self.assertIn('csv',exported['file']['contentType'])
        self.assertTrue(base64.b64decode(exported['file']['base64']))
        exported_json=self.event(710005,'/api/school/data-exchange/export',{'kind':'students','format':'json','expectedRevision':0})
        self.assertIsNotNone(exported_json['file']);self.assertIsNone(exported_json['data'])
        self.assertTrue(json.loads(base64.b64decode(exported_json['file']['base64'])))

    def test_g_foreign_directory_and_symlink_are_rejected_unchanged(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp',prefix='ideyafix-foreign-root.') as value:
            root=Path(value);sentinel=root/'unrelated.txt';sentinel.write_text('SYNTHETIC DO NOT CHANGE')
            before=(sentinel.read_bytes(),set(root.iterdir()))
            with self.assertRaises(ValueError):PlatformRuntime(root)
            self.assertEqual((sentinel.read_bytes(),set(root.iterdir())),before)
            link=root/'linked';link.symlink_to(self.root,target_is_directory=True)
            with self.assertRaises(ValueError):PlatformRuntime(link)

    def issue_staff_invite(self):
        username='ifx.invited.'+uuid4().hex[:12]
        nomination=self.event(710005,'/api/staff/nominations',{
            'username':username,'name':'Новый завуч Демо','role':'planner'})['data']
        invite=self.event(710005,'/api/staff/invites',{'nominationId':nomination['id']})['data']
        return invite['token'],username

    def test_h_native_invite_dynamic_profile_replay_and_restart(self):
        token,username=self.issue_staff_invite()
        result=self.runtime.claim_native_invite(token,'onboard-staff-event')
        self.assertEqual(result['status'],200)
        profile=result['data']['profile']
        self.assertGreaterEqual(profile['id'],720001);self.assertEqual(profile['role'],'planner')
        self.assertTrue(token not in json.dumps(result),'Capability must not be returned')
        self.assertEqual(self.runtime.claim_native_invite(token,'onboard-staff-new-event'),result)
        self.call(profile['id'],'GET','/api/planning/config')
        self.call(profile['id'],'GET','/api/teacher/preparation',expected=403)
        type(self).onboard_token=token;type(self).onboard_profile=profile
        self.runtime.close();type(self).runtime=PlatformRuntime(self.root)
        self.assertIn(profile,self.runtime.profiles())
        self.assertEqual(self.call(profile['id'],'GET','/api/staff/me')['data']['profile']['name'],profile['name'])
        with sqlite3.connect(self.root/'staff.sqlite3') as database:
            self.assertEqual(database.execute('SELECT COUNT(*) FROM staff_members WHERE username=?',(username,)).fetchone()[0],1)
        with sqlite3.connect(self.root/'profiles.sqlite3') as database:
            row=database.execute('SELECT password FROM profiles WHERE actor_id=?',(profile['id'],)).fetchone()
            self.assertTrue(row and len(row[0])>=32)

    def test_i_native_student_invite_preserves_mandatory_personal_binding(self):
        invitation=self.event(710005,'/api/staff/student-access/invitations',{
            'studentId':'ifx-student-2','username':'ifx.invited.pupil'})['data']
        token=invitation['invite']['path'].split('#invite=',1)[1]
        accepted=self.runtime.claim_native_invite(token,'onboard-student-event')
        self.assertEqual(accepted['status'],200)
        profile=accepted['data']['profile'];self.assertEqual(profile['role'],'student')
        self.assertEqual(profile['name'],'Вика Демо')
        self.call(profile['id'],'GET','/api/student/learning')
        own=self.call(profile['id'],'GET','/api/student/schedule')['data']
        self.assertEqual(own['student']['className'],'ИФ-8А');self.assertTrue(own['complete'])
        self.call(profile['id'],'GET','/api/teacher/learning',expected=403)

    def test_j_lost_native_accept_finish_recovers_read_only_without_second_accept(self):
        token=type(self).onboard_token;profile=type(self).onboard_profile
        digest=hashlib.sha256(token.encode()).hexdigest()
        with sqlite3.connect(self.root/'gateway.sqlite3') as database:
            database.execute("UPDATE gateway_receipts SET state='started',response=NULL WHERE event_id=?",('native-invite:'+digest,))
        with sqlite3.connect(self.root/'profiles.sqlite3') as database:
            database.execute("UPDATE profiles SET state='pending',membership_id=NULL WHERE claim_hash=?",(digest,))
        with sqlite3.connect(self.root/'staff.sqlite3') as database:
            before=database.execute("SELECT COUNT(*) FROM staff_audit WHERE action='accept'").fetchone()[0]
        result=self.runtime.claim_native_invite(token,'onboard-after-lost-response')
        self.assertEqual(result['status'],200);self.assertEqual(result['data']['profile'],profile)
        with sqlite3.connect(self.root/'staff.sqlite3') as database:
            self.assertEqual(database.execute("SELECT COUNT(*) FROM staff_audit WHERE action='accept'").fetchone()[0],before)
        self.assertIn(profile,self.runtime.profiles())

    def test_k_unknown_claim_does_not_retry_and_event_cannot_change_capability(self):
        token,username=self.issue_staff_invite()
        conflict=self.runtime.claim_native_invite(token,'onboard-staff-event')
        self.assertEqual(conflict['status'],409)
        digest=hashlib.sha256(token.encode()).hexdigest()
        with sqlite3.connect(self.root/'gateway.sqlite3') as database:
            database.execute("INSERT INTO gateway_receipts VALUES (?,?,'started',NULL)",('native-invite:'+digest,'synthetic-unknown'))
        uncertain=self.runtime.claim_native_invite(token,'onboard-uncertain-event')
        self.assertEqual(uncertain['status'],409);self.assertTrue(uncertain['data']['uncertain'])
        with sqlite3.connect(self.root/'staff.sqlite3') as database:
            self.assertEqual(database.execute('SELECT COUNT(*) FROM staff_members WHERE username=?',(username,)).fetchone()[0],0)
            self.assertEqual(database.execute('SELECT consumed FROM staff_invites WHERE token_hash=?',(digest,)).fetchone()[0],0)
        self.assertEqual(self.runtime.claim_native_invite('X'*43,'onboard-invalid-event')['status'],400)

    def test_l_native_prebuilt_proposal_does_not_get_forbidden_request_id(self):
        draft=self.call(710006,'GET','/api/timetable')['data']
        lesson=draft['lessons'][0]
        editable={key:lesson[key] for key in ('className','day','slot','start','end','subject','teachers','rooms')}
        editable['rooms']=['Кабинет 102']
        self.event(710006,'/api/schedule-agent/tasks',{
            'message':'Проверить готовое нативное предложение без автоматического requestId',
            'proposal':{'lessonId':lesson['id'],'expectedRevision':draft['revision'],'lesson':editable}})

    def test_m_explicit_nanogpt_off_config_never_reads_key_or_activates_requests(self):
        from platform_engine.provider_config import load_config
        with tempfile.TemporaryDirectory(dir='/private/tmp',prefix='ideyafix-provider-off.') as value:
            root=Path(value);key=root/'synthetic-key';key.write_text('SYNTHETIC KEY MUST NOT BE READ');key.chmod(0o600)
            config=root/'nanogpt.json'
            body={'apiKeyFile':str(key),'model':'','requestsEnabled':False}
            config.write_text(json.dumps(body));config.chmod(0o600)
            original_open=os.open
            def guarded(path,*args,**kwargs):
                if Path(path)==key:raise AssertionError('Disabled provider must not open key contents')
                return original_open(path,*args,**kwargs)
            with patch('platform_engine.provider_config.os.open',side_effect=guarded):
                self.assertFalse(load_config(config)['requestsEnabled'])
            runtime=PlatformRuntime(root/'engine',nanogpt_config=config)
            try:
                state=runtime.diagnostics()
                self.assertTrue(state['nanogpt']['configured']);self.assertFalse(state['nanogpt']['requestsEnabled'])
                self.assertTrue(state['networkDisabled']);self.assertFalse(state['providerCredentialsPresent'])
                self.assertFalse(runtime.request(710003,'GET','/api/teacher/preparation')['data']['ai']['enabled'])
            finally:runtime.close()
            config.write_text(json.dumps({**body,'requestsEnabled':True}))
            with self.assertRaises(ValueError):load_config(config)
            with self.assertRaises(RuntimeError):PlatformRuntime(root/'refused',nanogpt_config=config)

    def test_n_archived_student_domain_denies_cached_write_until_native_restore(self):
        actor=710002
        member=self.call(actor,'GET','/api/staff/me')['data']['profile']['membershipId']
        assignment=self.event(710003,'/api/teacher/learning/assignments',{
            'studentId':'ifx-student-4','subject':'Математика','topic':'Доли','gap':'Практика',
            'content':'Вымышленная работа Тимура','answers':'Ответ учителя','generate':False})['data']['assignment']
        path='/api/student/learning/assignments/'+assignment['id']+'/draft'
        body={'expectedRevision':assignment['revision'],'text':'Личный черновик ученика'}
        event='archive-domain-'+str(uuid4())
        cached=self.event(actor,path,body,event=event)
        revision=self.call(710008,'GET','/api/school/workspace')['data']['revision']
        self.event(710008,'/api/school/records/students/ifx-student-4/archive',{'expectedRevision':revision})
        with sqlite3.connect(self.root/'staff.sqlite3') as database:
            self.assertEqual(database.execute('SELECT enabled FROM staff_members WHERE id=?',(member,)).fetchone()[0],1)
        self.assertEqual(self.runtime.authorize(actor)['status'],403)
        self.event(actor,path,body,event=event,expected=403)
        self.runtime.close();type(self).runtime=PlatformRuntime(self.root)
        self.assertEqual(self.runtime.authorize(actor)['status'],403)
        self.event(actor,path,body,event=event,expected=403)
        revision=self.call(710008,'GET','/api/school/workspace')['data']['revision']
        self.event(710008,'/api/school/records/students/ifx-student-4/restore',{'expectedRevision':revision})
        self.assertEqual(self.runtime.authorize(actor)['status'],200)
        self.assertEqual(self.event(actor,path,body,event=event),cached)

    def test_o_teacher_assignment_mutation_receipt_rechecks_native_resource_access(self):
        path='/api/teacher/learning/assignments'
        body={'studentId':'ifx-student-4','subject':'Математика','topic':'Доли','gap':'Практика',
              'content':'Личный текст задания Тимура','answers':'Частный ответ учителя','generate':False}
        event='teacher-scoped-write-'+str(uuid4())
        cached=self.event(710003,path,body,event=event)
        identifier=cached['data']['assignment']['id']
        message_path=path+'/'+identifier+'/messages'
        message_body={'expectedRevision':cached['data']['assignment']['revision'],'text':'Личное пояснение учителя'}
        message_event='teacher-scoped-message-'+str(uuid4())
        message_cached=self.event(710003,message_path,message_body,event=message_event)
        learning_path=self.root/'student-learning.sqlite3'
        self.assertTrue(learning_path.exists())
        learning_before=learning_path.read_bytes()
        revision=self.call(710008,'GET','/api/school/workspace')['data']['revision']
        self.event(710008,'/api/school/records/students/ifx-student-4/archive',{'expectedRevision':revision})
        self.assertEqual(self.runtime.authorize(710003)['status'],200)
        self.call(710003,'GET',path+'/'+identifier,expected=404)
        self.event(710003,path,body,event=event,expected=404)
        self.event(710003,message_path,message_body,event=message_event,expected=404)
        self.assertEqual(learning_path.read_bytes(),learning_before)
        self.runtime.close();type(self).runtime=PlatformRuntime(self.root)
        self.event(710003,path,body,event=event,expected=404)
        self.event(710003,message_path,message_body,event=message_event,expected=404)
        revision=self.call(710008,'GET','/api/school/workspace')['data']['revision']
        self.event(710008,'/api/school/records/students/ifx-student-4/restore',{'expectedRevision':revision})
        self.assertEqual(self.event(710003,path,body,event=event),cached)
        self.assertEqual(self.event(710003,message_path,message_body,event=message_event),message_cached)
        self.assertEqual(learning_path.read_bytes(),learning_before)

    def test_z_revoked_profile_cannot_retrieve_cached_pdf_even_after_restart(self):
        body=self.make_document('ifx-student-3')
        event='revoked-document-'+str(uuid4())
        self.event(710007,'/api/student/clubs/documents/prepare',body,event=event)
        member=self.call(710007,'GET','/api/staff/me')['data']['profile']['membershipId']
        self.event(710005,'/api/staff/members/'+member+'/revoke',{})
        self.assertEqual(self.runtime.authorize(710007)['status'],401)
        self.event(710007,'/api/student/clubs/documents/prepare',body,event=event,expected=401)
        self.runtime.close();type(self).runtime=PlatformRuntime(self.root)
        self.event(710007,'/api/student/clubs/documents/prepare',body,event=event,expected=401)


if __name__=='__main__':unittest.main()
