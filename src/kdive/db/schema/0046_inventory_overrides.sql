-- 0046_inventory_overrides.sql — the per-identity inventory-override ledger (ADR-0199, #638).
-- Additive, forward-only (ADR-0015). Shifts the inventory reconcile model from
-- drift-repair-from-file (ADR-0021/0112) to seed-once-then-DB-authoritative: systems.toml still
-- repairs drift for every identity with NO row here, while a runtime mutation of a config-declared
-- identity writes a row that makes the mutation win over the file.
--
-- The PK matches the real inventory identity. `resources` is unique on (kind, name), and both
-- 'remote-libvirt' and 'fault-inject' are config-owned there, so a name can legitimately repeat
-- across kinds — hence `resource_kind` is part of the key. Build-host names are globally unique,
-- so a build-host override uses the fixed sentinel resource_kind = 'build-host'.
--
-- `disposition` is CHECK-constrained to the closed set mirrored by
-- InventoryOverrideDisposition (kdive.inventory.overrides); test_migrate.py pins the two together
-- (CHECK_ENUMS plus a bidirectional test). `detached` = "runtime owns the live row; ignore the
-- file's values"; `removed` = "suppress this identity; do not re-create it".
CREATE TABLE inventory_overrides (
    source_kind   text NOT NULL,
    resource_kind text NOT NULL,
    name          text NOT NULL,
    disposition   text NOT NULL
        CONSTRAINT inventory_overrides_disposition_check
        CHECK (disposition IN ('detached', 'removed')),
    reason        text NOT NULL,
    actor         text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_kind, resource_kind, name)
);
