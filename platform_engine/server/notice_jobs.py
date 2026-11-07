"""Durable queues for explicit school notice and document preparation."""
from dataclasses import dataclass, field, replace
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import HTTPException

from .integration_settings import _flag, _secret
from .request_planning import digest


TASK = 'shkola.prepare_schedule_notices'
TABLE = 'public.shkola_notice_jobs'
DOCUMENT_TASK = 'shkola.prepare_club_document'
DOCUMENT_TABLE = 'public.shkola_document_jobs'
AUTHORITY_FIELDS = ('schoolId', 'userId', 'membershipId', 'membershipRevision', 'role',
                    'staffRole', 'teacherRefs', 'calendarTeacherIds', 'permissions')
CONFLICT = 'Запрос уже использован или доступ изменился'
UNAVAILABLE = 'Хранилище фоновой подготовки недоступно. Проверьте тот же запрос позже.'


def verify_job_schema(connection, table, dispatch_index, *, runtime=False):
    """Read-only validation of the two operator-provisioned queue schemas."""
    names = {TABLE: 'shkola_notice_jobs', DOCUMENT_TABLE: 'shkola_document_jobs'}
    name = names.get(table)
    if name is None or dispatch_index != name + '_dispatch':
        raise ValueError('Unknown school queue schema')
    try:
        relation = connection.execute('''SELECT c.oid,c.relkind,c.relpersistence,c.relrowsecurity,c.relforcerowsecurity
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='public' AND c.relname=%s''', (name,)).fetchone()
        if (not relation or relation['relkind'] != 'r' or relation['relpersistence'] != 'p'
                or relation['relrowsecurity'] or relation['relforcerowsecurity']):
            raise HTTPException(503, UNAVAILABLE)
        oid = relation['oid']
        columns = connection.execute('''SELECT a.attname,format_type(a.atttypid,a.atttypmod) AS type,
            a.attnotnull,pg_get_expr(d.adbin,d.adrelid) AS default_value
            FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
            WHERE a.attrelid=%s AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum''', (oid,)).fetchall()
        specification = [('id','uuid',True),('school','text',True),('host_scope','text',True),
            ('member','text',True),('request_id','text',True),('fingerprint','text',True),
            ('actor','jsonb',True),('command','jsonb',True),('state','text',True),
            ('next_dispatch_at','timestamp with time zone',True),('lease_token','uuid',False),
            ('lease_until','timestamp with time zone',False),('receipt','jsonb',False),('rejection','integer',False)]
        if ([(c['attname'],c['type'],c['attnotnull']) for c in columns] != specification
                or any(c['default_value'] != ('clock_timestamp()' if c['attname']=='next_dispatch_at' else None)
                       for c in columns)):
            raise HTTPException(503, UNAVAILABLE)
        constraints = connection.execute('''SELECT contype,conkey::text AS keys,convalidated,
            condeferrable,condeferred,pg_get_constraintdef(oid) AS definition
            FROM pg_constraint WHERE conrelid=%s''', (oid,)).fetchall()
        checks = []
        for row in constraints:
            if not row['convalidated'] or row['condeferrable'] or row['condeferred']:
                raise HTTPException(503, UNAVAILABLE)
            checks.append((row['contype'], row['keys'], re.sub(r'\s+', '', row['definition'])))
        expected_check = "CHECK((state=ANY(ARRAY['queued'::text,'running'::text,'succeeded'::text,'rejected'::text])))"
        expected_constraints = [('p','{1}','PRIMARYKEY(id)'),
            ('u','{2,3,4,5}','UNIQUE(school,host_scope,member,request_id)'),('c','{9}',expected_check)]
        not_null_constraints = [('n','{' + str(number) + '}','NOTNULL' + column)
            for number,(column,_type,required) in enumerate(specification,1) if required]
        # PG18 also exposes the complete NOT NULL set in pg_constraint. Keep
        # the pre-18 catalogue and that exact set; partial/extra sets fail.
        if sorted(checks) not in (sorted(expected_constraints),
                sorted(expected_constraints + not_null_constraints)):
            raise HTTPException(503, UNAVAILABLE)
        indexes = connection.execute('''SELECT c.relname,i.indkey::text AS keys,i.indisprimary,
            i.indisunique,i.indisvalid,i.indisready,pg_get_expr(i.indpred,i.indrelid) AS predicate,
            i.indexprs IS NULL AS plain,a.amname,c.relpersistence
            FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid JOIN pg_am a ON a.oid=c.relam
            WHERE i.indrelid=%s''', (oid,)).fetchall()
        observed = []
        for row in indexes:
            if (not row['indisvalid'] or not row['indisready'] or not row['plain']
                    or row['amname'] != 'btree' or row['relpersistence'] != 'p'):
                raise HTTPException(503, UNAVAILABLE)
            if row['relname'] == dispatch_index:
                predicate = re.sub(r'\s+', '', row['predicate'] or '')
                if predicate != "(state=ANY(ARRAY['queued'::text,'running'::text]))":
                    raise HTTPException(503, UNAVAILABLE)
                observed.append(('dispatch',row['keys'],row['indisprimary'],row['indisunique']))
            elif row['predicate'] is None:
                observed.append(('constraint',row['keys'],row['indisprimary'],row['indisunique']))
            else:
                raise HTTPException(503, UNAVAILABLE)
        if sorted(observed) != sorted([('dispatch','2 3 10',False,False),
                ('constraint','1',True,True),('constraint','2 3 4 5',False,True)]):
            raise HTTPException(503, UNAVAILABLE)
        if connection.execute('''SELECT 1 FROM pg_inherits WHERE inhrelid=%s OR inhparent=%s
            UNION ALL SELECT 1 FROM pg_trigger WHERE tgrelid=%s AND NOT tgisinternal LIMIT 1''',
                (oid,oid,oid)).fetchone():
            raise HTTPException(503, UNAVAILABLE)
        if runtime:
            permissions = connection.execute('''SELECT
                has_table_privilege(current_user,%s,'SELECT') AS can_select,
                has_table_privilege(current_user,%s,'INSERT') AS can_insert,
                has_table_privilege(current_user,%s,'UPDATE') AS can_update,
                has_table_privilege(current_user,%s,'DELETE') AS can_delete,
                has_table_privilege(current_user,%s,'TRUNCATE') AS can_truncate,
                (has_schema_privilege(current_user,'public','CREATE')
                 OR has_database_privilege(current_user,current_database(),'CREATE')) AS can_create,
                (pg_has_role(current_user,c.relowner,'MEMBER')
                 OR pg_has_role(current_user,(SELECT datdba FROM pg_database WHERE datname=current_database()),'MEMBER')
                 OR pg_has_role(current_user,(SELECT nspowner FROM pg_namespace WHERE nspname='public'),'MEMBER')) AS is_owner,
                (EXISTS(SELECT 1 FROM unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER']) p(privilege)
                    WHERE has_table_privilege(current_user,c.oid,p.privilege||' WITH GRANT OPTION'))
                 OR EXISTS(SELECT 1 FROM unnest(ARRAY['SELECT','INSERT','UPDATE','REFERENCES']) p(privilege)
                    WHERE has_any_column_privilege(current_user,c.oid,p.privilege||' WITH GRANT OPTION'))) AS has_grant_option
                FROM pg_class c WHERE c.oid=%s''', (oid,oid,oid,oid,oid,oid)).fetchone()
            if (not permissions or not all(permissions[k] for k in ('can_select','can_insert','can_update'))
                    or any(permissions[k] for k in ('can_delete','can_truncate','can_create','is_owner','has_grant_option'))):
                raise HTTPException(503, UNAVAILABLE)

    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, UNAVAILABLE) from None


