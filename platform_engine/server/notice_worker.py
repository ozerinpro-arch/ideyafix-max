"""Explicit PostgreSQL provisioning, dispatch and Celery worker commands."""
import argparse
from contextlib import nullcontext
import os
from pathlib import Path
import stat
import time
from urllib.parse import urlsplit

from fastapi import HTTPException

from . import staff_access
from .club_documents import ClubDocuments, preview_enabled
from .launch_identity import install
from .notice_jobs import DOCUMENT_TASK, TASK, DocumentJobs, NoticeJobs, NoticeJobSettings, authority, create_broker, require_current, verify_job_schema
from .schedule_notifications import service_for
from .school_notice_jobs import SchoolNoticeSettings
from .school_requests import bound_school
from .student_learning import StudentLearningService, bound_service
from .student_schedule import StudentScheduleService


def worker_for(settings, *, replacement_storage=None):
    staff_config = staff_access.configuration()
    staff_path, school = staff_config
    if os.environ.get('SHKOLA_WORKSPACE_MODE') != 'school' or school != settings.school:
        raise RuntimeError('Нужен школьный контур обработчика уведомлений')
    working_profile = isinstance(settings, SchoolNoticeSettings)
    from .school_runtime_gate import RuntimeGate
    gate = RuntimeGate.for_staff(staff_path) if working_profile else None
    replacement_domain = getattr(settings, 'replacement_domain', None)
    replacement_base_scope = getattr(settings, 'replacement_base_scope', None)
    if working_profile:
        from .school_postgres import SchoolPostgres

        if (type(settings) is not SchoolNoticeSettings or type(settings.storage) is not SchoolPostgres
                or replacement_storage is not None and (
                    type(replacement_storage) is not SchoolPostgres or replacement_storage != settings.storage)):
            raise ValueError('Обработчику нужно хранилище настроенной школы PostgreSQL')
        replacement_storage = settings.storage
        settings.validate()
        origin = urlsplit(os.environ.get('SHKOLA_STAFF_ORIGIN', ''))
        replacement_storage.validate_staff(school, origin.scheme, origin.hostname, staff_path)
    elif replacement_storage is None:
        if not staff_path.is_file():
            raise RuntimeError('Нужны существующие отдельные базы школы и доступа')
        if replacement_domain is not None or replacement_base_scope is not None:
            raise ValueError('Настройки очереди PostgreSQL требуют явного хранилища')
        if os.environ.get('SHKOLA_TEST_MODE') == 'isolated':
            from .test_isolation import isolated_database_paths

            isolated_database_paths()
        info = Path(os.environ['SHKOLA_DB_PATH']).stat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid() or info.st_mode & 0o022):
            raise RuntimeError('Нужны существующие отдельные базы школы и доступа')
    else:
        from .notice_jobs import bind_replacement_notice_settings
        from .replacement_postgres import ReplacementPostgres

        if not staff_path.is_file():
            raise RuntimeError('Нужны существующие отдельные базы школы и доступа')
        if (type(replacement_storage) is not ReplacementPostgres or replacement_domain is None
                or replacement_base_scope is None):
            raise ValueError('Обработчику нужна подтверждённая привязка очереди PostgreSQL')
        origin = urlsplit(os.environ.get('SHKOLA_STAFF_ORIGIN', ''))
        replacement_storage.validate_staff(school, origin.scheme, origin.hostname, staff_path)
        settings = bind_replacement_notice_settings(settings, replacement_storage)
        replacement_storage = replacement_storage.bind_queue_identity(settings.replacement_domain)
    documents_enabled = (replacement_storage is None or working_profile) and preview_enabled(school, settings)
    staff_access.register_configuration(staff_config)
    install()
    learning = StudentLearningService(school, staff_path.parent / 'student-learning.sqlite3',
                                     domain_storage=settings.storage if working_profile else None)
    service = service_for(StudentScheduleService(learning), storage=replacement_storage)
    jobs, broker = NoticeJobs(settings), create_broker(settings)

    def verify(binding, roles):
        require_current(binding)
        profile = staff_access.resolve_membership(school, binding['membershipId'])
        if profile is None:
            raise HTTPException(503)
        if authority(profile) != binding or profile['staffRole'] not in roles:
            raise HTTPException(403)
        if working_profile:
            settings.validate()
        return profile

    @broker.task(name=TASK)
    def prepare(identifier):
        try:
            with gate.shared() if gate is not None else nullcontext():
                prepare_guarded(identifier)
        except HTTPException as error:
            if error.status_code != 503:
                raise

    def prepare_guarded(identifier):
        with staff_access.bound_configuration(staff_config), bound_school(school), bound_service(learning):
            if replacement_storage is not None:
                try:
                    if working_profile:
                        settings.validate()
                    replacement_storage.queue_identity()
                except HTTPException as error:
                    if error.status_code != 503:
                        raise
                    return
            jobs.run(identifier, service.prepare, verify=lambda binding: verify(binding, {'director', 'planner'}))

    if documents_enabled:
        documents = ClubDocuments(learning, DocumentJobs(settings))

        @broker.task(name=DOCUMENT_TASK)
        def prepare_document(identifier):
            with gate.shared() if gate is not None else nullcontext():
                with staff_access.bound_configuration(staff_config), bound_school(school), bound_service(learning):
                    if working_profile:
                        try:
                            if not preview_enabled(school, settings):
                                return
                        except (RuntimeError, HTTPException):
                            return
                    documents.jobs.run(identifier, documents.prepare, verify=lambda binding: verify(binding, {'student'}))

    return broker


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('init', 'dispatch', 'dispatch-once', 'worker'))
    action = parser.parse_args().action
    try:
        os.environ['PYTHON_DOTENV_DISABLED'] = '1'
        settings = NoticeJobSettings.from_environment(staff_access.configuration()[1])
        if settings is None:
            raise RuntimeError('Фоновая подготовка выключена')
        queues = [NoticeJobs(settings)]
        from .school_runtime_gate import RuntimeGate
        gate = RuntimeGate.for_staff(staff_access.configuration()[0]) if type(settings) is SchoolNoticeSettings else None
        documents_enabled = preview_enabled(settings.school, settings)
        if documents_enabled:
            queues.append(DocumentJobs(settings))
        if action == 'init':
            with gate.shared() if gate is not None else nullcontext():
                for jobs in queues:
                    if type(settings) is SchoolNoticeSettings and documents_enabled:
                        with jobs.connect() as connection:
                            verify_job_schema(connection, jobs._table, jobs._dispatch_index, runtime=True)
                    else:
                        jobs.initialize()
        elif action == 'worker':
            worker_for(settings).worker_main(['worker', '--pool=solo', '--loglevel=WARNING',
                                              '--without-gossip', '--without-mingle', '--without-heartbeat'])
        else:
            broker = create_broker(settings)
            if action == 'dispatch-once':
                with gate.shared() if gate is not None else nullcontext():
                    for jobs in queues:
                        jobs.dispatch(broker)
            else:
                from kombu.exceptions import KombuError
                from psycopg import Error
                from redis.exceptions import RedisError

                while True:
                    try:
                        with gate.shared() if gate is not None else nullcontext():
                            for jobs in queues:
                                jobs.dispatch(broker)
                    except (Error, KombuError, RedisError, OSError, HTTPException) as error:
                        if isinstance(error, HTTPException) and error.status_code != 503:
                            raise
                        print('Служба очереди недоступна; диспетчер повторит проверку.', flush=True)
                    time.sleep(2)
    except Exception:
        raise SystemExit('Команда очереди завершилась ошибкой. Проверьте конфигурацию и доступность служб.') from None


if __name__ == '__main__':
    main()
