-- 0054_artifacts_run_id.sql — correlate console artifacts to the Run active during
-- their window (ADR-0279, #935). Additive, forward-only (ADR-0015). Nullable; existing
-- rows and every non-console insert read NULL ("uncorrelated"). This is a correlation
-- attribute orthogonal to (owner_kind, owner_id) ownership — a console artifact stays
-- owner_kind='systems'; run_id additionally records the Run it belongs to. The partial
-- index serves the Run-scoped console manifest query (WHERE run_id = <run>).
ALTER TABLE artifacts ADD COLUMN run_id uuid REFERENCES runs (id);
CREATE INDEX artifacts_run_id_idx ON artifacts (run_id) WHERE run_id IS NOT NULL;
