-- 0095_investigation_builds.sql — immutable external build generations (ADR-0531, #1519).
CREATE TABLE investigation_builds (
    investigation_id uuid NOT NULL REFERENCES investigations(id),
    generation uuid NOT NULL,
    build_ref text NOT NULL,
    content_digest text NOT NULL,
    canonical_document jsonb NOT NULL,
    build_result jsonb NOT NULL,
    artifacts jsonb NOT NULL,
    target_kind text NOT NULL,
    build_profile jsonb NOT NULL,
    state text NOT NULL DEFAULT 'active'
        CONSTRAINT investigation_builds_state_check CHECK (state IN ('active', 'reclaiming')),
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT investigation_builds_pkey PRIMARY KEY (investigation_id, generation),
    CONSTRAINT investigation_builds_investigation_id_build_ref_key UNIQUE (investigation_id, build_ref),
    CONSTRAINT investigation_builds_content_digest_check
        CHECK (content_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT investigation_builds_build_ref_check
        CHECK (build_ref = content_digest || '.' || generation::text)
);
CREATE TRIGGER investigation_builds_set_updated_at BEFORE UPDATE ON investigation_builds
    FOR EACH ROW WHEN (OLD.* IS DISTINCT FROM NEW.*) EXECUTE FUNCTION set_updated_at();

CREATE INDEX investigation_builds_active_digest_idx
    ON investigation_builds (investigation_id, content_digest) WHERE state = 'active';
CREATE INDEX investigation_builds_expires_at_idx ON investigation_builds (expires_at);

ALTER TABLE runs ADD COLUMN build_ref text;
