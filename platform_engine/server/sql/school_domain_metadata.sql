-- Provision explicitly after validating and importing one school's domain.
-- The application validates the marker and never creates or changes it.
-- One dedicated database per school. Runtime: SELECT and UPDATE(id) only.
-- UPDATE(id) permits FOR SHARE; CHECK(id=1) prevents changing the ID.
-- No DELETE, TRUNCATE, schema CREATE or updates to identity columns.
CREATE TABLE public.school_domain_metadata (
    id integer PRIMARY KEY CHECK (id = 1),
    school_id text NOT NULL,
    domain_id uuid NOT NULL,
    restore_epoch uuid NOT NULL,
    schema_version integer NOT NULL CHECK (schema_version = 1)
);
