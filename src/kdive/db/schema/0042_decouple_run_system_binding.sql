-- ADR-0169: decouple build submission from a provisioned System.
-- A Run may exist before a System (system_id nullable) and records the resource kind
-- it committed to (target_kind), which selects the builder and constrains the System a
-- later runs.bind may attach.

ALTER TABLE runs ALTER COLUMN system_id DROP NOT NULL;
ALTER TABLE runs ADD COLUMN target_kind text;

-- Backfill every existing (necessarily bound) Run from its resource kind. The
-- runs -> systems -> allocations -> resources chain is referentially total (all NOT NULL
-- FKs with the default RESTRICT on-delete), so this populates every row.
UPDATE runs r
   SET target_kind = res.kind
  FROM systems s
  JOIN allocations a ON a.id = s.allocation_id
  JOIN resources res ON res.id = a.resource_id
 WHERE s.id = r.system_id;

-- Defensive: fail loudly with a clear message rather than an opaque NOT NULL violation if a
-- legacy row ever escaped the FK-chain invariant the backfill relies on.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM runs WHERE target_kind IS NULL) THEN
        RAISE EXCEPTION 'migration 0042: % run(s) have an unresolved target_kind backfill',
            (SELECT count(*) FROM runs WHERE target_kind IS NULL);
    END IF;
END $$;

ALTER TABLE runs ALTER COLUMN target_kind SET NOT NULL;
