-- ADR-0701: both systems.get Run lookups share this equality predicate.
CREATE INDEX runs_system_id_idx ON runs (system_id);
