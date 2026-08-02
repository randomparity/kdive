-- Bound and back off Investigation-build generation reclamation (ADR-0531, #1519).
ALTER TABLE investigation_builds ADD COLUMN reclaim_retry_at timestamptz;

CREATE TABLE investigation_build_gc_cursor (
    lane text PRIMARY KEY CHECK (lane IN ('expired', 'closed')),
    investigation_id uuid,
    generation uuid
);

INSERT INTO investigation_build_gc_cursor (lane) VALUES ('expired'), ('closed');
