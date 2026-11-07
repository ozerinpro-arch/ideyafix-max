"""Atomic local demo state and immutable event receipts; never a live outbox."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import stat

from fastapi import HTTPException
from . import domain

class Store:
    def __init__(self, path):
        self.path = Path(path).absolute()
        if any(p.is_symlink() for p in [self.path, *self.path.parents]):
            raise ValueError('Demo database must be a regular owned file')
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.exists():
            info = self.path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
                raise ValueError('Demo database must be a regular owned file')
            try:
                probe = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)
                try:
                    row = probe.execute('SELECT payload FROM state WHERE id=1').fetchone()
                    if row is None or json.loads(row[0]).get('kind') != 'ideyafix-synthetic-v1':
                        raise ValueError('Not an Ideyafix demo database')
                finally:
                    probe.close()
            except (sqlite3.Error, json.JSONDecodeError):
                raise ValueError('Not an Ideyafix demo database') from None
        with self.connection() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS receipts (event_id TEXT PRIMARY KEY, actor_id INTEGER NOT NULL,
                text TEXT NOT NULL, reply TEXT NOT NULL, provider_id TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id INTEGER NOT NULL,
                direction TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL);
            ''')
            row = db.execute('SELECT payload FROM state WHERE id=1').fetchone()
            if row is None:
                db.execute('INSERT INTO state VALUES (1,?)', (json.dumps(domain.seed(), ensure_ascii=False),))
            elif json.loads(row[0]).get('kind') != 'ideyafix-synthetic-v1':
                raise ValueError('Not an Ideyafix demo database')
        os.chmod(self.path, 0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _view(self, db, actor_id):
        data = json.loads(db.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])
        state = domain.view(data, actor_id)
        rows = db.execute('SELECT direction,text,created_at FROM messages WHERE actor_id=? ORDER BY id DESC LIMIT 100', (actor_id,)).fetchall()
        state['messages'] = [dict(direction=r['direction'], text=r['text'], createdAt=r['created_at']) for r in reversed(rows)]
        return state

    def view(self, actor_id):
        with self.connection() as db:
            return self._view(db, actor_id)

    def event(self, actor_id, text, event_id, transport):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM receipts WHERE event_id=?', (event_id,)).fetchone()
            if old:
                if old['actor_id'] != actor_id or old['text'] != text:
                    raise HTTPException(409, 'ID события уже использован с другим содержимым')
                return dict(reply=old['reply'], duplicate=True, state=self._view(db, actor_id))
            data = json.loads(db.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])
            reply = domain.command(data, actor_id, text)
            # The transport type is fail-closed synthetic. Real I/O is impossible;
            # this transaction is not proposed as an outbox for production use.
            result = transport.deliver(actor_id, reply, event_id)
            if result.get('accepted') is not True:
                raise HTTPException(503, 'Имитация MAX не приняла ответ; событие не сохранено')
            at = domain.now()
            db.execute('UPDATE state SET payload=? WHERE id=1', (json.dumps(data, ensure_ascii=False),))
            db.execute('INSERT INTO receipts VALUES (?,?,?,?,?,?)', (event_id, actor_id, text, reply, result['providerMessageId'], at))
            db.executemany('INSERT INTO messages(actor_id,direction,text,created_at) VALUES (?,?,?,?)',
                [(actor_id, 'incoming', text, at), (actor_id, 'outgoing', reply, at)])
            return dict(reply=reply, duplicate=False, state=self._view(db, actor_id))
