-- Permanent exact-worker termination evidence and fence credentials (ADR-0533, #1803).
CREATE TABLE worker_incarnations (
    incarnation text PRIMARY KEY,
    authority_kind text NOT NULL,
    authority_binding jsonb NOT NULL,
    fence_protocol integer NOT NULL CHECK (fence_protocol > 0),
    credential_hash bytea NOT NULL UNIQUE CHECK (octet_length(credential_hash) = 32),
    state text NOT NULL DEFAULT 'active',
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    terminated_at timestamptz,
    outcome text,
    CONSTRAINT worker_incarnations_incarnation_bounded
        CHECK (length(incarnation) BETWEEN 1 AND 512),
    CONSTRAINT worker_incarnations_authority_kind_bounded
        CHECK (authority_kind IN ('local', 'docker', 'kubernetes')),
    CONSTRAINT worker_incarnations_binding_object
        CHECK (jsonb_typeof(authority_binding) = 'object'),
    CONSTRAINT worker_incarnations_state_bounded
        CHECK (state IN ('active', 'terminated')),
    CONSTRAINT worker_incarnations_outcome_bounded
        CHECK (outcome IS NULL OR outcome IN ('succeeded', 'failed', 'killed')),
    CONSTRAINT worker_incarnations_terminal_shape
        CHECK ((state = 'active' AND terminated_at IS NULL AND outcome IS NULL)
            OR (state = 'terminated' AND terminated_at IS NOT NULL AND outcome IS NOT NULL))
);

ALTER TABLE investigation_build_use_recoveries
    ADD COLUMN authority_kind text NOT NULL,
    ADD COLUMN authority_binding jsonb NOT NULL,
    ADD COLUMN termination_outcome text NOT NULL,
    ADD COLUMN terminated_at timestamptz NOT NULL,
    ADD CONSTRAINT investigation_build_use_recoveries_authority_kind_bounded
        CHECK (authority_kind IN ('local', 'docker', 'kubernetes')),
    ADD CONSTRAINT investigation_build_use_recoveries_binding_object
        CHECK (jsonb_typeof(authority_binding) = 'object'),
    ADD CONSTRAINT investigation_build_use_recoveries_outcome_bounded
        CHECK (termination_outcome IN ('succeeded', 'failed', 'killed')),
    ADD CONSTRAINT investigation_build_use_recoveries_incarnation_fkey
        FOREIGN KEY (holder_worker_id) REFERENCES worker_incarnations (incarnation);
