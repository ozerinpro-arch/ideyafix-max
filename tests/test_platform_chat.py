from pathlib import Path
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from fastapi import HTTPException
from app.platform_chat import ChatState

class DurableChatTests(unittest.TestCase):
    def setUp(self):
        self.root=tempfile.TemporaryDirectory(dir='/private/tmp',prefix='ifx-chat-test.')
        self.addCleanup(self.root.cleanup)
        self.path=Path(self.root.name)/'conversation.sqlite3'
        self.store=ChatState(self.path)

    def test_state_reply_and_scope_survive_recreation(self):
        calls=[]
        def mutate(state):
            calls.append(True);state['step']=1
            return {'text':'Выберите дату','buttons':[{'label':'Сегодня','command':'/calendar today'}]}
        response=self.store.run('calendar','one:teacher','event-1','Календарь',mutate)
        again=ChatState(self.path)
        self.assertEqual(again.peek('calendar','one:teacher'),{'step':1})
        self.assertEqual(again.run('calendar','one:teacher','event-1','Календарь',mutate),response)
        self.assertEqual(len(calls),1)
        self.assertEqual(again.peek('calendar','two:teacher'),{})
        self.assertEqual(again.peek('calendar','one:student'),{})

    def test_conflicting_replay_and_failure_never_advance_state(self):
        self.store.run('learning','one','event','Первый',lambda state:({'text':'Принято'}))
        with self.assertRaises(HTTPException):
            self.store.run('learning','one','event','Другой',lambda state:({'text':'Нельзя'}))
        def fail(state):
            state['shouldNotPersist']=True
            raise RuntimeError('synthetic interrupted action')
        with self.assertRaises(RuntimeError):self.store.run('learning','one','next','Ответ',fail)
        self.assertEqual(self.store.peek('learning','one'),{})
        self.assertEqual(self.store.run('learning','one','next','Ответ',lambda state:{'text':'Повтор допустим'})['text'],'Повтор допустим')

    def test_unowned_input_is_not_consumed(self):
        def unowned(state):state['ignored']=True
        self.assertIsNone(self.store.run('schedule','one','e','Учебное',unowned))
        self.assertEqual(self.store.peek('schedule','one'),{})
        self.assertEqual(self.store.run('learning','one','e','Учебное',lambda state:{'text':'Задания'})['text'],'Задания')

    def test_concurrent_replay_executes_one_transition(self):
        calls=[]
        def transition(state):
            calls.append(1);state['count']=state.get('count',0)+1;return {'text':'Один ответ'}
        with ThreadPoolExecutor(max_workers=5) as pool:
            responses=list(pool.map(lambda _:self.store.run('calendar','one','e','Да',transition),range(5)))
        self.assertEqual(len(calls),1)
        self.assertTrue(all(r==responses[0] for r in responses))
        self.assertEqual(self.store.peek('calendar','one'),{'count':1})

    def test_unrelated_database_is_not_modified(self):
        import sqlite3
        foreign=Path(self.root.name)/'foreign.sqlite3'
        with sqlite3.connect(foreign) as db:db.execute('CREATE TABLE unrelated(value)')
        original=foreign.read_bytes()
        with self.assertRaises((ValueError,sqlite3.Error)):ChatState(foreign)
        self.assertEqual(foreign.read_bytes(),original)

if __name__=='__main__':unittest.main()
