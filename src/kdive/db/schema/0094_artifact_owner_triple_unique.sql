-- 0094_artifact_owner_triple_unique.sql — one catalog claim per artifact ownership triple
-- (ADR-0528, #1750). Additive, forward-only (ADR-0015). Existing duplicate claims make this
-- migration fail for explicit operator repair; choosing or deleting durable claims is unsafe.
CREATE UNIQUE INDEX artifacts_owner_triple_uniq
    ON artifacts (owner_kind, owner_id, object_key);
