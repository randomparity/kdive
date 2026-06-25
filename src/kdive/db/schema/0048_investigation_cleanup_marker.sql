-- 0048_investigation_cleanup_marker.sql — uploaded-build-artifact cleanup marker (ADR-0234 §4, #768).
-- Additive, forward-only (ADR-0015). `cleanup_pending_at` marks an investigation whose run-owned
-- build artifacts the reconciler `gc_investigation_artifacts` sweep should reclaim after a grace
-- window. `investigations.close` stamps it; already-closed rows are back-marked with `updated_at`
-- (their frozen close instant — `closed` is terminal and link/set/unlink refuse terminal rows) so
-- the close-driven sweep also reclaims historical closed investigations. NULL = not pending.
ALTER TABLE investigations ADD COLUMN cleanup_pending_at timestamptz;

UPDATE investigations SET cleanup_pending_at = updated_at WHERE state = 'closed';
