-- Exact executing-attempt fences (ADR-0531, #1519).
CREATE TABLE investigation_build_uses (
    use_id uuid PRIMARY KEY,
    investigation_id uuid NOT NULL,
    generation uuid NOT NULL,
    job_id uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    attempt integer NOT NULL CHECK (attempt > 0),
    holder_worker_id text NOT NULL,
    lease_expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT investigation_build_uses_generation_fkey
        FOREIGN KEY (investigation_id, generation)
        REFERENCES investigation_builds(investigation_id, generation) ON DELETE RESTRICT
);
CREATE INDEX investigation_build_uses_generation_idx
    ON investigation_build_uses (investigation_id, generation);
CREATE INDEX investigation_build_uses_live_generation_idx
    ON investigation_build_uses (investigation_id, generation, lease_expires_at);
