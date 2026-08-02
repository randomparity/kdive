-- Preserve same-Investigation expiry recovery after reusable-build reclamation (ADR-0531, #1519).
CREATE TABLE investigation_build_tombstones (
    investigation_id uuid NOT NULL REFERENCES investigations(id) ON DELETE CASCADE,
    build_ref text NOT NULL,
    expires_at timestamptz NOT NULL,
    reclaimed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (investigation_id, build_ref)
);
