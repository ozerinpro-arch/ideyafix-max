"""Actual copied native auth/API tests for educational conversation adapters.

All roots are disposable, separate from the user's real MAX runtime. No provider
network, source application imports, production identities or secrets are used.
"""
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from app.platform_learning import LearningAssistant, NativeFailure
from app.platform_runtime import PlatformRuntime


class NativeLearningAdapterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='ifx-learning-test-', dir='/private/tmp')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.root.chmod(0o700)
        self.runtime = PlatformRuntime(root=self.root)
        self.addCleanup(self.runtime.close)
        self.assistant = LearningAssistant(self.runtime)
        self.session = 'synthetic-learning-session'

    def say(self, actor, text, event=None):
        reply = self.assistant.handle(actor, text, event or str(uuid4()), session_key=self.session)
        self.assertIsNotNone(reply)
        self.assertIn('text', reply)
        self.assertIsInstance(reply['buttons'], list)
        self.assertNotIn('Проверьте ответ и допустимый формат', reply['text'])
        self.assertNotIn('Действие не подтверждено', reply['text'])
        return reply

    def form(self, actor, command, answers):
        reply = self.say(actor, command)
        for answer in answers:
            reply = self.say(actor, answer)
        self.assertIn('Подтвердить', [button['label'] for button in reply['buttons']], reply)
        return self.say(actor, 'Подтвердить')

    def data(self, actor, path):
        response = self.runtime.request(actor, 'GET', path)
        self.assertEqual(response['status'], 200, response.get('data'))
        return response['data']

    def make_assignment(self, due_at='/learning skip'):
        result = self.form(710003, '/learning assign', ['1', '1', 'Доли', 'Пробел в сравнении долей',
            'Проверка долей', 'Сравните одну вторую и одну треть.', 'Одна вторая больше.', due_at])
        self.assertIn('подтверждено', result['text'])
        return self.data(710003, '/api/teacher/learning')['assignments'][-1]['id']

    def open_assignment(self, actor):
        self.say(actor, '/learning list')
        return self.say(actor, '/learning choose 1')

    def max_file_conversation(self,actor):
        from app.platform_assistant import PlatformAssistant
        from app.platform_store import PlatformStore
        from app.platform_wire import normalize_platform_update
        from tests.test_platform_store import native
        import time
        platform=PlatformAssistant(self.runtime);store=PlatformStore(self.root/('file-wire-'+str(actor)+'.sqlite3'),platform)
        def deliver(event):
            result=store.ingest(event);replies=[]
            while(item:=store.claim_next())is not None:
                replies.append(item['reply']);store.finish(item['id'],{'status':'accepted','code':'synthetic','provider_message_id':'synthetic'})
            return result,replies[-1]if replies else None
        invitation=store.issue_invite(actor);deliver(native('bot_started',payload=invitation['payload']))
        def send(text):return deliver(native(text=text))[1]
        def file_event(raw,name='text.txt',sender=123,chat=456):
            wire={'update_type':'message_created','timestamp':int(time.time()*1000),'message':{'sender':{'user_id':sender},'recipient':{'chat_type':'dialog','chat_id':chat},'body':{'mid':str(uuid4()),'attachments':[{'type':'file','filename':name,'size':len(raw),'payload':{'url':'https://files.max.ru/synthetic-test-file'}}]}}}
            event=normalize_platform_update(wire)
            # Only MAX's download I/O is substituted; wire, private owner
            # dispatch, conversations and final native API remain actual.
            event['file']={'name':name,'contentType':'application/octet-stream','base64':base64.b64encode(raw).decode()}
            return event
        return store,send,file_event,deliver

    def test_material_create_revision_export_and_exact_event_replay(self):
        self.say(710003, '/materials new')
        for text in ['1', 'Тестовый план', 'Доли', 'ИФ-7А', 'Математика', 'Научиться сравнивать',
                     '40', '1', 'Разделите фигуру на равные части.', 'Ответы для учителя']:
            last = self.say(710003, text)
        self.assertIn('Проверьте перед сохранением', last['text'])
        event = str(uuid4())
        first = self.say(710003, '/materials confirm', event)
        repeated = self.say(710003, '/materials confirm', event)
        self.assertEqual(first, repeated)
        materials = self.data(710003, '/api/teacher/preparation/materials')['materials']
        self.assertEqual(len(materials), 1)
        self.assertEqual(materials[0]['revision'], 1)
        self.say(710003, '/materials list'); self.say(710003, '/materials choose 1')
        exported = self.say(710003, '/materials export')
        self.assertEqual(exported['document']['contentType'], 'text/plain; charset=utf-8')
        result = self.form(710003, '/materials edit', ['Новая версия', 'Доли', 'ИФ-7А', 'Математика',
            'Сравнить доли', '45', '1', 'Новая последовательность заданий.', 'Новые ответы'])
        self.assertIn('подтверждено', result['text'])
        material = self.data(710003, '/api/teacher/preparation/materials')['materials'][0]
        self.assertEqual(material['revision'], 2)
        self.assertEqual(material['content'], 'Новая последовательность заданий.')

    def test_actual_max_txt_material_limits_invalid_inputs_and_lost_outer_reply(self):
        store,send,file_event,deliver=self.max_file_conversation(710003)
        send('/materials new')
        for text in ['1','Большой личный материал','Доли','ИФ-7А','Математика','Цель','40','1']:prompt=send(text)
        self.assertIn('24000',prompt['text'])
        for raw,name in [(b'\xff','invalid.txt'),(b'has\x00binary','nul.txt'),(b'a'*24001,'large.txt'),(b'text','wrong.csv')]:
            _,reply=deliver(file_event(raw,name));self.assertIn('Нужен файл .txt',reply['text'])
        self.assertEqual(self.data(710003,'/api/teacher/preparation/materials')['materials'],[])
        content='  /learning confirm\n'+'т'*(24000-len('  /learning confirm\n')-1)+'\n'
        event=file_event(content.encode(),'material.txt')
        with patch.object(store,'_paged',side_effect=RuntimeError('Synthetic lost outer file reply')):
            with self.assertRaises(RuntimeError):store.ingest(event)
        _,reply=deliver(event)
        self.assertIn('Ответы для преподавателя',reply['text'])
        self.assertIn('12000',reply['text'])
        self.assertTrue(deliver(event)[0]['duplicate'])
        answers='ОТВЕТЫ_ПЕДАГОГА_'+'я'*(12000-len('ОТВЕТЫ_ПЕДАГОГА_'))
        _,preview=deliver(file_event(answers.encode(),'answers.txt'))
        self.assertIn('Подтвердить',[button['label']for button in preview['buttons']])
        full=base64.b64decode(preview['document']['base64']).decode()
        self.assertIn(content,full);self.assertIn(answers,full)
        self.assertEqual(self.data(710003,'/api/teacher/preparation/materials')['materials'],[])
        send('/materials confirm')
        rows=self.data(710003,'/api/teacher/preparation/materials')['materials']
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['content'],content);self.assertEqual(rows[0]['answers'],answers)
        self.assertEqual(self.runtime.request(710004,'GET','/api/teacher/preparation/materials/'+rows[0]['id'])['status'],403)

    def test_actual_max_txt_student_draft_and_submit_preserve_native_8000_limit(self):
        identity=self.make_assignment();store,send,file_event,deliver=self.max_file_conversation(710004)
        send('/learning list');send('/learning choose 1');prompt=send('/learning draft')
        self.assertIn('8000',prompt['text'])
        _,invalid=deliver(file_event(b'x'*8001));self.assertIn('8000',invalid['text'])
        text='  /learning confirm\n'+'о'*(8000-len('  /learning confirm\n')-1)+'\n'
        _,preview=deliver(file_event(text.encode(),'draft.txt'))
        self.assertIn('Подтвердить',[button['label']for button in preview['buttons']])
        path='/api/student/learning/assignments/'+identity
        self.assertNotEqual(self.data(710004,path)['work']['draft']['text'],text)
        send('/learning confirm')
        self.assertEqual(self.data(710004,path)['work']['draft']['text'],text)
        teacher=self.data(710003,'/api/teacher/learning/assignments/'+identity)
        self.assertIsNone(teacher['work']['draft'])
        send('/learning list');send('/learning choose 1');send('/learning submit')
        deliver(file_event(text.encode(),'submission.txt'));send('/learning confirm')
        self.assertEqual(self.data(710004,path)['work']['attempts'][0]['text'],text)
        self.assertEqual(self.data(710004,path)['assignment']['status'],'submitted')

    def test_material_class_filter_assessment_lesson_binding_and_dashboard(self):
        self.say(710003,'/materials new')
        for text in ['3','Проверка по уроку','Доли','ИФ-7А','Математика','Проверить навык','30']:
            lesson_prompt=self.say(710003,text)
        selected=next(button for button in lesson_prompt['buttons'] if 'Математика' in button['label'])
        self.say(710003,selected['command']);self.say(710003,'Три задания по долям.');self.say(710003,'Ключи к трём заданиям.')
        self.say(710003,'/materials confirm')
        material=self.data(710003,'/api/teacher/preparation/materials')['materials'][0]
        self.assertEqual(material['kind'],'assessment')
        self.assertIsNotNone(material['lessonId'])
        self.assertEqual(material['publicationVersion'],1)
        dashboard=self.say(710003,'/materials dashboard')
        self.assertIn('Текущий урок:',dashboard['text']);self.assertIn('Ближайший урок:',dashboard['text'])
        classes=self.say(710003,'/materials classes')
        selected=next(button for button in classes['buttons'] if 'ИФ-7А' in button['label'])
        filtered=self.say(710003,selected['command'])
        self.assertIn('Проверка по уроку',filtered['text'])
        self.assertEqual(self.runtime.request(710001,'GET','/api/teacher/preparation/materials/'+material['id'])['status'],404)

    def test_checked_saved_material_handoff_requires_review_before_manual_assignment(self):
        self.form(710003,'/materials new',['3','Проверенный материал','Доли','ИФ-7А','Математика','Сравнить доли','40','1','Сравните 1/2 и 1/3.','1/2 больше 1/3.'])
        self.say(710003,'/materials list'); opened=self.say(710003,'/materials choose 1')
        self.assertIn('Перейти к выдаче задания',[button['label']for button in opened['buttons']])
        prompt=self.say(710003,'/materials assign')
        student=next(button for button in prompt['buttons']if 'Саша Демо' in button['label'])
        self.say(710003,student['command']);self.say(710003,'Проверить сравнение')
        preview=self.say(710003,'/learning skip')
        self.assertIn('Проверенный сохранённый материал',preview['text'])
        self.assertIn('1/2 больше 1/3.',preview['text'])
        self.assertEqual(self.data(710003,'/api/teacher/learning')['assignments'],[])
        self.say(710003,'/learning confirm')
        assignment=self.data(710003,'/api/teacher/learning')['assignments'][0]
        self.assertEqual(assignment['content'],'Сравните 1/2 и 1/3.')
        self.assertEqual(assignment['answers'],'1/2 больше 1/3.')
        student_view=self.data(710004,'/api/student/learning/assignments/'+assignment['id'])
        self.assertNotIn('answers',student_view['assignment'])
        policy=self.say(710003,'/learning assign_ai')
        self.assertIn('Сначала сохраните и проверьте',policy['text'])
        menu=self.say(710003,'/ai')
        self.assertNotIn('Задание с ИИ',[button['label']for button in menu['buttons']])
        self.say(710003,'/materials list');self.say(710003,'/materials choose 1')
        self.form(710003,'/materials edit',['Длинный материал','Доли','ИФ-7А','Математика','/materials skip','40','1','x'*4001,'Ключ'])
        self.say(710003,'/materials list');self.say(710003,'/materials choose 1')
        blocked=self.say(710003,'/materials assign')
        self.assertIn('Текст не сокращается автоматически',blocked['text'])
        self.assertEqual(len(self.data(710003,'/api/teacher/learning')['assignments']),1)

    def test_material_conflict_preserves_draft_compares_and_requires_new_confirmation(self):
        self.form(710003,'/materials new',['1','Исходный материал','Доли','ИФ-7А','Математика','Цель','40','1','Исходный текст','Исходные ответы'])
        material=self.data(710003,'/api/teacher/preparation/materials')['materials'][0]
        self.say(710003,'/materials list');self.say(710003,'/materials choose 1')
        self.say(710003,'/materials edit')
        for answer in ['Мой новый заголовок','Доли','ИФ-7А','Математика','Моя цель','40','1','Мой сохранённый ввод','Мои ответы']:
            self.say(710003,answer)
        external=self.runtime.request(710003,'PATCH','/api/teacher/preparation/materials/'+material['id'],payload={'expectedRevision':1,'requestId':str(uuid4()),'content':'Серверная правка другого окна'},event_id=str(uuid4()))
        self.assertEqual(external['status'],200)
        conflict=self.say(710003,'/materials confirm')
        self.assertIn('Ваш введённый текст сохранён',conflict['text'])
        comparison=self.say(710003,'/materials compare')
        self.assertIn('Серверная правка другого окна',comparison['text'])
        rebased=self.say(710003,'/materials rebase')
        self.assertIn('Мой сохранённый ввод',rebased['text'])
        self.assertEqual(self.data(710003,'/api/teacher/preparation/materials')['materials'][0]['revision'],2)
        self.say(710003,'/materials confirm')
        result=self.data(710003,'/api/teacher/preparation/materials')['materials'][0]
        self.assertEqual((result['revision'],result['content'],result['answers']),(3,'Мой сохранённый ввод','Мои ответы'))

    def test_assignment_queue_status_search_and_current_publication_next_lesson(self):
        identity=self.make_assignment()
        search=self.say(710003,'/learning search');self.assertIn('Поиск по заданию',search['text'])
        found=self.say(710003,'Саша')
        self.assertIn('Проверка долей',found['text'])
        for command in ('submitted','revision','reviewed'):
            self.assertIn('нет доступных записей',self.say(710003,'/learning '+command)['text'])
        self.open_assignment(710004);self.form(710004,'/learning submit',['Мой ответ'])
        self.assertIn('Проверка долей',self.say(710003,'/learning submitted')['text'])
        dashboard=self.data(710003,'/api/teacher/preparation')
        if dashboard['nextLesson']:
            self.assertIn('Подготовить следующий урок',[row['label']for row in self.say(710003,'/materials dashboard')['buttons']])
        else:
            blocked=self.say(710003,'/materials next')
            self.assertIn('Нет подтверждённой даты',blocked['text'])
        # Only the time-dependent next-lesson projection is synthetic here;
        # the owned lesson ID and final save still pass the real native API.
        lesson=dashboard['lessons'][0]
        original=self.runtime.request
        def projected(actor,method,path,payload=None,event_id=None):
            result=original(actor,method,path,payload=payload,event_id=event_id)
            if actor==710003 and method=='GET'and path=='/api/teacher/preparation':
                result={**result,'data':{**result['data'],'nextLesson':lesson}}
            return result
        with patch.object(self.runtime,'request',side_effect=projected):
            result=self.form(710003,'/materials next',['1','Подготовка ближайшего урока','Доли','Цель','План по уроку','/materials skip'])
        self.assertIn('подтверждено',result['text'])
        material=self.data(710003,'/api/teacher/preparation/materials')['materials'][0]
        self.assertEqual(material['lessonId'],lesson['id'])
        self.assertEqual(material['className'],lesson['className'])
        self.assertEqual(material['durationMinutes'],40)

    def test_new_confirmation_event_reuses_intent_after_committed_native_effect(self):
        self.say(710003, '/materials new')
        for text in ['1', 'Единственная запись', 'Доли', 'ИФ-7А', 'Математика', 'Цель',
                     '40', '1', 'Содержимое', 'Ответы']:
            self.say(710003, text)
        original = self.runtime.request
        requests = []
        def lose_first_reply(actor, method, path, payload=None, event_id=None):
            response = original(actor, method, path, payload, event_id)
            if method == 'POST' and path == '/api/teacher/preparation/materials':
                requests.append((event_id, payload))
                if len(requests) == 1:
                    self.assertEqual(response['status'], 200)
                    raise RuntimeError('Synthetic lost RPC reply after durable native receipt')
            return response
        with patch.object(self.runtime, 'request', side_effect=lose_first_reply):
            with self.assertRaises(RuntimeError):
                self.assistant.handle(710003, '/materials confirm', str(uuid4()), session_key=self.session)
            response = self.say(710003, '/materials confirm', str(uuid4()))
        self.assertIn('подтверждено', response['text'])
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0], requests[1])
        self.assertEqual(len(self.data(710003, '/api/teacher/preparation/materials')['materials']), 1)

    def test_actual_assignment_draft_two_attempts_review_and_message(self):
        identity = self.make_assignment()
        opened = self.open_assignment(710004)
        self.assertNotIn('Одна вторая больше.', opened['text'])
        self.form(710004, '/learning draft', ['Пока думаю над ответом.'])
        detail = self.data(710004, '/api/student/learning/assignments/' + identity)
        self.assertEqual(detail['work']['draft']['text'], 'Пока думаю над ответом.')
        self.open_assignment(710004)
        self.form(710004, '/learning submit', ['Одна треть больше.'])
        self.open_assignment(710003)
        self.form(710003, '/learning review', ['1', '2', 'Сравните одинаковые целые.'])
        self.open_assignment(710004)
        self.form(710004, '/learning submit', ['Одна вторая больше.'])
        self.open_assignment(710003)
        self.form(710003, '/learning review', ['2', '1', 'Теперь верно.'])
        self.open_assignment(710004)
        self.form(710004, '/learning message', ['Спасибо, понял.'])
        detail = self.data(710003, '/api/teacher/learning/assignments/' + identity)
        self.assertEqual(len(detail['work']['attempts']), 2)
        self.assertEqual(detail['work']['attempts'][0]['review']['decision'], 'revision_requested')
        self.assertEqual(detail['work']['attempts'][1]['review']['decision'], 'reviewed')
        self.assertEqual(detail['messages'][-1]['text'], 'Спасибо, понял.')

    def test_distinct_native_students_do_not_share_assignments_answers_or_materials(self):
        sasha_assignment = self.make_assignment()
        roster = self.data(710003, '/api/teacher/learning')['students']
        slavik_index = next(n+1 for n, row in enumerate(roster) if row['id']=='ifx-student-3')
        self.form(710003, '/learning assign', [str(slavik_index), '1', 'Личный пробел Славика',
            'Тестовая тема', 'Задание только Славику', 'SLAVIK_SYNTHETIC_CONTENT', 'TEACHER_ONLY_SYNTHETIC_ANSWERS', '/learning skip'])
        slavik_assignment = next(row['id'] for row in self.data(710003, '/api/teacher/learning')['assignments'] if row['studentId']=='ifx-student-3')
        own_sasha = self.say(710004, '/learning list')
        own_slavik = self.say(710007, '/learning list')
        self.assertNotIn('Задание только Славику', own_sasha['text'])
        self.assertNotIn('Проверка долей', own_slavik['text'])
        for actor, foreign_id in ((710004,slavik_assignment),(710007,sasha_assignment)):
            response = self.runtime.request(actor,'GET','/api/student/learning/assignments/'+foreign_id)
            self.assertEqual(response['status'],404)
            self.assertNotIn('SYNTHETIC_CONTENT',json.dumps(response.get('data')))
            self.assertEqual(self.runtime.request(actor,'GET','/api/teacher/preparation/materials')['status'],403)
        private_answers = self.data(710007,'/api/student/learning/assignments/'+slavik_assignment)
        self.assertNotIn('answers',private_answers['assignment'])
        self.assertNotIn('TEACHER_ONLY_SYNTHETIC_ANSWERS',json.dumps(private_answers))

    def test_teacher_cached_assignment_and_list_recheck_native_scope_after_pupil_archive(self):
        from app.platform_assistant import PlatformAssistant
        identity=self.make_assignment()
        platform=PlatformAssistant(self.runtime)
        def say(text,event=None):return platform.handle(710003,text,event or str(uuid4()),session_key='teacher-current-read-scope')
        listing=say('/learning list','cached-private-list')
        detail=say('/learning choose 1','cached-private-detail')
        self.assertIn('Сравните одну вторую',detail['text'])
        self.assertTrue(any(g['kind']=='native_read'and g['path'].endswith('/assignments/'+identity)for g in detail['resourceGuards']))
        self.assertTrue(any(g['kind']=='native_read'and g['path']=='/api/teacher/learning'for g in listing['resourceGuards']))
        workspace=self.data(710008,'/api/school/workspace')
        archived=self.runtime.request(710008,'POST','/api/school/records/students/ifx-student-1/archive',{'expectedRevision':workspace['revision']},str(uuid4()))
        self.assertEqual(archived['status'],200)
        self.assertEqual(self.runtime.authorize(710003)['status'],200)
        self.assertEqual(self.runtime.request(710003,'GET','/api/teacher/learning/assignments/'+identity)['status'],404)
        for command,event in [('/learning choose 1','cached-private-detail'),('/learning list','cached-private-list')]:
            reply=say(command,event)
            self.assertTrue(reply.get('resourceDenied'))
            self.assertNotIn('Сравните одну вторую',reply['text'])
            self.assertFalse(reply.get('document'))
        fresh=say('/learning list')
        self.assertFalse(fresh.get('resourceDenied'))
        self.assertIn('нет доступных записей',fresh['text'])

    def test_complete_message_history_preserves_older_messages_and_current_native_scope(self):
        from app.platform_assistant import PlatformAssistant
        identity=self.make_assignment();path='/api/student/learning/assignments/'+identity
        for number in range(8):
            detail=self.data(710004,path)
            result=self.runtime.request(710004,'POST',path+'/messages',{'expectedRevision':detail['thread']['revision'],'requestId':str(uuid4()),'text':'SYNTHETIC_HISTORY_MESSAGE_'+str(number),'askAi':False},str(uuid4()))
            self.assertEqual(result['status'],200)
        platform=PlatformAssistant(self.runtime)
        def say(actor,text,event=None):return platform.handle(actor,text,event or str(uuid4()),session_key='complete-history')
        say(710003,'/learning list');card=say(710003,'/learning choose 1')
        self.assertIn('Вся переписка',[button['label']for button in card['buttons']])
        self.assertNotIn('SYNTHETIC_HISTORY_MESSAGE_0',card['text'])
        transcript=say(710003,'/learning history','full-history-event')
        text=base64.b64decode(transcript['document']['base64']).decode()
        for number in range(8):self.assertIn('SYNTHETIC_HISTORY_MESSAGE_'+str(number),text)
        self.assertTrue(any(guard['kind']=='native_read'and guard['path'].endswith('/assignments/'+identity)for guard in transcript['resourceGuards']))
        say(710004,'/learning list');say(710004,'/learning choose 1')
        student=say(710004,'/learning history')
        student_text=base64.b64decode(student['document']['base64']).decode()
        self.assertIn('SYNTHETIC_HISTORY_MESSAGE_0',student_text)
        self.assertNotIn('Одна вторая больше.',student_text)
        workspace=self.data(710008,'/api/school/workspace')
        archived=self.runtime.request(710008,'POST','/api/school/records/students/ifx-student-1/archive',{'expectedRevision':workspace['revision']},str(uuid4()))
        self.assertEqual(archived['status'],200)
        denied=say(710003,'/learning history','full-history-event')
        self.assertTrue(denied.get('resourceDenied'));self.assertFalse(denied.get('document'))

    def test_native_published_replacement_request_share_frozen_version_and_revoke(self):
        from tests import test_platform_schedule as schedule_fixture
        schedule_fixture.prepare_published_replacement(self.runtime,replacement_teacher='Лера Демо')
        self.form(710003, '/materials new', ['1','Материал для замены','Доли','ИФ-7А','Математика',
            'Понять доли','40','1','Первая версия содержимого','Ответы первой версии'])
        self.say(710001,'/materials shares');self.say(710001,'/materials choose 1')
        self.say(710001,'/materials share_request');requested=self.say(710001,'/materials confirm')
        self.assertIn('подтверждено',requested['text'])
        self.say(710003,'/materials shares');self.say(710003,'/materials choose 1')
        self.say(710003,'/materials share_material');self.say(710003,'/materials choose 1')
        shared=self.say(710003,'/materials confirm')
        self.assertIn('подтверждено',shared['text'])
        self.say(710001,'/materials shares');self.say(710001,'/materials choose 1')
        first=self.say(710001,'/materials grant_open')
        self.assertIn('Первая версия содержимого',first['text'])
        self.say(710003,'/materials list');self.say(710003,'/materials choose 1')
        self.form(710003,'/materials edit',['Материал второй версии','Доли','ИФ-7А','Математика',
            'Новая цель','40','1','Вторая версия содержимого','Новые ответы'])
        self.say(710001,'/materials shares');self.say(710001,'/materials choose 1')
        frozen=self.say(710001,'/materials grant_open')
        self.assertIn('Первая версия содержимого',frozen['text'])
        self.assertNotIn('Вторая версия содержимого',frozen['text'])
        def native_post(path,body):
            response=self.runtime.request(710005,'POST',path,body,str(uuid4()))
            self.assertEqual(response['status'],200,response.get('data'))
            return response['data']
        status=self.data(710005,'/api/school/replacements')
        other=next(row for row in status['lessons'] if row['subject']=='Математика' and row['teachers']==['Олег Демо'])
        selected={'date':other['date'],'lessonId':other['id'],'replacementTeacher':'Марина Демо',
            'reason':'Тест новой публикации','acknowledgeUnchecked':False,
            'expectedActiveVersion':status['activeVersion'],'expectedDraftRevision':status['draftRevision']}
        preview=native_post('/api/school/replacements/preview',selected)
        self.assertTrue(preview['ready'],preview.get('blockers'))
        applied=native_post('/api/school/replacements/apply',{'input':selected,'requestId':str(uuid4()),'previewHash':preview['previewHash']})
        current=self.data(710005,'/api/school/replacements');replacement=applied['record']
        versions={'expectedActiveVersion':current['activeVersion'],'expectedDraftRevision':current['draftRevision']}
        preview=native_post('/api/school/replacements/'+replacement['id']+'/publish-preview',versions)
        self.assertTrue(preview['ready'],preview.get('blockers'))
        native_post('/api/school/replacements/'+replacement['id']+'/publish',{
            **versions,'requestId':str(uuid4()),'previewHash':preview['previewHash']})
        self.say(710001,'/materials shares');self.say(710001,'/materials choose 1')
        stale=self.say(710001,'/materials grant_open')
        self.assertIn('изменились',stale['text'])
        self.assertNotIn('Первая версия содержимого',stale['text'])
        sender_items=self.say(710003,'/materials shares')
        original_grant=next(button for button in sender_items['buttons'] if 'ИФ-7А' in button['label'])
        self.say(710003,original_grant['command'])
        self.form(710003,'/materials grant_revoke',['Тестовый отзыв доступа'])
        self.say(710001,'/materials shares');self.say(710001,'/materials choose 1')
        denied=self.say(710001,'/materials grant_open')
        self.assertIn('отозвано',denied['text'])
        history=self.say(710003,'/materials share_history')
        self.assertIn('версия материала 1',history['text'])
        entry=self.say(710003,'/materials choose 1')
        self.assertIn('Тестовый отзыв доступа',entry['text'])
        self.assertIn('прежней публикации',entry['text'])

    def test_real_native_role_guards_ai_off_and_recovery_views(self):
        denied = self.say(710004, '/materials list')
        self.assertIn('недоступна', denied['text'])
        before = len(self.data(710003, '/api/teacher/preparation/materials')['materials'])
        disabled = self.say(710003, '/ai material')
        self.assertIn('выключен', disabled['text'])
        self.assertIn('не выполнялся', disabled['text'])
        self.assertEqual(len(self.data(710003, '/api/teacher/preparation/materials')['materials']), before)
        self.make_assignment('2020-01-01T12:00:00+03:00')
        response = self.say(710004, '/recovery list')
        self.assertIn('Выберите задание', response['text'])
        self.assertIn('Проверка долей', response['text'])
        lessons = self.say(710004, '/recovery lessons')
        self.assertIn('Выберите прошедший урок', lessons['text'])
        selected = self.say(710004, '/recovery choose 1')
        self.assertIn('не привязаны к конкретному уроку', selected['text'])
        self.assertIn('Проверка долей', selected['text'])

    def test_conversation_restart_role_isolation_cancel_and_nonownership(self):
        self.say(710003, '/materials new')
        self.say(710003, '1')
        self.assistant = LearningAssistant(self.runtime)
        response = self.say(710003, 'Сохранённый ввод')
        self.assertEqual(response['text'], 'Тема')
        other = self.assistant.handle(710004, 'Чужой ответ', str(uuid4()), session_key=self.session)
        self.assertIsNone(other)
        cancelled = self.say(710003, '/cancel')
        self.assertIn('отменён', cancelled['text'])
        self.assertIsNone(self.assistant.handle(710003, '/schedule', str(uuid4()), session_key=self.session))
        self.assertEqual(self.data(710003, '/api/teacher/preparation/materials')['materials'], [])

    def test_native_scoped_journal_and_consultation_lists(self):
        for actor in (710003, 710004, 710005):
            response = self.say(actor, '/journal view')
            self.assertIn('Журнал:', response['text'])
            self.assertIn('оценок нет', response['text'])
        for actor in (710003, 710004):
            response = self.say(actor, '/consultations list')
            self.assertIn('нет доступных записей', response['text'])
        blocked = self.say(710006, '/journal view')
        self.assertIn('недоступен', blocked['text'])
        self.assertEqual(self.runtime.request(710006,'GET','/api/director/lesson-progress')['status'],403)

    def test_director_native_journal_import_and_all_form_options_are_reachable(self):
        menu=self.say(710005,'/journal')
        self.assertIn('Внести запись',[button['label']for button in menu['buttons']])
        prompt=self.say(710005,'/journal new')
        student=next(button for button in prompt['buttons']if 'Саша Демо' in button['label'])
        prompt=self.say(710005,student['command'])
        lesson=next(button for button in prompt['buttons']if 'Математика' in button['label'])
        self.say(710005,lesson['command']);self.say(710005,'1');self.say(710005,'5')
        self.say(710005,'/journal confirm')
        self.assertIn('Средняя оценка: 5',self.say(710004,'/journal view')['text'])
        self.assertEqual(self.say(710005,'/journal export')['document']['contentType'],'text/csv; charset=utf-8')
        from app.platform_learning import _field
        state={'_inputEvent':'synthetic-options','_actor':710003}
        self.assistant._form(state,'learning','assignment_search',[_field('query','Выбор',options=[('Вариант '+str(n),str(n))for n in range(1,26)])])
        page=self.assistant._advance(710003,'learning','options 2',state,'option-page')
        self.assertLessEqual(len(page['buttons']),12)
        self.assertIn('17. Вариант 17',[button['label']for button in page['buttons']])
        self.assertIn('← Назад',[button['label']for button in page['buttons']])

    def test_dynamic_native_teacher_and_student_can_use_learning_after_genuine_invites(self):
        def post(path,payload):
            result=self.runtime.request(710005,'POST',path,payload=payload,event_id=str(uuid4()))
            self.assertEqual(result['status'],200,result['data']);return result['data']
        nomination=post('/api/staff/nominations',{'username':'ifx.new.teacher','name':'Новый педагог Демо','role':'teacher','teacherRefs':['Олег Демо'],'calendarTeacherIds':['t2']})
        token=post('/api/staff/invites',{'nominationId':nomination['id']})['token']
        accepted=self.runtime.claim_native_invite(token,str(uuid4()))
        self.assertEqual(accepted['status'],200,accepted['data'])
        teacher=accepted['data']['profile']['id'];self.assertGreaterEqual(teacher,720001)
        invitation=post('/api/staff/student-access/invitations',{'studentId':'ifx-student-2','username':'ifx.new.pupil'})
        student_token=invitation['invite']['path'].split('#invite=',1)[1]
        accepted=self.runtime.claim_native_invite(student_token,str(uuid4()))
        self.assertEqual(accepted['status'],200,accepted['data'])
        student=accepted['data']['profile']['id'];self.assertNotEqual(student,teacher)
        material=self.form(teacher,'/materials new',['1','Личный новый материал','Доли','ИФ-8А','Математика','Цель','40','1','Новый личный текст','/materials skip'])
        self.assertIn('подтверждено',material['text'])
        self.assertEqual(len(self.data(teacher,'/api/teacher/preparation/materials')['materials']),1)
        self.assertEqual(self.data(710003,'/api/teacher/preparation/materials')['materials'],[])
        prompt=self.say(teacher,'/learning assign')
        selected=next(button for button in prompt['buttons']if 'Вика Демо'in button['label'])
        self.say(teacher,selected['command'])
        for answer in ['1','Сравнение долей','Проверить сравнение','Новое личное задание','Сравните доли.','Ответ только новому учителю','/learning skip']:
            self.say(teacher,answer)
        self.say(teacher,'/learning confirm')
        self.assertIn('Новое личное задание',self.open_assignment(student)['text'])
        self.assertNotIn('Ответ только новому учителю',self.open_assignment(student)['text'])
        self.form(student,'/learning submit',['Ответ новой ученицы'])
        self.assertEqual(self.data(teacher,'/api/teacher/learning')['assignments'][0]['status'],'submitted')
        self.assertEqual(self.data(710004,'/api/student/learning')['assignments'],[])
        self.assertIn('Средняя оценка',self.say(student,'/journal view')['text'])
        self.assertNotIn(token,json.dumps(material,ensure_ascii=False))
        self.assertNotIn(student_token,json.dumps(material,ensure_ascii=False))

    def test_actual_journal_guarded_import_export_and_scoped_analytics(self):
        def prepare(attendance, grade):
            student_prompt = self.say(710008,'/journal new')
            student = next(button for button in student_prompt['buttons'] if 'Саша Демо' in button['label'])
            lesson_prompt = self.say(710008,student['command'])
            self.assertTrue(all('ИФ-8А' not in button['label'] for button in lesson_prompt['buttons']))
            lesson = next(button for button in lesson_prompt['buttons'] if 'Математика' in button['label'])
            self.say(710008,lesson['command']);self.say(710008,attendance)
            return self.say(710008,grade)
        preview = prepare('1','4.5')
        self.assertIn('Штатная проверка пройдена',preview['text'])
        created = self.say(710008,'/journal confirm')
        self.assertIn('подтверждено', created['text'])
        data = self.data(710004, '/api/student/lesson-progress')
        self.assertEqual((data['summary']['recordCount'], data['summary']['averageGrade']), (1, 4.5))
        self.assertEqual(self.data(710003,'/api/teacher/lesson-progress')['summary']['averageGrade'],4.5)
        blocked = prepare('2','5')
        self.assertIn('не прошла штатную проверку', blocked['text'])
        self.assertEqual(self.data(710004, '/api/student/lesson-progress')['summary']['averageGrade'], 4.5)
        exported = self.say(710008, '/journal export')
        self.assertEqual(exported['document']['name'], 'ideyafix-journal.csv')
        csv = base64.b64decode(exported['document']['base64']).decode('utf-8-sig')
        self.assertIn('ifx-student-1', csv)
        self.assertIn(';present;4.5;', csv)
        history=self.data(710004,'/api/student/lesson-progress')['history']
        when=history['rows'][0]['lessonDate']
        filtered=self.form(710004,'/journal period',[when,when,'Математика'])
        self.assertIn('Средняя оценка: 4.5',filtered['text'])
        page=self.say(710004,'/journal history 2')
        self.assertIn('Предыдущие записи',[button['label'] for button in page['buttons']])

    def test_actual_ai_endpoints_validate_commands_and_honestly_remain_off(self):
        identity=self.make_assignment()
        detail=self.data(710004,'/api/student/learning/assignments/'+identity)
        initial_messages=len(detail['messages'])
        cases=[(710003,'material_ai',{'kind':'lesson_plan','title':'ignored','topic':'Доли','className':'ИФ-7А','subject':'Математика','objective':'','durationMinutes':40,'lessonId':None,'instructions':'Тестовый черновик'},{}),
               (710003,'assignment_create',{'studentId':'ifx-student-1','subject':'Математика','topic':'Доли','gap':'Сравнение долей','title':'Тестовый ИИ','content':'Дайте проверку','answers':'','generate':True,'dueAt':None},{}),
               (710004,'practice_create',{'subject':'Математика','topic':'Доли','instructions':'Потренировать сравнение'},{}),
               (710004,'learning_tutor',{'expectedRevision':detail['thread']['revision'],'text':'Объясните шаг'}, {'path':'/api/student/learning/assignments/'+identity})]
        for actor,operation,values,context in cases:
            with self.subTest(operation=operation):
                with self.assertRaises(NativeFailure) as error:
                    self.assistant._execute(actor,operation,values,context,str(uuid4()))
                self.assertEqual(error.exception.status,422 if operation=='assignment_create'else 503)
        self.assertEqual(self.data(710003,'/api/teacher/preparation/materials')['materials'],[])
        self.assertEqual(len(self.data(710003,'/api/teacher/learning')['assignments']),1)
        self.assertEqual(len(self.data(710004,'/api/student/learning/assignments/'+identity)['messages']),initial_messages)

    def test_stale_other_namespace_confirmation_does_not_consume_current_form(self):
        self.say(710003,'/materials new');self.say(710003,'1')
        stale=self.say(710003,'/learning confirm')
        self.assertIn('другому разделу',stale['text'])
        next_question=self.say(710003,'Сохранённое название')
        self.assertEqual(next_question['text'],'Тема')
        self.assertEqual(self.data(710003,'/api/teacher/preparation/materials')['materials'],[])

    def test_actual_clubs_charges_pdf_reconciliation_and_archive(self):
        result = self.form(710005, '/clubs program_new', ['Тестовая лаборатория', 'Марина Демо',
            'По субботам', 'Тестовый кабинет', 'Вымышленная программа', '1250.50', '1'])
        self.assertIn('подтверждено', result['text'])
        self.form(710005, '/clubs enrollment_new', ['1', '1', '1', '/clubs skip'])
        self.form(710005, '/clubs charge_new', ['1', date.today().strftime('%Y-%m'), '1'])
        charges = self.data(710005, '/api/school/clubs')['charges']
        self.assertEqual(len(charges), 1)
        self.assertEqual(charges[0]['amountKopeks'], 125050)
        self.say(710005, '/clubs charges'); self.say(710005, '/clubs choose 1')
        self.say(710005, '/clubs charge_confirm'); self.say(710005, '/clubs confirm')
        self.say(710004, '/clubs documents')
        pdf = self.say(710004, '/clubs choose 1')
        self.assertEqual(pdf['document']['contentType'], 'application/pdf')
        self.assertTrue(base64.b64decode(pdf['document']['base64'], validate=True).startswith(b'%PDF-'))
        self.say(710005, '/clubs reconcile'); self.say(710005, '/clubs choose 1')
        self.form(710005, '/clubs bind_account', ['test-account-demo'])
        self.say(710005, '/clubs reconcile'); self.say(710005, '/clubs choose 1')
        self.form(710005, '/clubs record_receipt', ['test-receipt-demo', '1000', date.today().isoformat(), '1'])
        received = self.data(710005, '/api/director/club-reconciliation')['charges'][0]
        self.assertEqual(received['receivedKopeks'], 100000)
        self.assertEqual(self.data(710004, '/api/student/clubs')['receipts'][0]['receivedKopeks'], 100000)
        self.say(710005, '/clubs charges'); self.say(710005, '/clubs choose 1')
        self.say(710005, '/clubs charge_cancel'); blocked = self.say(710005, '/clubs confirm')
        self.assertIn('изменились', blocked['text'])
        self.assertEqual(self.data(710005, '/api/school/clubs')['charges'][0]['status'], 'confirmed')
        self.say(710005, '/clubs programs'); self.say(710005, '/clubs choose 1')
        self.say(710005, '/clubs record_archive'); self.say(710005, '/clubs confirm')
        self.assertEqual(self.data(710004, '/api/student/clubs')['items'], [])
        self.say(710005, '/clubs programs'); self.say(710005, '/clubs choose 1')
        self.say(710005, '/clubs record_restore'); self.say(710005, '/clubs confirm')
        self.assertEqual(len(self.data(710004, '/api/student/clubs')['items']), 1)

    def test_cancelled_document_never_reappears_from_conversation_or_pending_max_cache(self):
        from app.platform_assistant import PlatformAssistant
        from app.platform_store import PlatformStore
        from tests.test_platform_store import native
        self.form(710005,'/clubs program_new',['Проверка актуальности PDF','Марина Демо','По средам','Тестовое место','Описание','100','1'])
        self.form(710005,'/clubs enrollment_new',['1','1','1','Участие'])
        self.form(710005,'/clubs charge_new',['1',date.today().strftime('%Y-%m'),'1'])
        self.say(710005,'/clubs charges');self.say(710005,'/clubs choose 1');self.say(710005,'/clubs charge_confirm');self.say(710005,'/clubs confirm')
        platform=PlatformAssistant(self.runtime);store=PlatformStore(self.root/'outer-cache.sqlite3',platform)
        def drain():
            results=[]
            while (item:=store.claim_next())is not None:
                results.append(item);store.finish(item['id'],{'status':'accepted','code':'synthetic','provider_message_id':'synthetic'})
            return results
        invitation=store.issue_invite(710004);store.ingest(native('bot_started',payload=invitation['payload']));drain()
        store.ingest(native(text='/clubs documents'));drain()
        lost=native(text='/clubs choose 1')
        with patch.object(store,'_paged',side_effect=RuntimeError('Synthetic lost outer transaction')):
            with self.assertRaises(RuntimeError):store.ingest(lost)
        # A second legitimate read is pending delivery when the charge changes.
        store.ingest(native(text='/clubs documents'));drain();store.ingest(native(text='/clubs choose 1'))
        with store.connection()as db:
            payload=json.loads(db.execute('SELECT p.payload FROM platform_reply_payloads p JOIN live_outbox o ON o.id=p.outbox_id WHERE o.state=? ORDER BY o.rowid DESC LIMIT 1',('queued',)).fetchone()['payload'])
        self.assertEqual(payload['resourceGuards'][0]['kind'],'club_document')
        document=self.data(710004,'/api/student/clubs')['documents'][0]
        charge=self.data(710005,'/api/school/clubs')['charges'][0]
        response=self.runtime.request(710005,'POST','/api/school/clubs/charges/'+charge['id']+'/cancel',{'expectedRevision':self.data(710005,'/api/school/clubs')['revision']},str(uuid4()))
        self.assertEqual(response['status'],200)
        self.assertEqual(self.runtime.authorize(710004)['status'],200)
        fresh=self.runtime.request(710004,'POST','/api/student/clubs/documents/prepare',{'documentId':document['id'],'revision':document['revision']},str(uuid4()))
        self.assertEqual(fresh['status'],404)
        store.ingest(lost)
        self.assertTrue(all(not item['reply'].get('document')for item in drain()))

    def test_revoked_grant_never_reappears_from_conversation_or_pending_max_cache(self):
        from app.platform_assistant import PlatformAssistant
        from app.platform_store import PlatformStore
        from tests.test_platform_store import native
        from tests.test_platform_schedule import prepare_published_replacement
        prepare_published_replacement(self.runtime,replacement_teacher='Лера Демо')
        self.form(710003,'/materials new',['1','Проверка отзыва','Доли','ИФ-7А','Математика','Цель','40','1','Отзываемый закрытый текст','Отзываемые закрытые ответы'])
        self.say(710003,'/materials shares');self.say(710003,'/materials choose 1');self.say(710003,'/materials share_material');self.say(710003,'/materials choose 1');self.say(710003,'/materials confirm')
        platform=PlatformAssistant(self.runtime);store=PlatformStore(self.root/'outer-grant-cache.sqlite3',platform)
        def drain():
            results=[]
            while (item:=store.claim_next())is not None:
                results.append(item);store.finish(item['id'],{'status':'accepted','code':'synthetic','provider_message_id':'synthetic'})
            return results
        invitation=store.issue_invite(710001);store.ingest(native('bot_started',payload=invitation['payload']));drain()
        store.ingest(native(text='/materials shares'));drain();store.ingest(native(text='/materials choose 1'));drain()
        lost=native(text='/materials grant_open')
        with patch.object(store,'_paged',side_effect=RuntimeError('Synthetic lost outer transaction')):
            with self.assertRaises(RuntimeError):store.ingest(lost)
        store.ingest(native(text='/materials grant_open'))
        with store.connection()as db:
            payload=json.loads(db.execute('SELECT p.payload FROM platform_reply_payloads p JOIN live_outbox o ON o.id=p.outbox_id WHERE o.state=? ORDER BY o.rowid DESC LIMIT 1',('queued',)).fetchone()['payload'])
        self.assertEqual(payload['resourceGuards'][0]['kind'],'material_grant')
        data=self.data(710003,'/api/teacher/replacement-materials');grant=data['items'][0]['grant']['id']
        revoke=self.runtime.request(710003,'POST','/api/teacher/replacement-materials/grants/'+grant+'/revoke',{'expectedRevision':data['revision'],'requestId':str(uuid4()),'reason':'Тестовый отзыв'},str(uuid4()))
        self.assertEqual(revoke['status'],200)
        self.assertEqual(self.runtime.authorize(710001)['status'],200)
        self.assertEqual(self.runtime.request(710001,'GET','/api/teacher/replacement-materials/grants/'+grant)['status'],403)
        store.ingest(lost)
        for item in drain():
            self.assertFalse(item['reply'].get('document'))
            self.assertNotIn('Отзываемый закрытый текст',item['reply']['text'])
            self.assertNotIn('Отзываемые закрытые ответы',item['reply']['text'])

    def test_actual_club_edits_enrollment_archive_and_positive_charge_cancel(self):
        self.form(710005,'/clubs program_new',['Кружок редактирования','Марина Демо','По средам','Тестовое место','Описание','99.50','1'])
        self.form(710005,'/clubs enrollment_new',['1','1','1','Участие'])
        self.say(710005,'/clubs programs');self.say(710005,'/clubs choose 1')
        self.form(710005,'/clubs program_edit',['Обновлённая программа','Марина Демо','По четвергам','Новое место','Новое описание','100.25','1'])
        program=self.data(710005,'/api/school/clubs')['programs'][0]
        self.assertEqual((program['title'],program['feeKopeks']),('Обновлённая программа',10025))
        self.say(710005,'/clubs enrollments');self.say(710005,'/clubs choose 1')
        self.form(710005,'/clubs enrollment_edit',['2','Временная пауза'])
        self.assertEqual(self.data(710005,'/api/school/clubs')['enrollments'][0]['status'],'paused')
        self.say(710005,'/clubs enrollments');self.say(710005,'/clubs choose 1')
        self.form(710005,'/clubs enrollment_edit',['1','Участие возобновлено'])
        self.form(710005,'/clubs charge_new',['1',date.today().strftime('%Y-%m'),'1'])
        self.say(710005,'/clubs charges');self.say(710005,'/clubs choose 1')
        self.say(710005,'/clubs charge_cancel');self.say(710005,'/clubs confirm')
        self.assertEqual(self.data(710005,'/api/school/clubs')['charges'][0]['status'],'cancelled')
        self.say(710005,'/clubs enrollments');self.say(710005,'/clubs choose 1')
        self.say(710005,'/clubs record_archive');self.say(710005,'/clubs confirm')
        self.assertEqual(self.data(710004,'/api/student/clubs')['items'],[])
        self.say(710005,'/clubs enrollments');self.say(710005,'/clubs choose 1')
        self.say(710005,'/clubs record_restore');self.say(710005,'/clubs confirm')
        self.assertEqual(len(self.data(710004,'/api/student/clubs')['items']),1)

    def test_actual_consultation_offer_book_cancel_and_time_collision_guard(self):
        own = self.data(710003, '/api/staff/my-schedule')
        week = date.fromisoformat(own['periodStart'])
        last = week + timedelta(days=6)
        start = last.isoformat() + 'T15:00:00+03:00'
        if datetime.fromisoformat(start) <= datetime.now(timezone.utc) + timedelta(minutes=2):
            # Seeded lessons cover the current week. After Sunday afternoon,
            # publish the next fixture week before exercising a future booking.
            director = 710005

            def post(path, body):
                response = self.runtime.request(director, 'POST', path, body, str(uuid4()))
                self.assertEqual(response['status'], 200, response.get('data'))
                return response['data']

            period = week + timedelta(weeks=1)
            config = self.data(director, '/api/planning/config')
            published = self.data(director, '/api/school/publications')
            run = post('/api/planning/runs', {
                'periodStart': period.isoformat(), 'mode': 'generate', 'timeLimitSeconds': 3,
                'expectedConfigRevision': config['revision'],
                'expectedDraftRevision': published['draftRevision'],
            })
            deadline = time.monotonic() + 20
            while run['status'] in {'queued', 'running'} and time.monotonic() < deadline:
                time.sleep(.05)
                run = self.data(director, '/api/planning/runs/' + run['id'])
            self.assertIn(run['status'], {'optimal', 'feasible'}, run.get('message'))
            task = post('/api/planning/runs/' + run['id'] + '/propose', {})
            self.assertEqual(post('/api/schedule-agent/tasks/' + task['id'] + '/decision',
                                  {'decision': 'approve'})['status'], 'completed')
            versions = {'runId': run['id'], 'expectedActiveVersion': published['activeVersion'],
                        'expectedDraftRevision': self.data(director, '/api/timetable')['revision']}
            preview = post('/api/school/publications/preview', versions)
            self.assertTrue(preview['ready'], preview)
            post('/api/school/publications/publish', {**versions, 'previewHash': preview['previewHash']})
            start = (period + timedelta(days=6)).isoformat() + 'T15:00:00+03:00'
        result = self.form(710003, '/consultations new', ['1', 'Обсудить доли', 'Тестовый кабинет', start, '3'])
        self.assertIn('подтверждено', result['text'])
        duplicate = self.form(710003, '/consultations new', ['1', 'Пересекающееся время', 'Тестовый кабинет', start, '3'])
        self.assertIn('изменились', duplicate['text'])
        self.assertEqual(len(self.data(710003, '/api/teacher/consultations')['items']), 1)
        self.say(710004, '/consultations list'); self.say(710004, '/consultations choose 1')
        self.say(710004, '/consultations book'); self.say(710004, '/consultations confirm')
        self.assertEqual(self.data(710003, '/api/teacher/consultations')['items'][0]['status'], 'booked')
        self.say(710003, '/consultations list'); self.say(710003, '/consultations choose 1')
        self.say(710003, '/consultations cancel_booking'); self.say(710003, '/consultations confirm')
        self.assertEqual(self.data(710004, '/api/student/consultations')['items'][0]['status'], 'cancelled')


