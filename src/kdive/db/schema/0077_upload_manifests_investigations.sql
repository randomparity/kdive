-- 0077_upload_manifests_investigations.sql — admit an investigation-scoped upload owner
-- (ADR-0441, #1502). Additive, forward-only (ADR-0015). The investigation-scoped uploaded
-- rootfs (ADR-0441 §3) reuses the ADR-0048 upload_manifests table for its upload window under
-- owner ('investigations', inv_id), but 0006's owner_kind CHECK only admits ('runs','systems').
-- Widen it to include 'investigations' so `artifacts.create_investigation_upload` can persist
-- its manifest; the finalize (`investigations.complete_rootfs_upload`) deletes it, and the
-- re-scoped manifest reaper collects a stale window. No data change — the constraint only widens.
ALTER TABLE upload_manifests DROP CONSTRAINT upload_manifests_owner_kind_check;
ALTER TABLE upload_manifests ADD CONSTRAINT upload_manifests_owner_kind_check
    CHECK (owner_kind IN ('runs', 'systems', 'investigations'));