def authority(actor):
    return {key: actor[key] for key in AUTHORITY_FIELDS} | {'membershipVerified': True}


def require_current(binding):
    from . import staff_access

    verdict = staff_access.membership_status(binding)
    if verdict == 'unverifiable':
        raise HTTPException(503, 'Не удалось подтвердить доступ. Проверьте тот же запрос позже.')
    if verdict != 'current':
        raise HTTPException(403, 'Школьный доступ изменился')


def _endpoint(value, schemes):
    parsed = urlsplit(value)
    if (parsed.scheme not in schemes or not re.fullmatch(r'[A-Za-z0-9.-]+', parsed.hostname or '')
            or parsed.port is None or parsed.query or parsed.fragment or len(parsed.path) <= 1
            or not parsed.password):
        raise ValueError('Нужен явный защищённый адрес службы очереди')
    return parsed


@dataclass(frozen=True)
class NoticeJobSettings:
    school: str
    scope: str
    postgres: str = field(repr=False)
    redis: str = field(repr=False)
    postgres_tls: str
    replacement_domain: str | None = None
    replacement_base_scope: str | None = None

    @classmethod
    def from_environment(cls, school):
        if not _flag(os.environ, 'SHKOLA_NOTICE_QUEUE_ENABLED'):
            return None
        if os.environ.get('SHKOLA_DOMAIN_BACKEND') == 'postgresql':
            from .school_notice_jobs import SchoolNoticeSettings
            return SchoolNoticeSettings.from_environment(school)
        try:
            paths = [Path(os.environ.get(key, '')) for key in ('SHKOLA_STAFF_DB_PATH', 'SHKOLA_DB_PATH')]
            if any(not path.is_absolute() or '..' in path.parts or path.resolve() != path for path in paths):
                raise ValueError()
            postgres = _secret(os.environ, 'SHKOLA_NOTICE_PG_URL', needed=True)
            redis = _secret(os.environ, 'SHKOLA_NOTICE_REDIS_URL', needed=True)
            pg = _endpoint(postgres, {'postgresql'})
            broker = _endpoint(redis, {'redis', 'rediss'})
            pg_local = pg.hostname == '127.0.0.1'
            if (not pg.username or not pg.password or not broker.password
                    or broker.scheme == 'redis' and broker.hostname != '127.0.0.1'
                    or any(os.environ.get(key) for key in
                           ('CELERY_BROKER_URL', 'CELERY_RESULT_BACKEND', 'CELERY_CONFIG_MODULE'))):
                raise ValueError()
        except (OSError, ValueError, UnicodeError):
            raise RuntimeError('Неверная защищённая настройка очереди уведомлений') from None
        return cls(school, digest([school, *(str(path) for path in paths)]), postgres, redis,
                   'disable' if pg_local else 'verify-full')


