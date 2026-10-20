-- ADR-0786: journal capture destinations and retain retirement fences.
ALTER TABLE runs ADD COLUMN vmcore_retired_at timestamptz;
ALTER TABLE runs ADD COLUMN vmcore_download_protected_until timestamptz;

CREATE TABLE vmcore_capture_executions (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES runs(id),
    job_id uuid,
    job_attempt integer,
    method text NOT NULL CHECK (method IN ('kdump', 'fadump', 'host_dump')),
    raw_key text NOT NULL UNIQUE,
    redacted_key text NOT NULL UNIQUE,
    state text NOT NULL DEFAULT 'capturing'
        CHECK (state IN ('capturing', 'published', 'retiring', 'retired')),
    legacy boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz,
    captured_at timestamptz,
    raw_version_id text,
    redacted_version_id text,
    raw_tombstone_version_id text,
    redacted_tombstone_version_id text,
    retry_at timestamptz,
    CHECK (raw_key <> redacted_key),
    CHECK (legacy OR (job_id IS NOT NULL AND job_attempt IS NOT NULL AND job_attempt > 0)),
    CHECK (state <> 'published' OR
        (raw_version_id IS NOT NULL AND redacted_version_id IS NOT NULL
         AND captured_at IS NOT NULL AND published_at IS NOT NULL)),
    CHECK (state <> 'retired' OR
        (raw_tombstone_version_id IS NOT NULL AND redacted_tombstone_version_id IS NOT NULL))
);
CREATE UNIQUE INDEX vmcore_one_published_pair ON vmcore_capture_executions(run_id)
    WHERE state = 'published';
CREATE INDEX vmcore_retirement_due ON vmcore_capture_executions(retry_at, created_at, id)
    WHERE state <> 'retired';
CREATE INDEX vmcore_published_age ON vmcore_capture_executions(captured_at, id)
    WHERE state = 'published';
CREATE TABLE vmcore_retention_baseline (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    mode text NOT NULL CHECK (mode IN ('fresh', 'legacy_verified')),
    completed_at timestamptz NOT NULL DEFAULT now()
);

REVOKE ALL ON vmcore_capture_executions, vmcore_retention_baseline FROM PUBLIC;
GRANT SELECT ON vmcore_capture_executions, vmcore_retention_baseline
    TO kdive_server, kdive_worker, kdive_reconciler;
GRANT INSERT, UPDATE ON vmcore_capture_executions TO kdive_worker;
GRANT UPDATE ON vmcore_capture_executions TO kdive_server, kdive_reconciler;
