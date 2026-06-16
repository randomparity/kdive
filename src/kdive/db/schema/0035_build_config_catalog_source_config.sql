-- Declarative [[build_config]] home in systems.toml (ADR-0122): a third provenance value.
-- 'config' = published by the systems.toml reconcile (file-authoritative, beats 'operator').
-- Drop and re-add the CHECK so 'config' is accepted; no new column. The reconcile pass writes
-- 'config'; the seed's WHERE source='seed' guard already refuses any non-seed row, so it
-- refuses 'config' too.
ALTER TABLE build_config_catalog DROP CONSTRAINT build_config_catalog_source_check;
ALTER TABLE build_config_catalog
    ADD CONSTRAINT build_config_catalog_source_check
        CHECK (source IN ('seed', 'operator', 'config'));
