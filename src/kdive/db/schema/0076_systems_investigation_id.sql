-- 0076_systems_investigation_id.sql — advisory System<->Investigation binding + the schema
-- foundation for investigation-scoped uploaded rootfs (ADR-0441, #1502).
-- Additive, forward-only (ADR-0015). `systems.investigation_id` is nullable with no default: a
-- classic allocation-only System keeps it NULL and is unaffected by anything downstream; the
-- index backs both the close-coupling check (ADR-0441 §7) and the reclaim-gate's referencer
-- enumeration (ADR-0441 §6). `investigations.rootfs_cleanup_pending_at` is a *dedicated* reclaim
-- marker for the investigation-scoped uploaded-rootfs sweep (ADR-0441 §6) — it must NOT reuse
-- `cleanup_pending_at`, which `gc_investigation_artifacts` (0048) owns and clears independently
-- of the rootfs sweep's own worklist. The partial UNIQUE index on `artifacts (object_key)` backs
-- `investigations.complete_rootfs_upload` finalize idempotency (ADR-0441 §3) for
-- `owner_kind = 'investigations'` rows only — partial so it never touches existing rows of any
-- other owner kind. `artifacts.encoding` / `uncompressed_size` are the durable home for the
-- transport encoding a post-finalize provision needs (ADR-0441 §3/§5); both nullable, absent
-- means identity encoding.

ALTER TABLE systems ADD COLUMN investigation_id uuid REFERENCES investigations (id);
CREATE INDEX systems_investigation_id_idx ON systems (investigation_id);

ALTER TABLE investigations ADD COLUMN rootfs_cleanup_pending_at timestamptz;

CREATE UNIQUE INDEX artifacts_investigations_object_key_uniq
    ON artifacts (object_key) WHERE owner_kind = 'investigations';

ALTER TABLE artifacts
    ADD COLUMN encoding text,
    ADD COLUMN uncompressed_size bigint;
