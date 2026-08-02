-- 0099_investigation_build_use_recovery.sql — independently evidenced use-fence recovery.
-- An operator may release a fence only after independently confirming that the exact
-- worker process is dead. Keep that evidence after the use row and generation are gone.
CREATE TABLE investigation_build_use_recoveries (
    use_id uuid PRIMARY KEY,
    investigation_id uuid NOT NULL,
    generation uuid NOT NULL,
    job_id uuid NOT NULL,
    attempt integer NOT NULL,
    holder_worker_id text NOT NULL,
    recovered_by text NOT NULL,
    evidence text NOT NULL,
    recovered_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT investigation_build_use_recoveries_evidence_nonempty
        CHECK (length(btrim(evidence)) > 0),
    CONSTRAINT investigation_build_use_recoveries_actor_nonempty
        CHECK (length(btrim(recovered_by)) > 0)
);
