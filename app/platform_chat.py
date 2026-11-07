"""Private durable conversation state and replay receipts, independent of the API engine."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat

from fastapi import HTTPException

class ChatState:
    def __init__(self, path):
        self.path = Path(path).absolute()
        if any(p.is_symlink() for p in [self.path, *self.path.parents]):
            raise ValueError('Conversation state must be private and regular')
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.exists():
            info=self.path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise ValueError('Conversation state must be private and regular')
            with sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True) as probe:
                row=probe.execute('SELECT kind FROM chat_identity').fetchone()
                if row is None or row[0]!='ideyafix-platform-chat-v1':
                    raise ValueError('Not an Ideyafix conversation store')
        with self.connection() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS chat_identity (kind TEXT NOT NULL);
            INSERT INTO chat_identity SELECT 'ideyafix-platform-chat-v1' WHERE NOT EXISTS (SELECT 1 FROM chat_identity);
            CREATE TABLE IF NOT EXISTS chat_state(namespace TEXT NOT NULL,scope TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(namespace,scope));
            CREATE TABLE IF NOT EXISTS chat_receipts(namespace TEXT NOT NULL,scope TEXT NOT NULL,event_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL,reply TEXT NOT NULL,PRIMARY KEY(namespace,scope,event_id));
            ''')
        os.chmod(self.path,0o600)

    @contextmanager
    def connection(self):
        db=sqlite3.connect(self.path,timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def peek(self,namespace,session_key):
        with self.connection() as db:
            row=db.execute('SELECT payload FROM chat_state WHERE namespace=? AND scope=?',(namespace,session_key)).fetchone()
            return json.loads(row[0]) if row else {}

    def run(self,namespace,session_key,event_id,text,fn):
        if not all(isinstance(v,str) and v for v in [namespace,session_key,event_id,text]):
            raise ValueError('Explicit conversation scope and event required')
        fingerprint=hashlib.sha256(text.encode()).hexdigest()
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT fingerprint,reply FROM chat_receipts WHERE namespace=? AND scope=? AND event_id=?',
                (namespace,session_key,event_id)).fetchone()
            if old:
                if old[0]!=fingerprint:
                    raise HTTPException(409,'Событие диалога повторено с другим содержимым')
                return json.loads(old[1])
            row=db.execute('SELECT payload FROM chat_state WHERE namespace=? AND scope=?',(namespace,session_key)).fetchone()
            state=json.loads(row[0]) if row else {}
            reply=fn(state)
            if reply is None:
                return None
            if not isinstance(reply,dict) or not isinstance(reply.get('text'),str):
                raise ValueError('A conversation reply needs text')
            encoded=json.dumps(reply,ensure_ascii=False,allow_nan=False)
            db.execute('INSERT INTO chat_state VALUES(?,?,?) ON CONFLICT(namespace,scope) DO UPDATE SET payload=excluded.payload',
                (namespace,session_key,json.dumps(state,ensure_ascii=False,allow_nan=False)))
            db.execute('INSERT INTO chat_receipts VALUES(?,?,?,?,?)',(namespace,session_key,event_id,fingerprint,encoded))
            return json.loads(encoded)
