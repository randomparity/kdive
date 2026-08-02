-- 0096_investigation_build_safety.sql — executing-attempt fences and fair reclaim retries.
ALTER TABLE investigation_builds ADD COLUMN reclaim_retry_at timestamptz;

CREATE TABLE investigation_build_uses (
    use_id uuid PRIMARY KEY,
    investigation_id uuid NOT NULL,
    generation uuid NOT NULL,
    job_id uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    attempt integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT investigation_build_uses_generation_fkey
        FOREIGN KEY (investigation_id, generation)
        REFERENCES investigation_builds(investigation_id, generation) ON DELETE RESTRICT,
    CONSTRAINT investigation_build_uses_job_attempt_key UNIQUE (job_id, attempt)
);
CREATE INDEX investigation_build_uses_generation_idx
    ON investigation_build_uses (investigation_id, generation);

CREATE TABLE investigation_build_gc_cursor (
    lane text PRIMARY KEY,
    investigation_id uuid,
    generation uuid
);
INSERT INTO investigation_build_gc_cursor (lane) VALUES ('expired'), ('closed');
