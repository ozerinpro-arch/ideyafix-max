"""Outgoing-only MAX receiver; durable intake before cursor acknowledgement."""
import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import signal
import stat
import time
from fastapi import HTTPException

from .live_store import LiveStore
from .live_transport import LiveMaxClient
from .max_settings import MaxSettings
from .school_demo_links import SCHOOL_STAFF_URL
from .school_notification_client import (
    DELIVERY_STATES, SchoolNotificationClient, SchoolNotificationsUnavailable, school_link_token,
)
from .vendor.max_wire import normalize_update, MaxWireError

SCHOOL_POLL_INTERVAL = 5
SCHOOL_RETRY_INTERVAL = 10

@contextmanager
def exclusive_worker(database):
    path = Path(database).with_name('max-polling.lock')
    if not path.is_absolute() or any(p.is_symlink() for p in [path, *path.parents]):
        raise ValueError('invalid_worker_lock')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
            raise ValueError('invalid_worker_lock')
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('max_worker_already_running') from None
        yield
    finally:
        os.close(fd)

class Poller:
    def __init__(self, settings, *, client=None, platform_assistant=None, school_client=None):
        if not settings.enabled:
            raise ValueError('real_max_disabled')
        self.settings = settings
        self.school_token_file = settings.school_notifications_token_file if settings.platform_enabled else None
        self.school_client = school_client if self.school_token_file is not None else None
        self.next_school_claim = self.school_retry_after = 0
        self.assistant=None
        if settings.platform_enabled:
            from .platform_assistant import PlatformAssistant
            from .platform_store import PlatformStore
            from .platform_transport import PlatformMaxClient
            from .school_demo_links import load_web_demo_accounts
            demo_file = settings.web_demo_accounts_file
            demo_accounts = load_web_demo_accounts(demo_file) if demo_file else None
            self.assistant=platform_assistant if platform_assistant is not None else PlatformAssistant(nanogpt_config=settings.nanogpt_config)
            self.store=PlatformStore(settings.db_path,self.assistant,bot_username=settings.bot_username,web_demo_accounts=demo_accounts)
            self.client=client if client is not None else PlatformMaxClient(settings.token_file,settings.ca_file)
        else:
            self.store = LiveStore(settings.db_path)
            self.client = client if client is not None else LiveMaxClient(settings.token_file, settings.ca_file)
        self.ready = False
        self.last_send = {}

    def initialize(self):
        identity = self.client.get_me()
        if identity.status != 'accepted' or identity.data.get('username') != self.settings.bot_username or identity.data.get('isBot') is not True:
            raise ValueError('max_bot_identity_not_verified')
        subscriptions = self.client.list_subscriptions()
        if subscriptions.status != 'accepted':
            raise ValueError('max_subscriptions_not_verified')
        if subscriptions.data.get('subscriptions'):
            raise ValueError('active_webhook_refused')
        self.store.recover()
        self.store.heartbeat()
        self.ready = True
        return dict(botVerified=True, subscriptions=0, mode='long_polling')

    def poll_once(self):
        if not self.ready:
            self.initialize()
        self.store.heartbeat()
        prior_send = self.send_one()
        batch=self.store.pending_batch()
        if batch is None:
            marker = self.store.get_cursor()
            outcome = self.client.get_updates(marker=marker, limit=20, timeout=5)
            if outcome.status != 'accepted':
                return dict(state='waiting', code=outcome.code, accepted=0, skipped=0, sendState=prior_send)
            next_marker = outcome.data['marker']
            if marker is not None and next_marker is not None and next_marker < marker:
                raise ValueError('max_cursor_moved_backwards')
            # With marker=None MAX may return only the latest update. Persist
            # the received body before intake so a later latest update cannot
            # replace an uncommitted first batch after a storage failure.
            self.store.stage_batch(marker,next_marker,outcome.data['updates'])
            batch=self.store.pending_batch()
        accepted, skipped = 0, 0
        for raw in batch['updates']:
            try:
                if self.settings.platform_enabled:
                    from .platform_wire import normalize_platform_update
                    native=normalize_platform_update(raw)
                else:
                    native = normalize_update(json.dumps(raw, ensure_ascii=True, allow_nan=False).encode()).as_dict()
            except (MaxWireError, UnicodeError, TypeError, ValueError):
                skipped += 1
                continue
            # Ignore retained events from before the explicit local activation.
            if native['timestamp'] < self.settings.activated_at_ms:
                skipped += 1
                continue
            school_link = school_link_token(native) if self.school_token_file is not None else None
            if native.get('file')is not None:
                try:native['file']=self.client.download_document(native['file'])
                except Exception:native['file']={'error':'download_unavailable'}
            try:
                if school_link is None:
                    self.store.ingest(native)
                else:
                    self.store.ingest(native, school_link=school_link)
            except HTTPException as error:
                # A native shape can pass the wire parser yet fail the stricter
                # domain-input contract. Its atomic ingest rolled back; malformed
                # input must not hold all later provider events behind it.
                if error.status_code == 422:
                    skipped += 1
                    continue
                raise
            accepted += 1
            if native['eventType']=='message_callback'and hasattr(self.client,'answer_callback'):
                try:self.client.answer_callback(native['nativeId'])
                except Exception:pass  # Callback UI acknowledgement is best effort.
        # Storage failures propagate above; a failed batch never acknowledges its tail.
        self.store.complete_batch()
        self.store.heartbeat()
        sent = self.send_one()
        self._poll_school()
        return dict(state='polled', accepted=accepted, skipped=skipped, sendState=sent)

    def _report_school_receipt(self, claim_id, status):
        # Both true (stored) and false (terminal refusal) settle the local receipt.
        self.school_client.finish(claim_id, status)
        self.store.school_receipt_reported(claim_id, status)

    def _poll_school(self):
        now = time.monotonic()
        if self.school_token_file is None or now < self.school_retry_after:
            return
        link = None
        try:
            receipt = self.store.pending_school_receipt()
            if receipt is None:
                link = self.store.next_school_link()
                if link is None and now < self.next_school_claim:
                    return
            if self.school_client is None:
                try:
                    self.school_client = SchoolNotificationClient(self.school_token_file)
                except ValueError:
                    raise SchoolNotificationsUnavailable() from None
            if receipt is not None:
                self._report_school_receipt(*receipt)
                return
            if link is not None:
                linked = self.school_client.link(link['token'], link['chat_id'], link['sender_id'])
                self.store.school_link_result(link['event_id'], linked)
                return
            self.next_school_claim = now + SCHOOL_POLL_INTERVAL
            notice = self.school_client.claim()
            if notice is None or not self.store.start_school_delivery(notice['claimId']):
                return
            status = self._send_school_notice(notice)
            self.store.school_delivery_result(notice['claimId'], status)
            self._report_school_receipt(notice['claimId'], status)
        except SchoolNotificationsUnavailable:
            self.school_retry_after = time.monotonic() + SCHOOL_RETRY_INTERVAL
            if link is not None:
                self.store.school_link_result(link['event_id'], None)

    def _send_school_notice(self, notice):
        try:
            outcome = self._send_to_chat(notice['chatId'], self.client.send_reply, notice['text'],
                                         buttons=[{'label': 'Открыть школу', 'url': SCHOOL_STAFF_URL}])
            if outcome.status in DELIVERY_STATES and (outcome.status != 'accepted' or outcome.provider_message_id):
                return outcome.status
        except Exception:
            pass
        return 'unknown'

    def _send_to_chat(self, chat, send, *args, **kwargs):
        wait = .55 - (time.monotonic() - self.last_send.get(chat, 0))
        if wait > 0:
            time.sleep(wait)
        try:
            return send(int(chat), *args, **kwargs)
        finally:
            self.last_send[chat] = time.monotonic()

    def send_one(self):
        item = self.store.claim_next()
        if item is None:
            return 'idle'
        chat = item['chat_id']
        try:
            if item.get('reply')is not None:
                reply=item['reply']
                options={'buttons':reply.get('buttons',()),'document':reply.get('document')}
                if reply.get('image') is not None:
                    options.update(image=reply['image'],image_guard=lambda:self.store.image_is_current(item))
                outcome=self._send_to_chat(chat,self.client.send_reply,reply['text'],**options)
            else:
                outcome = self._send_to_chat(chat,self.client.send_message,item['text'])
        except Exception:
            self.store.finish(item['id'], dict(status='unknown', code='transport_failure', provider_message_id=None))
            return 'unknown'
        value = dict(status=outcome.status, code=outcome.code, provider_message_id=outcome.provider_message_id)
        return self.store.finish(item['id'], value)['state']

    def close(self):
        if self.school_client is not None:
            self.school_client.close()
        self.client.close()
        if self.assistant is not None:self.assistant.close()

