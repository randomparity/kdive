-- Bound every public build-artifact GC lane with an independent durable cursor (#1519).
CREATE TABLE build_artifact_gc_cursors (
    lane text PRIMARY KEY,
    after_id uuid,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT build_artifact_gc_cursors_lane_check CHECK (
        lane IN (
            'closed-investigations',
            'closed-legacy-artifacts',
            'expired-legacy-artifacts'
        )
    )
);

INSERT INTO build_artifact_gc_cursors (lane) VALUES
    ('closed-investigations'),
    ('closed-legacy-artifacts'),
    ('expired-legacy-artifacts');

CREATE INDEX investigations_build_cleanup_pending_idx
    ON investigations (id)
    WHERE cleanup_pending_at IS NOT NULL;

CREATE INDEX artifacts_legacy_build_gc_idx
    ON artifacts (id)
    WHERE owner_kind = 'runs' AND retention_class IN ('build', 'kernel-build');