def bind_replacement_notice_settings(settings, storage):
    """Bind an explicitly local queue to the exact imported PostgreSQL domain."""
    from .replacement_postgres import ReplacementPostgres, SCHOOL

    if type(settings) is not NoticeJobSettings or type(storage) is not ReplacementPostgres:
        raise ValueError('Нужны явные настройки очереди и школьного стенда')
    base = settings.scope if settings.replacement_base_scope is None else settings.replacement_base_scope
    try:
        jobs = _endpoint(settings.postgres, {'postgresql'})
        broker = _endpoint(settings.redis, {'redis'})
        if (settings.school != SCHOOL or jobs.hostname != '127.0.0.1' or not jobs.username
                or broker.hostname != '127.0.0.1' or settings.postgres_tls != 'disable'
                or not isinstance(base, str) or not re.fullmatch(r'[a-f0-9]{64}', base)
                or any(key.startswith('PG') or key in {'CELERY_BROKER_URL', 'CELERY_RESULT_BACKEND',
                                                      'CELERY_CONFIG_MODULE'} for key in os.environ)):
            raise ValueError()
    except (AttributeError, TypeError, ValueError):
        raise ValueError('Очередь стенда требует явные локальные службы') from None
    identity = storage.queue_identity()
    scope = digest([base, 'replacement-pg-v1', str(storage.socket_dir), identity])
    if settings.replacement_domain is None and settings.replacement_base_scope is None:
        return replace(settings, scope=scope, replacement_domain=identity, replacement_base_scope=base)
    if (settings.replacement_domain, settings.scope, settings.replacement_base_scope) != (identity, scope, base):
        raise ValueError('Очередь привязана к другому школьному домену')
    return settings


