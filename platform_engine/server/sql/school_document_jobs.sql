-- Operator-only provisioning for the existing synthetic club-charge PDF queue.
-- Runtime role receives SELECT, INSERT, UPDATE separately via a quoted role
-- identifier; it must not receive CREATE, DELETE, TRUNCATE or object ownership.
-- Selected `notice_worker init` verifies this schema and performs no DDL.
CREATE TABLE IF NOT EXISTS public.shkola_document_jobs (
    id uuid PRIMARY KEY,
    school text NOT NULL,
    host_scope text NOT NULL,
    member text NOT NULL,
    request_id text NOT NULL,
    fingerprint text NOT NULL,
    actor jsonb NOT NULL,
    command jsonb NOT NULL,
    state text NOT NULL CHECK (state IN ('queued','running','succeeded','rejected')),
    next_dispatch_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    lease_token uuid,
    lease_until timestamptz,
    receipt jsonb,
    rejection int,
    UNIQUE(school,host_scope,member,request_id)
);
CREATE INDEX IF NOT EXISTS shkola_document_jobs_dispatch
    ON public.shkola_document_jobs (school,host_scope,next_dispatch_at)
    WHERE state IN ('queued','running');
REVOKE ALL ON public.shkola_document_jobs FROM PUBLIC;
