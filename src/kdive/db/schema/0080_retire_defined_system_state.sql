-- 0080_retire_defined_system_state.sql — resolve stranded `defined` Systems and retire the state
-- (#1600, ADR-0457 §2/§3). Forward-only (ADR-0015). Data resolution first, then the CHECK tighten.
--
-- `systems.define` was the only producer of `defined` and `systems.provision_defined` the only
-- consumer that advanced it; both are removed in this change, so the state becomes unreachable and
-- leaves `SystemState`. A pre-existing `defined` row that survived that removal would be stranded
-- twice over: it holds a `max_concurrent_systems` slot (admission's `_NON_TERMINAL_SYSTEM`) and an
-- `active` Allocation, `systems.provision` refuses to advance it, and the tool its rejection names
-- no longer exists. Worse, `System.model_validate` would raise on the unknown enum value, so
-- `systems.get` / `systems.list` / every admission read over that row would fail outright. The
-- migration is therefore mandatory, not a convenience backfill (ADR-0457 §3).
--
-- TERMINAL STATE: `torn_down`, not `failed`. `defined -> torn_down` was already a legal edge
-- ("an abandoned create-without-provision System torn down without first advancing to
-- provisioning") and is the honest description of what happened: no provider work ever started,
-- so there is no host domain, no overlay, and nothing to reap — only a reservation to give back.
-- `failed` would assert an error that never occurred and would show up in failure reporting.
-- `torn_down` is terminal and outside `_NON_TERMINAL_SYSTEM`, so it genuinely releases the quota
-- slot rather than merely renaming the strand.
--
-- THE ALLOCATION IS LEFT TO THE RECONCILER. The row's Allocation stays `active`; this migration
-- does not touch it. `reap_orphaned_active_allocations` is exactly the repair for "an `active`
-- allocation whose only System is terminal or absent" (ADR-0109), it re-checks the predicate under
-- the PROJECT -> ALLOCATION lock, and it writes the release audit trail and the ledger credit that
-- a bare SQL UPDATE here could not. Reverting the allocation in SQL would duplicate that logic
-- outside the lock. Its `DEFAULT_ORPHANED_ACTIVE_GRACE` (2 min, measured from
-- `allocations.updated_at`) means the slot comes back one reconciler pass later, not instantly.
--
-- AUDIT. Each resolved row gets one `audit_log` row so the object's trail stays continuous: an
-- agent reading the trail sees `->defined` followed by `defined->torn_down` rather than a System
-- that silently changed state between deploys. It is written under the system principal
-- convention the reconciler uses (`audit.record_system`, e.g. `system:reconciler`), with `tool`
-- naming this migration since no MCP tool performed it. `args_digest` is the digest of the empty
-- argument map — the same value `args_digest({})` produces — because the migration took no
-- arguments; it is computed here rather than pasted as a hex literal so the tie to that
-- definition is visible.
INSERT INTO audit_log (principal, agent_session, project, tool, object_kind, object_id,
                       transition, args_digest)
SELECT 'system:migration',
       NULL,
       s.project,
       'migration:0080_retire_defined_system_state',
       'systems',
       s.id,
       'defined->torn_down',
       encode(sha256('{}'::bytea), 'hex')
FROM systems s
WHERE s.state = 'defined';

-- `updated_at` is trigger-maintained, so the row's changed-at moves with the state.
UPDATE systems SET state = 'torn_down' WHERE state = 'defined';

-- With every row resolved, drop `defined` from the admitted set so the DB rejects a value the
-- code can no longer read. Drop-and-recreate keeps the constraint name stable for the SQL<->enum
-- tie in test_migrate.py (mirrors 0044's removal of `failed` from component_uploads_state_check).
ALTER TABLE systems DROP CONSTRAINT systems_state_check;
ALTER TABLE systems ADD CONSTRAINT systems_state_check
    CHECK (state IN ('provisioning', 'ready', 'reprovisioning',
                     'restoring', 'paused', 'crashing', 'crashed', 'torn_down', 'failed'));
