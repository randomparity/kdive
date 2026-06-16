-- Operator write-path for build-config fragments (ADR-0119): provenance for the
-- build_config_catalog row. 'seed' = published by the packaged deploy-time seed (the
-- default, and the value existing rows backfill to); 'operator' = published by an admin
-- via buildconfig.set. The seed's source-guarded upsert refuses to overwrite an
-- 'operator' row, so a later migrate never clobbers an operator override.
ALTER TABLE build_config_catalog
    ADD COLUMN source text NOT NULL DEFAULT 'seed'
        CHECK (source IN ('seed', 'operator'));
