from pathlib import Path
from tempfile import TemporaryDirectory, gettempdir
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient

from app.school_demo import COOKIE, create_app
from tests.test_platform_schedule import synthetic_timetable_document


ORIGIN = 'http://127.0.0.1:5212'


class SchoolDemoTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(prefix='ifx-school-demo-test-', dir=Path(gettempdir()).resolve())
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.app = create_app(self.root)
        self.client = self.enterContext(TestClient(self.app, base_url=ORIGIN))

    def state(self, role, client=None):
        response = (client or self.client).get('/api/demo/state', params={'role': role})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def post(self, role, text, event_id=None):
        return self.client.post('/api/demo/action', headers={'Origin': ORIGIN}, json={
            'role': role, 'text': text, 'event_id': event_id or str(uuid4()),
        })

    def say(self, role, text, event_id=None):
        response = self.post(role, text, event_id)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['reply']

    def test_origin_host_validation_and_missing_session_do_not_create_a_workspace(self):
        path = '/api/demo/state?role=teacher'
        bad_host = self.client.get(path, headers={'Host': 'other.example:5212'})
        self.assertEqual(bad_host.status_code, 403)
        self.assertIn("frame-ancestors 'none'", bad_host.headers['Content-Security-Policy'])
        self.assertEqual(bad_host.headers['Cache-Control'], 'no-store')
        self.assertEqual(self.client.get(path, headers={'Origin': 'http://localhost:5212'}).status_code, 403)
        self.assertEqual(self.client.get(path, headers={'Sec-Fetch-Site': 'same-site'}).status_code, 403)
        body = {'role': 'teacher', 'text': '/home', 'event_id': str(uuid4())}
        self.assertEqual(self.client.post('/api/demo/action', json=body).status_code, 403)
        self.assertEqual(self.client.post('/api/demo/action', json=body, headers={'Origin': 'https://other.example'}).status_code, 403)
        self.assertEqual(self.post('teacher', '/home').status_code, 403)
        self.assertEqual(self.post('platform_admin', '/home').status_code, 422)
        self.assertEqual(self.post('teacher', '/home', 'not-a-uuid').status_code, 422)
        self.assertEqual(self.client.post('/api/demo/action', content=' ' * 32769, headers={
            'Origin': ORIGIN, 'Content-Type': 'application/json',
        }).status_code, 413)
        self.assertEqual(self.client.get('/api/max/status').status_code, 404)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_four_native_roles_have_distinct_state_and_personal_assignments(self):
        first = self.client.get('/api/demo/state?role=teacher')
        self.assertEqual(first.status_code, 200, first.text)
        self.assertIn(f'{COOKIE}=', first.headers['Set-Cookie'])
        self.assertIn('HttpOnly', first.headers['Set-Cookie'])
        self.assertIn('SameSite=strict', first.headers['Set-Cookie'])
        teacher = first.json()
        student = self.state('student')
        planner = self.state('planner')
        director = self.state('director')
        self.assertEqual({row['role'] for row in teacher['profiles']}, {'teacher', 'student', 'planner', 'director'})
        self.assertEqual(len({state['profile']['id'] for state in (teacher, student, planner, director)}), 4)
        self.assertEqual(len(teacher['assignments']), 2)
        self.assertEqual(len(student['assignments']), 1)
        self.assertEqual(student['assignments'][0]['title'], 'Саше: найти половину')
        self.assertNotIn('answers', student['assignments'][0])
        self.assertNotIn('answers', teacher['assignments'][0])
        self.assertEqual(student['sections'], {})
        self.assertEqual(teacher['sections'], {})
        self.assertTrue(teacher['timetable']['lessons'])
        self.assertTrue(student['timetable']['lessons'])
        self.assertTrue(planner['timetable']['publicationVersion'])
        self.assertEqual(planner['timetable'], director['timetable'])
        self.assertTrue(planner['sections']['draftTimetable']['lessons'])
        self.assertEqual(set(director['sections']), {'draftTimetable'})
        materials = self.say('teacher', '/materials list')
        self.assertIn('Демо: сравниваем доли', str(materials['buttons']))
        self.assertEqual(self.post('director', '/max').status_code, 403)
        self.assertFalse({'/max', '/hackathon', '/people', '/role'} & {
            button['command'] for button in director['menu']['buttons']
        })

    def test_native_assignment_effect_replay_and_browser_workspace_isolation(self):
        self.state('teacher')
        self.say('teacher', '/learning assign')
        self.state('teacher')  # State refresh must leave the live form open.
        self.assertEqual(len(self.state('student')['assignments']), 1)
        for value in ('1', '1', 'Доли', 'Сравнение', 'Локальная проверка долей',
                      'Сравните половину и треть.', 'Ответ преподавателя', '/learning skip'):
            reply = self.say('teacher', value)
        self.assertIn('/learning confirm', {button['command'] for button in reply['buttons']})
        confirm_event = str(uuid4())
        confirmed = self.say('teacher', '/learning confirm', confirm_event)
        self.assertIn('подтверждено', confirmed['text'])
        self.assertEqual(self.say('teacher', '/learning confirm', confirm_event), confirmed)
        self.assertEqual(self.post('teacher', '/home', confirm_event).status_code, 409)
        personal = self.state('student')['assignments']
        self.assertEqual(len(personal), 2)
        self.assertIn('Локальная проверка долей', {row['title'] for row in personal})
        self.assertNotIn('Ответ преподавателя', str(personal))
        second_browser = TestClient(self.app, base_url=ORIGIN)
        self.addCleanup(second_browser.close)
        self.assertEqual(len(self.state('student', second_browser)['assignments']), 1)
        self.assertNotEqual(self.client.cookies[COOKIE], second_browser.cookies[COOKIE])
        self.assertEqual(len(list(self.root.iterdir())), 2)

    def test_native_xlsx_preview_from_local_cabinet(self):
        initial = self.state('director')['sections']['draftTimetable']
        menu = self.say('director', '/schedule menu')
        preview = next(button['command'] for button in menu['buttons'] if 'Предпросмотр' in button['label'])
        self.assertIn('Прикрепите XLSX', self.say('director', preview)['text'])
        body = {'role': 'director', 'document': synthetic_timetable_document(), 'event_id': str(uuid4())}
        self.assertEqual(self.client.post('/api/demo/upload', json=body).status_code, 403)
        response = self.client.post('/api/demo/upload', json=body, headers={'Origin': ORIGIN})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('/schedule confirm', {button['command'] for button in response.json()['reply']['buttons']})
        result = self.say('director', '/schedule confirm')
        self.assertNotIn('не подтверждено', result['text'])
        self.assertEqual(self.state('director')['sections']['draftTimetable'], initial)