class NoticeJobs:
    _table = TABLE
    _dispatch_index = 'shkola_notice_jobs_dispatch'
    _task = TASK
    _roles = frozenset({'director', 'planner'})
    _terminal_statuses = frozenset({403, 409, 422, 429})
    _rejection_message = 'Подготовка отклонена. Проверьте школьный доступ и версию публикации.'
    _limit_message = 'Достигнут предел журнала уведомлений. Требуется архивирование.'

    def __init__(self, settings):
        self.settings = settings

    def connect(self):
        from .school_notice_jobs import SchoolNoticeSettings
        if type(self.settings) is SchoolNoticeSettings:
            return self.settings.connect()
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(self.settings.postgres, sslmode=self.settings.postgres_tls,
                               connect_timeout=3, options='-c statement_timeout=5000 -c lock_timeout=3000',
                               row_factory=dict_row)

    def initialize(self):
        with self.connect() as db:
            db.execute(f"""CREATE TABLE IF NOT EXISTS {self._table} (
                id uuid PRIMARY KEY, school text NOT NULL, host_scope text NOT NULL, member text NOT NULL,
                request_id text NOT NULL, fingerprint text NOT NULL, actor jsonb NOT NULL, command jsonb NOT NULL,
                state text NOT NULL CHECK (state IN ('queued','running','succeeded','rejected')),
                next_dispatch_at timestamptz NOT NULL DEFAULT clock_timestamp(),
                lease_token uuid, lease_until timestamptz, receipt jsonb, rejection int,
                UNIQUE(school,host_scope,member,request_id)
            )""")
            db.execute(f"""CREATE INDEX IF NOT EXISTS {self._dispatch_index}
                ON {self._table} (school,host_scope,next_dispatch_at) WHERE state IN ('queued','running')""")

    def submit(self, command, actor):
        # The HTTP frontdoor has already checked CSRF and the current actor.
        binding = authority(actor)
        school, member, request = self.settings.school, actor['membershipId'], command['requestId']
        if school != actor['schoolId'] or actor['staffRole'] not in self._roles:
            raise HTTPException(403, 'Школьный доступ изменился')
        require_current(binding)
        key = (school, self.settings.scope, member, request)
        query = f"""SELECT id,fingerprint,receipt,rejection,
                    CASE WHEN state='running' AND lease_until<=clock_timestamp() THEN 'queued' ELSE state END AS state
                    FROM {self._table}
                    WHERE school=%s AND host_scope=%s AND member=%s AND request_id=%s"""
        fingerprint = digest([binding, command])
        try:
            from psycopg import Error
            from psycopg.types.json import Jsonb
        except ImportError:
            raise HTTPException(503, UNAVAILABLE) from None
        try:
            with self.connect() as db:
                row = db.execute(query, key).fetchone()
                if row is None:
                    db.execute(f"""INSERT INTO {self._table}
                        (id,school,host_scope,member,request_id,fingerprint,actor,command,state)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'queued')
                        ON CONFLICT(school,host_scope,member,request_id) DO NOTHING""",
                        (uuid4(), *key, fingerprint, Jsonb(binding), Jsonb(command)))
                    row = db.execute(query, key).fetchone()
                require_current(binding)
        except Error:
            raise HTTPException(503, UNAVAILABLE) from None
        if row['fingerprint'] != fingerprint:
            raise HTTPException(409, CONFLICT)
        if row['state'] == 'rejected':
            if row['rejection'] == 429:
                raise HTTPException(409, self._limit_message)
            raise HTTPException(row['rejection'], self._rejection_message)
        if row['state'] == 'succeeded':
            return 200, row['receipt']
        return 202, {'status': row['state'], 'jobId': str(row['id']), 'requestId': request}

    def dispatch(self, broker):
        with self.connect() as db:
            rows = db.execute(f"""WITH available AS (
                SELECT id FROM {self._table}
                WHERE school=%s AND host_scope=%s AND next_dispatch_at<=clock_timestamp()
                    AND (state='queued' OR (state='running' AND lease_until<=clock_timestamp()))
                ORDER BY next_dispatch_at,id LIMIT 20 FOR UPDATE SKIP LOCKED
            ) UPDATE {self._table} AS job
                SET next_dispatch_at=clock_timestamp()+interval '30 seconds'
                FROM available WHERE job.id=available.id RETURNING job.id""",
                (self.settings.school, self.settings.scope)).fetchall()
        for row in rows:
            # Reservation is committed before the network call. A lost publish
            # or broker restart makes the same durable row eligible again.
            broker.send_task(self._task, args=[str(row['id'])], retry=False)

    def run(self, identifier, prepare, *, verify):
        from psycopg.types.json import Jsonb

        try:
            job = UUID(identifier)
        except (ValueError, AttributeError, TypeError):
            return
        if str(job) != identifier:
            return
        token = uuid4()
        with self.connect() as db:
            row = db.execute(f"""UPDATE {self._table}
                SET state='running',lease_token=%s,lease_until=clock_timestamp()+interval '2 minutes'
                WHERE school=%s AND host_scope=%s AND id=%s
                    AND (state='queued' OR (state='running' AND lease_until<=clock_timestamp()))
                RETURNING actor,command,fingerprint""", (token, self.settings.school, self.settings.scope, job)).fetchone()
        if row is None:
            return
        try:
            if row['actor'].get('schoolId') != self.settings.school or digest([row['actor'], row['command']]) != row['fingerprint']:
                raise HTTPException(409)
            actor = verify(row['actor'])
            receipt = prepare(row['command'], actor)
        except HTTPException as error:
            if error.status_code not in self._terminal_statuses:
                return  # An uncertain operation keeps the lease and exact request for retry.
            state, receipt, status = 'rejected', None, error.status_code
        else:
            state, status = 'succeeded', None
        with self.connect() as db:
            db.execute(f"""UPDATE {self._table} SET state=%s,receipt=%s,rejection=%s,lease_token=NULL,lease_until=NULL
                WHERE school=%s AND host_scope=%s AND id=%s AND state='running' AND lease_token=%s""",
                (state, Jsonb(receipt) if receipt is not None else None, status,
                 self.settings.school, self.settings.scope, job, token))