def main():
    parser = argparse.ArgumentParser(description='Идеяфикс · MAX Long Polling без сайта')
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    settings = MaxSettings.load(args.config)
    if not settings.enabled:
        print(json.dumps({'state': 'disabled'}))
        return 0
    stop = False
    def requested_stop(*_):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGTERM, requested_stop)
    signal.signal(signal.SIGINT, requested_stop)
    worker = None
    try:
        # Exactly one local consumer across provider wait, durable intake and send.
        with exclusive_worker(settings.db_path):
            worker = Poller(settings)
            initialized = worker.initialize()
            if args.check:
                print(json.dumps(initialized))
                return 0
            if args.once:
                print(json.dumps(worker.poll_once()))
                return 0
            print(json.dumps({'state': 'running', **initialized}), flush=True)
            while not stop:
                try:
                    worker.poll_once()
                except Exception:
                    # No raw provider body, credentials, IDs or exception text in logs.
                    print(json.dumps({'state': 'retrying', 'code': 'poll_cycle_failed'}), flush=True)
                if not stop:
                    time.sleep(.5)
        return 0
    except Exception:
        print(json.dumps({'state': 'stopped', 'code': 'max_initialization_failed'}))
        return 1
    finally:
        if worker is not None:
            worker.close()

if __name__ == '__main__':
    raise SystemExit(main())
