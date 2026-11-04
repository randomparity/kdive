-- BYO inventory and refusing runtime prerequisites (ADR-0813, #1817).
-- Forward-only; operational lease acquisition/reclaim and adopt-facts writers ship later.
ALTER TABLE resources DROP CONSTRAINT resources_kind_check;
ALTER TABLE resources ADD CONSTRAINT resources_kind_check
    CHECK (kind IN ('local-libvirt', 'fault-inject', 'remote-libvirt', 'byo-host'));

INSERT INTO cost_class_coefficients (cost_class, coeff) VALUES ('byo', 1.0)
    ON CONFLICT (cost_class) DO NOTHING;

ALTER TABLE systems ADD COLUMN byo_adopt_facts jsonb
    CONSTRAINT systems_byo_adopt_facts_object_check
    CHECK (byo_adopt_facts IS NULL OR jsonb_typeof(byo_adopt_facts) = 'object');

CREATE TABLE byo_console_leases (
    id uuid PRIMARY KEY,
    resource_id uuid NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
    mode text NOT NULL CHECK (mode IN ('reading', 'preempting')),
    holder_worker_id text NOT NULL CHECK (octet_length(holder_worker_id) BETWEEN 1 AND 512),
    acquired_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    CHECK (expires_at > acquired_at)
);

-- Shared readers coexist; #1818 must lock the Resource before cross-mode checks/inserts.
CREATE UNIQUE INDEX byo_console_leases_preempting_idx
    ON byo_console_leases(resource_id) WHERE mode = 'preempting';
CREATE INDEX byo_console_leases_resource_idx ON byo_console_leases(resource_id);
CREATE INDEX byo_console_leases_expires_idx ON byo_console_leases(expires_at);
-- No runtime grants: there is no lease reader/writer in the skeleton (ADR-0533).
