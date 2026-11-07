"""Explicit local MAX receiver: outgoing HTTPS only, no public listener.

Cursor advances only after durable ingestion. One owned OS lock serializes each
poll batch across processes; no active webhook is deleted or silently replaced.
"""
import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
import threading

from fastapi import HTTPException

from .max_transport import MaxClient


IDENTITY_TYPES = ('bot_started', 'bot_stopped')


class MaxPoller:
    def __init__(self, resources, *, client=None):
        self.resources = resources
        self._client = client
        self._owns_client = False
        self._subscriptions_checked = False
        if client is not None:
            import httpx
            if (resources.settings.max_token != 'SYNTHETIC-TOKEN' or type(client) is not MaxClient
                    or type(client._client._transport) is not httpx.MockTransport):
                raise ValueError('Only the explicit synthetic MAX test client can be injected')

    def close(self):
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    @contextmanager
    def _exclusive_poll(self):
        path = Path(self.resources.settings.max_db).with_suffix('.poll.lock')
        if path.parent.resolve(strict=True) != path.parent:
            raise ValueError('Canonical private polling directory required')
        parent = path.parent.stat()
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid()
                or stat.S_IMODE(parent.st_mode) != 0o700):
            raise ValueError('Owned private polling directory required')
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        held = False
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                    or (info.st_dev, info.st_ino) != (path.stat().st_dev, path.stat().st_ino)):
                raise ValueError('Owned private polling lock required')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
            except BlockingIOError:
                yield False
            else:
                yield True
        finally:
            if held:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def poll_once(self):
        resources = self.resources
        if resources.max_runtime is None:
            return {'state': 'disabled', 'accepted': 0}
        if resources.settings.max_update_mode != 'long_polling':
            return {'state': 'polling_not_selected', 'accepted': 0}
        with resources.bind():
            with self._exclusive_poll() as selected:
                if not selected:
                    return {'state': 'poller_busy', 'accepted': 0}
                if self._client is None:
                    self._client = MaxClient(resources.settings.max_token,
                        additional_ca_file=resources.settings.max_ca_file or None)
                    self._owns_client = True
                if not self._subscriptions_checked:
                    subscriptions = self._client.list_subscriptions()
                    if subscriptions.status != 'accepted':
                        return {'state': 'subscriptions_unverified', 'accepted': 0}
                    if subscriptions.data['subscriptions']:
                        return {'state': 'webhook_subscription_active', 'accepted': 0}
                    self._subscriptions_checked = True
                runtime = resources.max_runtime
                before = runtime.store.read().get('polling', {}).get('marker')
                response = self._client.get_updates(marker=before, limit=20, timeout=5,
                                                    update_types=IDENTITY_TYPES)
                if response.status != 'accepted':
                    # Never change the cursor after a transport or response failure.
                    return {'state': 'poll_unconfirmed', 'accepted': 0, 'code': response.code}
                data = response.data
                after = data['marker']
                if before is not None and (after is None or after < before):
                    return {'state': 'cursor_regression_refused', 'accepted': 0}
                accepted = rejected = skipped = 0
                for update in data['updates']:
                    if update.get('update_type') not in IDENTITY_TYPES:
                        skipped += 1
                        continue
                    try:
                        raw = json.dumps(update, ensure_ascii=False, allow_nan=False).encode('utf-8')
                    except (UnicodeError, ValueError, TypeError):
                        rejected += 1
                        continue
                    try:
                        runtime.ingest(raw,
                            [('X-Max-Bot-Api-Secret', resources.settings.max_secret)])
                        accepted += 1
                    except HTTPException as error:
                        if error.status_code not in {400, 403, 409, 413, 415, 422}:
                            # Earlier durable events will be deduplicated on replay.
                            return {'state': 'ingest_retry_required', 'accepted': accepted}
                        rejected += 1
                # MAX acknowledges previous events on the NEXT marker request.
                # Persist this marker only after the whole batch is durable or
                # explicitly rejected. A crash before here repeats native IDs.
                with runtime.store.edit() as state:
                    current = state.get('polling', {}).get('marker')
                    if current != before:
                        raise HTTPException(409, 'Позиция приёма MAX изменилась')
                    state['polling'] = {'marker': after}
                return {'state': 'polled', 'accepted': accepted, 'rejected': rejected,
                        'skipped': skipped, 'cursorSaved': True}


def configured_resources():
    from .integration_settings import IntegrationSettings
    from .school_integrations import IntegrationResources
    from .school_postgres import SchoolPostgres, SchoolPostgresSettings
    school = os.environ.get('SHKOLA_SCHOOL_ID', '')
    selected = SchoolPostgresSettings.from_environment(school)
    if selected is not None:
        from .school_max import resources_for_school
        return resources_for_school(SchoolPostgres(selected))
    return IntegrationResources(IntegrationSettings.from_environment(school))


def main(argv=None):
    parser = argparse.ArgumentParser(description='Локальный MAX Long Polling без публичного сервера')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args(argv)
    os.environ['PYTHON_DOTENV_DISABLED'] = '1'
    resources = configured_resources()
    poller = MaxPoller(resources)
    from .max_app import pump_once
    stop = threading.Event()
    try:
        if resources.max_runtime is None or resources.settings.max_update_mode != 'long_polling':
            print(json.dumps({'state': 'disabled_or_not_configured'}))
            return 2
        with resources.bind():
            resources.max_runtime.recover()
        while not stop.is_set():
            try:
                result = poller.poll_once()
                # Process a bounded number of already durable events/commands.
                if result['state'] == 'polled':
                    for _ in range(20):
                        if not pump_once(resources)['processed']:
                            break
            except Exception:
                result = {'state': 'temporarily_unavailable'}
            if args.once:
                print(json.dumps(result))
                return 0 if result['state'] == 'polled' else 2
            stop.wait(1 if result['state'] == 'polled' else 5)
    except KeyboardInterrupt:
        return 0
    finally:
        poller.close()
        resources.close()


if __name__ == '__main__':
    raise SystemExit(main())
