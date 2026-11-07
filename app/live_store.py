"""Private native MAX intake/outbox for the five explicitly fictional demo roles.

No token, transport, configuration discovery or network operation belongs here.
Native event, domain update, transcript and outbox commit in one transaction;
workers claim an attempt before I/O and never retry an unknown attempt.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time

from fastapi import HTTPException

from . import domain
from .store import Store


_ROLE_IDS = {'лера': 710001, 'тимур': 710002, 'марина': 710003, 'саша': 710004, 'роман': 710005}
_ROLE_MENU = 'Тестовые роли: /role лера · /role тимур · /role марина · /role саша · /role роман.'
_UNBOUND = ('Это прототип «Идеяфикс» с вымышленными данными. Для подключения откройте '
            'одноразовую ссылку из локального стенда. Без неё команды не меняют демо.')
_TERMINAL = {'accepted', 'rejected', 'unknown'}


def _fail(message='Некорректное событие MAX'):
    raise HTTPException(422, message)


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'-?(0|[1-9][0-9]{0,18})', value):
        _fail()
    if not -(2 ** 63) <= int(value) < 2 ** 63:
        _fail()
    return value


def _text(value, maximum):
    return isinstance(value, str) and 0 < len(value) <= maximum and not any(
        ord(c) < 32 and c not in '\r\n\t' or 0x7F <= ord(c) <= 0x9F or 0xD800 <= ord(c) <= 0xDFFF for c in value)


def _now_ms():
    return int(time.time() * 1000)


def _validate(event):
    if not isinstance(event, dict) or event.get('isPrivate') is not True:
        _fail()
    fields = {'eventType', 'eventId', 'senderId', 'chatId', 'timestamp', 'nativeId', 'text',
              'startPayload', 'callbackPayload', 'payloadHash', 'isPrivate', 'messageTimestamp', 'callbackTimestamp'}
    if set(event) != fields:
        _fail()
    kind = event.get('eventType')
    if not isinstance(kind, str) or kind not in {'bot_started', 'bot_stopped', 'message_created'}:
        _fail()
    for name in ('eventId', 'payloadHash'):
        if not isinstance(event.get(name), str) or not re.fullmatch(r'[a-f0-9]{64}', event[name]):
            _fail()
    _identifier(event.get('senderId')); _identifier(event.get('chatId'))
    if type(event.get('timestamp')) is not int or not 0 <= event['timestamp'] < 2 ** 63:
        _fail()
    if kind == 'message_created' and (not _text(event.get('text'), 4000) or not _text(event.get('nativeId'), 200)):
        _fail()
    if kind in {'bot_started', 'bot_stopped'} and event.get('text') is not None:
        _fail()
    if event['callbackPayload'] is not None or event['callbackTimestamp'] is not None:
        _fail()
    if kind != 'bot_started' and event['startPayload'] is not None:
        _fail()
    if kind in {'bot_started', 'bot_stopped'} and (event['nativeId'] is not None or event['messageTimestamp'] is not None):
        _fail()
    if event['messageTimestamp'] is not None and (type(event['messageTimestamp']) is not int
            or not 0 <= event['messageTimestamp'] <= event['timestamp']):
        _fail()
    if kind == 'bot_started' and event.get('startPayload') is not None and not _text(event['startPayload'], 128):
        _fail()
    return event


class LiveStore(Store):
    def __init__(self, path):
        parent = Path(path).absolute().parent
        if parent.exists():
            info = parent.stat()
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError('Live MAX runtime directory must be private 0700')
        super().__init__(path)
        with self.connection() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS live_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS live_invites (
                hash TEXT PRIMARY KEY, actor_id INTEGER NOT NULL, expires_at INTEGER NOT NULL,
                used INTEGER NOT NULL CHECK(used IN (0,1)));
            CREATE TABLE IF NOT EXISTS live_sessions (
                sender_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL UNIQUE, actor_id INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS live_events (
                event_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS live_outbox (
                id TEXT PRIMARY KEY, event_id TEXT NOT NULL UNIQUE, chat_id TEXT NOT NULL,
                text TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('queued','started','accepted','rejected','unknown')),
                provider_id TEXT, error_code TEXT);
            CREATE TABLE IF NOT EXISTS live_poll_batch (
                id INTEGER PRIMARY KEY CHECK(id=1),prior_marker TEXT NOT NULL,
                next_marker TEXT NOT NULL,payload TEXT NOT NULL);
            ''')

    def issue_invite(self, actor_id):
        if type(actor_id) is not int or actor_id not in domain.BY_ID:
            _fail('Неизвестная тестовая роль')
        payload = secrets.token_urlsafe(32)
        expires = _now_ms() + 15 * 60 * 1000
        with self.connection() as db:
            db.execute('INSERT INTO live_invites VALUES (?,?,?,0)',
                       (hashlib.sha256(payload.encode('ascii')).hexdigest(), actor_id, expires))
        return dict(payload=payload, expiresAt=expires)

    def _invite(self, db, event):
        token = event.get('startPayload')
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
            return None
        digest = hashlib.sha256(token.encode('ascii')).hexdigest()
        row = db.execute('SELECT actor_id,expires_at,used FROM live_invites WHERE hash=?', (digest,)).fetchone()
        if row is None or row['used'] or row['expires_at'] <= _now_ms() or row['actor_id'] not in domain.BY_ID:
            return None
        # One native private dialog cannot become another sender's capability.
        owner = db.execute('SELECT sender_id FROM live_sessions WHERE chat_id=?', (event['chatId'],)).fetchone()
        if owner is not None and owner['sender_id'] != event['senderId']:
            return None
        db.execute('UPDATE live_invites SET used=1 WHERE hash=?', (digest,))
        db.execute('INSERT INTO live_sessions VALUES (?,?,?) ON CONFLICT(sender_id) DO UPDATE SET chat_id=excluded.chat_id,actor_id=excluded.actor_id',
                   (event['senderId'], event['chatId'], row['actor_id']))
        return row['actor_id']

    def ingest(self, normalized):
        event = _validate(normalized)
        identifier, payload_hash = event['eventId'], event['payloadHash']
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT payload_hash FROM live_events WHERE event_id=?', (identifier,)).fetchone()
            if old:
                if old['payload_hash'] != payload_hash:
                    raise HTTPException(409, 'Событие MAX повторено с другим содержимым')
                return dict(duplicate=True, queued=0)
            actor_id = None
            incoming = None
            reply = None
            if event['eventType'] == 'bot_stopped':
                owner = db.execute('SELECT sender_id FROM live_sessions WHERE chat_id=?', (event['chatId'],)).fetchone()
                db.execute('DELETE FROM live_sessions WHERE sender_id=? AND chat_id=?', (event['senderId'], event['chatId']))
                if owner is None or owner['sender_id'] == event['senderId']:
                    db.execute("UPDATE live_outbox SET state='rejected',error_code='dialog_stopped' WHERE chat_id=? AND state='queued'", (event['chatId'],))
                    db.execute("UPDATE live_events SET state='rejected' WHERE event_id IN (SELECT event_id FROM live_outbox WHERE chat_id=? AND error_code='dialog_stopped')", (event['chatId'],))
            elif event['eventType'] == 'bot_started':
                actor_id = self._invite(db, event)
                if actor_id is None and event['startPayload'] is None:
                    current = db.execute('SELECT actor_id FROM live_sessions WHERE sender_id=? AND chat_id=?',
                                         (event['senderId'], event['chatId'])).fetchone()
                    if current is not None:
                        actor_id = current['actor_id']
                incoming = '/start'
                reply = (_UNBOUND if actor_id is None else
                         f"Вы подключены как {domain.BY_ID[actor_id]['name']}. Это вымышленная роль, не настоящий аккаунт участника.\n\n{_ROLE_MENU}\n/start — меню.")
            else:
                session = db.execute('SELECT actor_id FROM live_sessions WHERE sender_id=? AND chat_id=?',
                                     (event['senderId'], event['chatId'])).fetchone()
                if session is None:
                    reply = _UNBOUND
                else:
                    actor_id = session['actor_id']
                    incoming = event['text']
                    verb, _, argument = incoming.strip().partition(' ')
                    if verb.casefold() == '/role':
                        alias = argument.strip().casefold()
                        selected = _ROLE_IDS.get(alias)
                        if selected is None and alias.isascii() and alias.isdigit():
                            selected = int(alias) if int(alias) in domain.BY_ID else None
                        if not alias:
                            reply = f"Текущая тестовая роль: {domain.BY_ID[actor_id]['name']}.\n{_ROLE_MENU}"
                        elif selected is None:
                            reply = 'Такой тестовой роли нет.\n' + _ROLE_MENU
                        else:
                            actor_id = selected
                            db.execute('UPDATE live_sessions SET actor_id=? WHERE sender_id=? AND chat_id=?',
                                       (actor_id, event['senderId'], event['chatId']))
                            reply = f"Теперь вы {domain.BY_ID[actor_id]['name']}. Все роли и предметные данные вымышлены.\n/start — меню.\n{_ROLE_MENU}"
                    elif verb.casefold() in {'/start', '/help'}:
                        reply = (f"{domain.BY_ID[actor_id]['name']}, это тестовый бот «Идеяфикс». "
                                 'Диалог идёт через настоящий MAX; все роли и учебные данные тестовые.\n'
                                 '/hackathon — хакатон.\n/education — учебное демо.\n' + _ROLE_MENU)
                    elif len(incoming) > 2000:
                        reply = 'Для этой демонстрации отправьте команду до 2000 символов.\n' + _ROLE_MENU
                    else:
                        data = json.loads(db.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])
                        reply = domain.command(data, actor_id, incoming)
                        db.execute('UPDATE state SET payload=? WHERE id=1', (json.dumps(data, ensure_ascii=False),))
                        reply += '\n\n' + _ROLE_MENU
            queued = 0
            if reply is not None:
                reply = reply[:4000]
                outbox_id = hashlib.sha256(('ideyafix-live-outbox:' + identifier).encode('ascii')).hexdigest()
                db.execute("INSERT INTO live_outbox VALUES (?,?,?,?,'queued',NULL,NULL)",
                           (outbox_id, identifier, event['chatId'], reply,))
                queued = 1
                if actor_id is not None:
                    at = domain.now()
                    db.executemany('INSERT INTO messages(actor_id,direction,text,created_at) VALUES (?,?,?,?)',
                                   [(actor_id, 'incoming', incoming, at), (actor_id, 'outgoing', reply, at)])
            db.execute('INSERT INTO live_events VALUES (?,?,?)', (identifier, payload_hash, 'queued' if queued else 'processed'))
            return dict(duplicate=False, queued=queued)

    def get_cursor(self):
        with self.connection() as db:
            row = db.execute("SELECT value FROM live_meta WHERE key='poll_cursor'").fetchone()
            return json.loads(row['value']) if row else None

    def pending_batch(self):
        with self.connection()as db:
            row=db.execute('SELECT prior_marker,next_marker,payload FROM live_poll_batch WHERE id=1').fetchone()
            return {'priorMarker':json.loads(row['prior_marker']),'marker':json.loads(row['next_marker']),
                    'updates':json.loads(row['payload'])}if row else None

    def stage_batch(self,prior_marker,next_marker,updates):
        if not isinstance(updates,list)or len(updates)>100:raise ValueError('invalid_max_batch')
        for marker in (prior_marker,next_marker):
            if marker is not None and(type(marker)is not int or not 0<=marker<2**63):raise ValueError('invalid_max_marker')
        encoded=json.dumps(updates,ensure_ascii=False,allow_nan=False,separators=(',',':'))
        if len(encoded.encode())>4*1024*1024:raise ValueError('max_batch_too_large')
        with self.connection()as db:
            db.execute('BEGIN IMMEDIATE')
            cursor=db.execute("SELECT value FROM live_meta WHERE key='poll_cursor'").fetchone()
            current=json.loads(cursor['value'])if cursor else None
            if current!=prior_marker:raise HTTPException(409,'Курсор MAX изменился до сохранения batch')
            old=db.execute('SELECT prior_marker,next_marker,payload FROM live_poll_batch WHERE id=1').fetchone()
            values=(json.dumps(prior_marker),json.dumps(next_marker),encoded)
            if old:
                if tuple(old)!=values:raise HTTPException(409,'Другой batch MAX уже ожидает обработки')
                return
            db.execute('INSERT INTO live_poll_batch VALUES(1,?,?,?)',values)

    def complete_batch(self):
        with self.connection()as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT prior_marker,next_marker FROM live_poll_batch WHERE id=1').fetchone()
            if row is None:raise ValueError('max_batch_not_staged')
            cursor=db.execute("SELECT value FROM live_meta WHERE key='poll_cursor'").fetchone()
            current=json.loads(cursor['value'])if cursor else None
            if current!=json.loads(row['prior_marker']):raise HTTPException(409,'Курсор MAX изменился во время обработки batch')
            next_marker=json.loads(row['next_marker'])
            if next_marker is not None:
                if current is not None and next_marker<current:raise HTTPException(409,'Курсор MAX не может уменьшаться')
                db.execute("INSERT INTO live_meta VALUES('poll_cursor',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(json.dumps(next_marker),))
            db.execute('DELETE FROM live_poll_batch WHERE id=1')

    def save_cursor(self, marker):
        if marker is not None and (type(marker) is not int or not 0 <= marker < 2 ** 63):
            _fail('Некорректная позиция MAX')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT value FROM live_meta WHERE key='poll_cursor'").fetchone()
            current = json.loads(row['value']) if row else None
            if current is not None and (marker is None or marker < current):
                raise HTTPException(409, 'Позиция MAX не может уменьшаться')
            db.execute("INSERT INTO live_meta VALUES ('poll_cursor',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (json.dumps(marker),))

    def heartbeat(self):
        with self.connection() as db:
            db.execute("INSERT INTO live_meta VALUES ('heartbeat',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(time.time()),))

    def worker_alive(self):
        with self.connection() as db:
            row = db.execute("SELECT value FROM live_meta WHERE key='heartbeat'").fetchone()
            try:
                return row is not None and 0 <= time.time() - float(row['value']) < 40
            except (TypeError, ValueError):
                return False

    def claim_next(self):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT id,chat_id,text,event_id FROM live_outbox WHERE state='queued' ORDER BY rowid LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE live_outbox SET state='started' WHERE id=? AND state='queued'", (row['id'],))
            db.execute("UPDATE live_events SET state='started' WHERE event_id=?", (row['event_id'],))
            return dict(id=row['id'], chat_id=row['chat_id'], text=row['text'])

    def finish(self, identifier, outcome):
        if not isinstance(outcome, dict) or not isinstance(outcome.get('status'), str) or outcome['status'] not in {'accepted', 'rejected', 'unknown'}:
            _fail('Некорректная квитанция MAX')
        provider_id = outcome.get('provider_message_id')
        if provider_id is not None and not _text(provider_id, 256):
            _fail('Некорректная квитанция MAX')
        state = outcome['status']
        if state == 'accepted' and provider_id is None:
            state = 'unknown'
        code = outcome.get('code')
        code = code if isinstance(code, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,80}', code) else 'unclassified'
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT event_id,state FROM live_outbox WHERE id=?', (identifier,)).fetchone()
            if row is None:
                raise HTTPException(404, 'Попытка MAX не найдена')
            if row['state'] in _TERMINAL:
                return dict(state=row['state'])
            if row['state'] != 'started':
                raise HTTPException(409, 'Попытка MAX ещё не начата')
            db.execute('UPDATE live_outbox SET state=?,provider_id=?,error_code=? WHERE id=?',
                       (state, provider_id, code, identifier))
            db.execute('UPDATE live_events SET state=? WHERE event_id=?', (state, row['event_id']))
            return dict(state=state)

    def recover(self):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute("SELECT event_id FROM live_outbox WHERE state='started'").fetchall()
            db.execute("UPDATE live_outbox SET state='unknown',error_code='worker_restart_unknown' WHERE state='started'")
            for row in rows:
                db.execute("UPDATE live_events SET state='unknown' WHERE event_id=?", (row['event_id'],))
            return dict(unknown=len(rows))

    def safe_status(self):
        with self.connection() as db:
            result = {name: 0 for name in ('queued', 'started', 'accepted', 'rejected', 'unknown')}
            for row in db.execute('SELECT state,COUNT(*) AS count FROM live_outbox GROUP BY state'):
                result[row['state']] = row['count']
            result['bindings'] = db.execute('SELECT COUNT(*) FROM live_sessions').fetchone()[0]
            result['events'] = db.execute('SELECT COUNT(*) FROM live_events').fetchone()[0]
            return result