class DocumentJobs(NoticeJobs):
    _table = DOCUMENT_TABLE
    _dispatch_index = 'shkola_document_jobs_dispatch'
    _task = DOCUMENT_TASK
    _roles = frozenset({'student'})
    _terminal_statuses = NoticeJobs._terminal_statuses | {404}
    _rejection_message = 'Документ недоступен. Проверьте школьный доступ и обновите список документов.'
    _limit_message = 'Достигнут предел подготовки документов. Требуется архивирование.'

    def initialize(self):
        from .school_notice_jobs import SchoolNoticeSettings
        if type(self.settings) is SchoolNoticeSettings:
            with self.connect() as db:
                verify_job_schema(db, self._table, self._dispatch_index, runtime=True)
            return
        return super().initialize()

    def run(self, identifier, prepare, *, verify):
        def prepare_current(command, actor):
            try:
                return prepare(command, actor)
            except HTTPException as error:
                # Only this fixed document source path reports known mid-render
                # membership loss as 401; HTTP/session policy remains unchanged.
                if error.status_code == 401:
                    raise HTTPException(403, self._rejection_message) from None
                raise
        return super().run(identifier, prepare_current, verify=verify)


def create_broker(settings):
    from .school_notice_jobs import SchoolNoticeSettings
    if type(settings) is SchoolNoticeSettings:
        settings.validate()
    from celery import Celery

    broker = Celery('shkola-notices', broker=settings.redis)
    broker.conf.update(task_default_queue='shkola-notices-' + settings.scope,
                       task_serializer='json', accept_content=['json'], task_ignore_result=True,
                       task_acks_late=True, worker_prefetch_multiplier=1, worker_enable_remote_control=False,
                       broker_connection_timeout=3, broker_connection_retry_on_startup=False,
                       broker_transport_options={'socket_connect_timeout': 3, 'socket_timeout': 3,
                                                 'visibility_timeout': 300},
                       broker_use_ssl={'ssl_cert_reqs': 'required', 'ssl_check_hostname': True}
                       if settings.redis.startswith('rediss:') else None)
    return broker
