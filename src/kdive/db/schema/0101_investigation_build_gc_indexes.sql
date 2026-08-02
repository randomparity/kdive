-- Bound Investigation-build GC pin checks after candidate keyset selection (ADR-0531, #1519).
CREATE INDEX runs_live_build_ref_idx
    ON runs (investigation_id, build_ref)
    WHERE build_ref IS NOT NULL AND state IN ('created', 'running');

CREATE INDEX jobs_live_install_run_id_idx
    ON jobs ((payload->>'run_id'))
    WHERE kind = 'install' AND state IN ('queued', 'running');
