"""Explicit single-school MAX resources, separate from the rehearsal constructor.

No subscription, sending, schema provisioning or credential discovery on import.
All runtime operations share the native maintenance fence, including transport I/O.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from fastapi import HTTPException

from .integration_settings import IntegrationSettings
from .school_integrations import IntegrationResources, _current, _staff_authority
from . import request_extraction, school_requests, staff_access


UNAVAILABLE = 'Рабочее подключение MAX временно недоступно.'


def settings_for_school(school):
    # Working school MAX does not enable or even discover an AI credential.
    names = {'SHKOLA_STAFF_DB_PATH', 'SHKOLA_DB_PATH', 'AGENT_DB_PATH'}
    env = {k: v for k, v in os.environ.items() if k.startswith('SHKOLA_MAX_') or k in names}
    return IntegrationSettings.from_environment(school, env)


class SchoolMaxResources(IntegrationResources):
    working_max = True

    def __init__(self, settings, storage, *, staff_expected=None, transport=None):
        from .school_postgres import SchoolPostgres
        from .school_runtime_gate import RuntimeGate
        if type(settings) is not IntegrationSettings or type(storage) is not SchoolPostgres:
            raise ValueError('Expected explicit school settings and PostgreSQL storage')
        if settings.school_id != storage.settings.school:
            raise ValueError('MAX school differs from the selected PostgreSQL domain')
        self.settings, self.replacement_storage = settings, storage
        self.domain_binding = (os.environ.get('SHKOLA_DB_PATH', ''), os.environ.get('AGENT_DB_PATH', ''))
        self.extraction = self.max_runtime = self.identity = self.transport = None
        self.authority_ready = self.manager_ai_enabled = False
        self.initialization_blockers = []
        self._gate = self._expected_staff = self._staff_config = self._origin = None
        if not settings.max_requested:
            if transport is not None:
                raise ValueError('Disabled MAX cannot accept a transport')
            # OFF keeps no capability, credential, path or AI configuration.
            self.settings = IntegrationSettings(settings.school_id)
            return
        if (settings.request_ai_requested or settings.ai_key or settings.ai_db or settings.errors
                or not settings.max_config_ready):
            raise ValueError('Incomplete explicit school MAX configuration')
        path, school = staff_access.configuration()
        if (school != settings.school_id or Path(settings.staff_db) != path
                or Path(settings.max_db) != path.parent / 'max.sqlite3'
                or path.resolve(strict=True) != path):
            raise ValueError('Expected the selected school private MAX sidecar')
        expected = staff_expected if staff_expected is not None else os.environ.get('SHKOLA_MAX_STAFF_IDENTITY', '')
        if not isinstance(expected, str) or not re.fullmatch(r'[a-f0-9]{64}', expected):
            raise ValueError('Explicit MAX staff identity required')
        self._staff_config, self._expected_staff = (path, school), expected
        self._origin = os.environ.get('SHKOLA_STAFF_ORIGIN', '')
        self._gate = RuntimeGate.for_staff(path)
        with self._gate.shared():
            self.validate_profile()
            if not _staff_authority(settings):
                raise ValueError('Expected an existing school staff authority')
            from .max_transport import MaxClient
            if transport is not None:
                import httpx
                if type(transport) is not httpx.MockTransport or settings.max_token != 'SYNTHETIC-TOKEN':
                    raise ValueError('Explicit synthetic MAX test transport required')
            sender = None
            if settings.max_send_requested:
                sender = MaxClient(settings.max_token, transport=transport,
                                   additional_ca_file=settings.max_ca_file or None)
            try:
                from .school_max_store import SchoolMaxStore
                from .max_runtime import MaxRuntime
                store = SchoolMaxStore(db_path=settings.max_db, school_id=school,
                    integration_id=settings.max_integration_id, enabled=True,
                    domain=storage.settings.expected_domain, staff_identity=expected,
                    validate_profile=self.validate_profile)
                runtime = MaxRuntime(integration_id=settings.max_integration_id, school_id=school,
                    db_path=settings.max_db, webhook_secret=settings.max_secret, transport=sender,
                    enabled=True, schedule_notices_enabled=settings.max_schedule_notices_requested,
                    replacement_storage=storage, replacement_domain=storage.settings.expected_domain,
                    replacement_staff=expected, max_store=store)
                with school_requests.bound_school(school):
                    # Validate an existing sidecar; absent state remains absent.
                    store.read()
                self.transport, self.max_runtime, self.identity = sender, runtime, runtime.identity
                self.authority_ready = True
            except BaseException:
                if sender is not None:
                    sender.close()
                raise

    def validate_profile(self):
        from .school_notice_jobs import staff_identity
        try:
            if (self._staff_config != staff_access.configuration()
                    or self.domain_binding != (os.environ.get('SHKOLA_DB_PATH', ''), os.environ.get('AGENT_DB_PATH', ''))
                    or os.environ.get('SHKOLA_STAFF_ORIGIN', '') != self._origin):
                raise ValueError('MAX profile configuration changed')
            path, school = self._staff_config
            if staff_identity(school, path, self._origin) != self._expected_staff:
                raise ValueError('MAX staff authority changed')
            origin = urlsplit(self._origin)
            self.replacement_storage.validate_staff(school, origin.scheme, origin.hostname, path)
        except (OSError, RuntimeError, ValueError, TypeError):
            raise HTTPException(503, UNAVAILABLE) from None

    @contextmanager
    def bind(self):
        if self.max_runtime is None:
            token = _current.set(self)
            try:
                with school_requests.bound_school(self.settings.school_id), request_extraction.bound_service(None):
                    yield self
            finally:
                _current.reset(token)
            return
        with self._gate.shared():
            self.validate_profile()
            token = _current.set(self)
            try:
                with school_requests.bound_school(self.settings.school_id), request_extraction.bound_service(None):
                    yield self
            finally:
                _current.reset(token)


def resources_for_school(storage):
    return SchoolMaxResources(settings_for_school(storage.settings.school), storage)