class NativeGeneratorContractTests(unittest.TestCase):
    def test_same_fork_generator_preserves_provider_configuration_with_synthetic_model(self):
        engine = Path(__file__).resolve().parents[1] / 'platform_engine'
        code = '''
import sys,json,socket
sys.path.insert(0,sys.argv[1])
from unittest.mock import patch
from server.schedule_agent import ModelConfiguration
from server.teacher_preparation import make_generator
captured=[]
class Model:
    def invoke(self,messages,**kwargs):
        captured.append(messages)
        return json.dumps({'title':'Тестовый черновик','content':'Проверенный синтетический ответ','answers':'Ответ'},ensure_ascii=False)
configuration=ModelConfiguration(api_key='SYNTHETIC-ONLY',model='synthetic-existing-provider-model',requests_enabled=True)
def factory(value):
    assert value is configuration
    return Model()
with patch.object(socket.socket,'connect',side_effect=AssertionError('Network forbidden')):
    generator=make_generator(configuration,model_factory=factory)
    result=generator(dict(kind='assessment',topic='Доли',className='ИФ-7А',subject='Математика',objective='',durationMinutes=40,instructions='',lessonId='private-native-ref'))
    assert result['title']=='Тестовый черновик'
    assert len(captured)==1 and 'private-native-ref' not in captured[0][1]['content']
    assert make_generator(ModelConfiguration()) is None
print('PASS_SYNTHETIC_NATIVE_GENERATOR')
'''
        result = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(engine)],
            env={'PATH': os.defpath, 'PYTHON_DOTENV_DISABLED': '1'}, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, 'Native synthetic generator contract failed: ' + result.stderr[-1000:])
        self.assertEqual(result.stdout.strip(), 'PASS_SYNTHETIC_NATIVE_GENERATOR')


if __name__ == '__main__':
    unittest.main()
