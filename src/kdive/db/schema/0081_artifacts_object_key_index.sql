-- 0081_artifacts_object_key_index.sql — general btree index on artifacts.object_key (#1570).
-- Additive, forward-only (ADR-0015).
--
-- The upload orphan sweep's reclaimability query (`_RECLAIMABLE_SQL` in
-- `kdive.reconciler.cleanup.upload_orphans`) anti-joins `artifacts` on `object_key` alone, with no
-- `owner_kind` filter, once per root in the bulk classify and again per candidate in the pre-delete
-- re-check. The only existing index that touches this column is the partial unique index from 0076,
-- `artifacts_investigations_object_key_uniq ON artifacts (object_key) WHERE owner_kind =
-- 'investigations'` — the planner cannot use a partial index to prove non-existence for a query with
-- no `owner_kind` predicate, so every `local/runs/` key (owner_kind = 'runs') falls back to a
-- sequential scan of the whole table. The match is plain equality over an `unnest`, not a prefix or
-- pattern search, so a plain btree suffices; no special operator class is needed.
--
-- This index is additional, not a replacement: the 0076 partial unique index still carries the
-- uniqueness constraint `investigations.complete_rootfs_upload` finalize idempotency depends on
-- (ADR-0441 §3), so it stays exactly as it is.

CREATE INDEX artifacts_object_key_idx ON artifacts (object_key);
