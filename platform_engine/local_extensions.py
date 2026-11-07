"""Explicit independent deployment adapters; native business validators stay intact."""
import json
from pathlib import Path
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from server.club_documents import ClubDocuments, DocumentInput
from server.notice_jobs import authority, require_current
from server.request_planning import digest
from server import school_clubs, club_payment_reconciliation


class LocalDocumentJobs:
    """Durable bounded single-worker jobs for local deterministic PDF rendering."""
    def __init__(self, path):
        self.path = Path(path)
        self.prepare = None
        with sqlite3.connect(path) as database:
            database.execute('''CREATE TABLE IF NOT EXISTS document_jobs(
                key TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,state TEXT NOT NULL,receipt TEXT)''')

    def submit(self, command, profile):
        binding = authority(profile)
        require_current(binding)
        if profile.get('staffRole') != 'student':
            raise HTTPException(403, 'Документ доступен только личному ученику')
        fingerprint = digest([binding, command])
        key = digest([binding['schoolId'], binding['membershipId'], command['requestId']])
        with sqlite3.connect(self.path) as database:
            database.execute('BEGIN IMMEDIATE')
            row = database.execute('SELECT fingerprint,state,receipt FROM document_jobs WHERE key=?', (key,)).fetchone()
            if row:
                if row[0] != fingerprint:
                    raise HTTPException(409, 'Команда документа изменилась')
                if row[1] != 'ready':
                    raise HTTPException(409, 'Начатая подготовка требует проверки; повтор отключён')
                require_current(binding)
                return 200, json.loads(row[2])
            if database.execute('SELECT COUNT(*) FROM document_jobs').fetchone()[0] >= 1000:
                raise HTTPException(429, 'Достигнут предел локальных документов')
            database.execute("INSERT INTO document_jobs VALUES (?,?,'started',NULL)", (key, fingerprint))
        receipt = self.prepare(command, profile)
        require_current(binding)
        with sqlite3.connect(self.path) as database:
            database.execute("UPDATE document_jobs SET state='ready',receipt=? WHERE key=? AND state='started'",
                             (json.dumps(receipt, ensure_ascii=False), key))
        return 200, receipt


def install(native, learning, actor, require_session, root):
    jobs = LocalDocumentJobs(root / 'document-jobs.sqlite3')
    documents = ClubDocuments(learning, jobs)
    jobs.prepare = documents.prepare
    router = APIRouter()

    @router.get('/api/student/clubs')
    def own_clubs(request: Request, profile=Depends(actor)):
        if request.query_params:
            raise HTTPException(422, 'Личный список кружков не принимает фильтры')
        require_session(request, profile)
        with learning.domain_scope() as connection:
            value = school_clubs.student_overview(learning, profile, connection=connection)
            value['documents'] = documents.listing(profile, connection=connection)
        value.update(club_payment_reconciliation.student_receipts(learning, profile))
        value['documentBackend'] = 'independent-local-document-jobs'
        require_session(request, profile)
        return value

    @router.post('/api/student/clubs/documents/prepare')
    def prepare_document(body: DocumentInput, request: Request, profile=Depends(actor)):
        if request.query_params:
            raise HTTPException(422, 'Учебный документ не принимает параметры запроса')
        require_session(request, profile)
        code, value = documents.submit(body, profile,
            before_return=lambda: require_session(request, profile))
        return JSONResponse({**value, 'backend': 'independent-local-document-jobs'}, status_code=code)

    router.include_router(club_payment_reconciliation.router_for(learning, actor, require_session))
    replaced = {'/api/student/clubs', '/api/student/clubs/documents/prepare'}
    existing = [r for r in native.router.routes if getattr(r, 'path', '') not in replaced]
    native.router.routes = []
    native.include_router(router)
    native.router.routes.extend(existing)
    native.openapi_schema = None
    return documents
