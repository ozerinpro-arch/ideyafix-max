"""Black-box synthetic Ideyafix flows; no school data, SQL assertions or network."""
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import create_app
from app.transport import SyntheticMax


ORIGIN = 'http://127.0.0.1:5211'
LERA, TIMUR, MARINA, SASHA, ROMAN = 710001, 710002, 710003, 710004, 710005


class IdeyafixFlows(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(
            dir='/private/tmp', prefix='ideyafix-flow-tests.')))
        self.database = self.root / 'synthetic-demo.sqlite3'
        for name in ('socket.socket.connect', 'socket.create_connection', 'socket.getaddrinfo'):
            self.stack.enter_context(patch(name, side_effect=AssertionError('External network forbidden in synthetic flows')))
        self.client = self.open_client()

    def open_client(self):
        self.transport = SyntheticMax()
        self.stack.callback(self.transport.close)
        return self.stack.enter_context(TestClient(create_app(db_path=self.database, transport=self.transport), base_url=ORIGIN))

    def state(self, actor):
        response = self.client.get('/api/demo/state', params={'actor_id': actor})
        self.assertEqual(response.status_code, 200)
        result = response.json()
        for key in ('messages', 'teams', 'assignments', 'quickActions'):
            self.assertIn(key, result)
        return result

    def event(self, actor, text, *, event_id=None, status=200, headers=None):
        response = self.client.post('/api/demo/events', json=dict(
            actor_id=actor, text=text, event_id=event_id or str(uuid4())),
            headers={'Origin': ORIGIN, **(headers or {})})
        self.assertEqual(response.status_code, status)
        if status != 200:
            return response
        result = response.json()
        self.assertIsInstance(result.get('reply'), str)
        self.assertIsInstance(result.get('duplicate'), bool)
        self.assertIsInstance(result.get('state'), dict)
        return result

    def team(self, actor, name):
        rows = [row for row in self.state(actor)['teams'] if row['name'] == name]
        self.assertEqual(len(rows), 1)
        return rows[0]

    def assignment(self, actor, identifier):
        rows = [row for row in self.state(actor)['assignments'] if row['id'] == identifier]
        self.assertEqual(len(rows), 1)
        return rows[0]

    def domain(self, actor):
        state = self.state(actor)
        return {key: deepcopy(state[key]) for key in ('teams', 'assignments')}

    def test_bootstrap_is_explicitly_synthetic_and_has_five_distinct_roles(self):
        response = self.client.get('/api/demo/bootstrap')
        self.assertEqual(response.status_code, 200)
        actors = response.json()['actors']
        self.assertNotIn('Школа.Действие', json.dumps(response.json(), ensure_ascii=False))
        self.assertEqual({row['id'] for row in actors}, {LERA, TIMUR, MARINA, SASHA, ROMAN})
        self.assertEqual(len(actors), 5)
        roles = {row['id']: row['role'] for row in actors}
        self.assertEqual(roles, {LERA: 'participant', TIMUR: 'participant', MARINA: 'mentor', SASHA: 'learner', ROMAN: 'organizer'})
        self.assertTrue(all(row.get('name') and row.get('label') for row in actors))
        for actor in roles:
            self.event(actor, '/start')

    def test_hackathon_team_join_idea_apply_revision_and_organizer_acceptance(self):
        title = 'Северная команда'
        self.event(LERA, '/hackathon')
        self.event(LERA, '/team ' + title)
        first = self.team(LERA, title)
        self.assertTrue(first['code'])
        self.assertEqual(len(first['members']), 1)
        self.event(TIMUR, '/join ' + first['code'])
        joined = self.team(TIMUR, title)
        self.assertEqual(joined['id'], first['id'])
        self.assertEqual(len(joined['members']), 2)
        self.assertEqual(len(set(joined['members'])), 2)
        self.event(LERA, '/idea Тёплый город | Карта доступных общественных пространств')
        idea = self.team(TIMUR, title)['idea']
        self.assertEqual(idea, dict(title='Тёплый город', description='Карта доступных общественных пространств'))
        self.event(TIMUR, '/apply')
        applied = self.team(LERA, title)['application']
        self.assertEqual(applied['status'], 'submitted')
        self.event(ROMAN, '/applications')
        self.assertEqual(self.team(ROMAN, title)['application']['id'], applied['id'])
        self.event(ROMAN, '/return ' + applied['id'] + ' | Уточните пользу для участников')
        returned = self.team(LERA, title)['application']
        self.assertEqual(returned['status'], 'revision')
        self.assertEqual(returned['comment'], 'Уточните пользу для участников')
        self.event(LERA, '/idea Тёплый город | Участники находят бесплатные площадки рядом с собой')
        self.event(LERA, '/apply')
        reapplied = self.team(TIMUR, title)['application']
        self.assertEqual(reapplied['status'], 'submitted')
        self.event(ROMAN, '/approve ' + reapplied['id'])
        self.assertEqual(self.team(LERA, title)['application']['status'], 'accepted')
        self.assertEqual(self.team(TIMUR, title)['application']['status'], 'accepted')

    def test_event_receipt_deduplicates_team_join_and_rejects_conflicting_body(self):
        event_id = str(uuid4())
        original = self.event(LERA, '/team Одна команда', event_id=event_id)
        before = self.state(LERA)
        sends = self.transport.call_count
        replay = self.event(LERA, '/team Одна команда', event_id=event_id)
        self.assertFalse(original['duplicate'])
        self.assertTrue(replay['duplicate'])
        self.assertEqual(self.state(LERA), before)
        self.assertEqual(self.transport.call_count, sends)
        self.event(LERA, '/team Другая команда', event_id=event_id, status=409)
        self.assertEqual(self.state(LERA), before)
        self.assertEqual(self.transport.call_count, sends)
        timur_before = self.state(TIMUR)
        self.event(TIMUR, '/team Одна команда', event_id=event_id, status=409)
        self.assertEqual(self.state(TIMUR), timur_before)
        self.assertEqual(self.transport.call_count, sends)
        team = self.team(LERA, 'Одна команда')
        join_id = str(uuid4())
        self.event(TIMUR, '/join ' + team['code'], event_id=join_id)
        before = self.state(TIMUR)
        sends = self.transport.call_count
        self.assertTrue(self.event(TIMUR, '/join ' + team['code'], event_id=join_id)['duplicate'])
        self.assertEqual(self.state(TIMUR), before)
        self.assertEqual(self.transport.call_count, sends)
        self.assertEqual(len(self.team(LERA, 'Одна команда')['members']), 2)

    def test_education_two_attempts_review_and_mentor_created_task(self):
        self.event(SASHA, '/education')
        self.event(SASHA, '/lessons')
        rows = self.state(SASHA)['assignments']
        self.assertGreaterEqual(len(rows), 1)
        seeded = rows[-1]
        self.assertEqual(seeded['learner']['id'], SASHA)
        self.assertEqual(seeded['mentor']['id'], MARINA)
        self.assertEqual(seeded['state'], 'assigned')
        self.event(SASHA, '/submit Первое решение: наблюдение без вывода')
        first = self.assignment(MARINA, seeded['id'])
        self.assertEqual(first['state'], 'submitted')
        self.assertEqual(first['submission']['attempt'], 1)
        self.assertEqual(first['submission']['text'], 'Первое решение: наблюдение без вывода')
        self.event(MARINA, '/review доработать | Добавь вывод и один пример')
        revision = self.assignment(SASHA, seeded['id'])
        self.assertEqual(revision['state'], 'revision')
        self.assertEqual(revision['review']['decision'], 'revision')
        self.assertEqual(revision['review']['comment'], 'Добавь вывод и один пример')
        request_id = str(uuid4())
        self.event(SASHA, '/submit Второе решение: вывод и конкретный пример', event_id=request_id)
        self.assertTrue(self.event(SASHA, '/submit Второе решение: вывод и конкретный пример', event_id=request_id)['duplicate'])
        second = self.assignment(MARINA, seeded['id'])
        self.assertEqual(second['state'], 'submitted')
        self.assertEqual(second['submission']['attempt'], 2)
        self.event(MARINA, '/review принять | Аргументировано и понятно')
        accepted = self.assignment(SASHA, seeded['id'])
        self.assertEqual(accepted['state'], 'accepted')
        self.assertEqual(accepted['review']['decision'], 'accepted')
        self.assertEqual(accepted['review']['comment'], 'Аргументировано и понятно')
        self.event(SASHA, '/progress')
        self.event(MARINA, '/task Новый эксперимент | Сравни два подхода и объясни выбор')
        created = [row for row in self.state(SASHA)['assignments'] if row['title'] == 'Новый эксперимент']
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]['state'], 'assigned')
        self.assertEqual(created[0]['instructions'], 'Сравни два подхода и объясни выбор')
        self.assertEqual(created[0]['mentor']['id'], MARINA)
        self.assertEqual(created[0]['learner']['id'], SASHA)

    def test_concurrent_same_event_has_one_domain_action_and_one_transport_call(self):
        identifier = str(uuid4())
        body = dict(actor_id=LERA, text='/team Параллельная команда', event_id=identifier)
        def post_once(_):
            response = self.client.post('/api/demo/events', json=body, headers={'Origin': ORIGIN})
            self.assertEqual(response.status_code, 200)
            return response.json()
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(post_once, range(5)))
        self.assertEqual(sum(not result['duplicate'] for result in results), 1)
        self.assertEqual(self.transport.call_count, 1)
        self.assertEqual(len([row for row in self.state(LERA)['teams'] if row['name'] == 'Параллельная команда']), 1)

    def test_role_denials_are_bot_feedback_without_domain_mutation(self):
        self.event(LERA, '/team Команда проверки ролей')
        self.event(LERA, '/idea Понятная идея | Её описание')
        self.event(LERA, '/apply')
        application = self.team(LERA, 'Команда проверки ролей')['application']['id']
        for actor, command in (
                (LERA, '/approve ' + application), (TIMUR, '/return ' + application + ' | Не моя роль'),
                (MARINA, '/team Команда наставника'), (ROMAN, '/submit Чужое решение'),
                (SASHA, '/task Нельзя | Выдать себе задание'), (LERA, '/review принять | Не мой ученик')):
            before = {who: self.domain(who) for who in (LERA, TIMUR, MARINA, SASHA, ROMAN)}
            result = self.event(actor, command)
            self.assertTrue(result['reply'].strip())
            self.assertEqual({who: self.domain(who) for who in before}, before)

    def test_learner_work_and_personal_transcript_are_not_exposed_to_hackathon_actors(self):
        secret = 'СИНТЕТИЧЕСКОЕ_ЛИЧНОЕ_РЕШЕНИЕ_86291'
        self.event(SASHA, '/submit ' + secret)
        self.assertIn(secret, json.dumps(self.state(SASHA), ensure_ascii=False))
        self.assertIn(secret, json.dumps(self.state(MARINA), ensure_ascii=False))
        for actor in (LERA, TIMUR, ROMAN):
            view = self.state(actor)
            self.assertEqual(view['assignments'], [])
            self.assertNotIn(secret, json.dumps(view, ensure_ascii=False))
        learner_before = self.domain(SASHA)
        self.event(LERA, '/submit Попытка участника изменить учебную работу')
        self.assertEqual(self.domain(SASHA), learner_before)

    def test_receipts_and_domain_state_survive_a_new_app_instance(self):
        identifier = str(uuid4())
        self.event(LERA, '/team Сохранённая команда', event_id=identifier)
        self.event(SASHA, '/submit Сохранённое учебное решение')
        before = {actor: self.state(actor) for actor in (LERA, SASHA, MARINA)}
        self.client = self.open_client()
        self.assertEqual(self.transport.call_count, 0)
        self.assertEqual({actor: self.state(actor) for actor in before}, before)
        self.assertEqual(self.transport.call_count, 0)
        self.assertTrue(self.event(LERA, '/team Сохранённая команда', event_id=identifier)['duplicate'])
        self.assertEqual({actor: self.state(actor) for actor in before}, before)
        self.assertEqual(self.transport.call_count, 0)

    def test_origin_host_unknown_actor_and_invalid_events_are_rejected_without_mutation(self):
        before = self.state(LERA)
        self.event(999999, '/start', status=422)
        self.event(LERA, 'x' * 100000, status=413)
        self.event(LERA, 'x' * 2001, status=422)
        self.event(LERA, '', status=422)
        self.event(LERA, '/start', event_id='not-a-uuid', status=422)
        self.event(LERA, '/team Атака', status=403, headers={'Origin': 'https://foreign.example'})
        self.event(LERA, '/team Атака', status=403, headers={'Host': 'foreign.example'})
        response = self.client.post('/api/demo/events', json=dict(actor_id=LERA, text='/start', event_id=str(uuid4())))
        self.assertEqual(response.status_code, 403)
        response = self.client.post('/api/demo/events', json=dict(actor_id=LERA, text='/start', event_id=str(uuid4()), role='organizer'), headers={'Origin': ORIGIN})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.client.get('/api/demo/state', params={'actor_id': 999999}).status_code, 422)
        self.assertEqual(self.state(LERA), before)

    def test_foreign_sqlite_is_rejected_without_data_schema_or_permission_changes(self):
        path = self.root / 'unrelated.sqlite3'
        with sqlite3.connect(path) as connection:
            connection.execute('CREATE TABLE unrelated_records(payload TEXT NOT NULL)')
            connection.execute('INSERT INTO unrelated_records VALUES (?)', ('SYNTHETIC DO NOT CHANGE',))
        path.chmod(0o640)
        before = path.read_bytes(), path.stat().st_mode, set(self.root.iterdir())
        with self.assertRaises(ValueError): create_app(db_path=path)
        self.assertEqual((path.read_bytes(), path.stat().st_mode, set(self.root.iterdir())), before)

    def test_long_valid_lesson_cycle_keeps_full_data_but_reply_fits_max(self):
        titles = []
        for index in range(3):
            title = ('Задание-' + str(index)).ljust(100, 'я')
            titles.append(title)
            self.event(MARINA, '/task ' + title + ' | ' + 'И' * 1500)
            self.event(SASHA, '/submit ' + 'Р' * 1800)
            self.event(MARINA, '/review доработать | ' + 'К' * 1000)
        result = self.event(SASHA, '/lessons')
        self.assertLessEqual(len(result['reply']), 4000)
        self.assertIn(titles[-1], result['reply'])
        self.assertIn(titles[-2], result['reply'])
        self.assertNotIn(titles[0], result['reply'])
        rows = [row for row in self.state(SASHA)['assignments'] if row['title'] in titles]
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertEqual(row['instructions'], 'И' * 1500)
            self.assertEqual(row['review']['comment'], 'К' * 1000)
            self.assertEqual(row['submission']['text'], 'Р' * 1800)


if __name__ == '__main__':
    unittest.main()
