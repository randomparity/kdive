-- Immutable System-time snapshot of mechanically verified root provenance (ADR-0583, #2106).
CREATE TABLE system_root_provenance (
    system_id       uuid PRIMARY KEY REFERENCES systems (id) ON DELETE CASCADE,
    source_image_id uuid NOT NULL,
    project         text NOT NULL,
    architecture    text NOT NULL CONSTRAINT system_root_provenance_arch_check
                         CHECK (architecture IN ('x86_64', 'ppc64le')),
    image_digest    text NOT NULL CONSTRAINT system_root_provenance_digest_check
                         CHECK (image_digest ~ '^sha256:[0-9a-f]{64}$'),
    root_spec       jsonb NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE FUNCTION reject_system_root_provenance_update() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'system_root_provenance is immutable';
END;
$$;

CREATE TRIGGER system_root_provenance_immutable
    BEFORE UPDATE ON system_root_provenance
    FOR EACH ROW EXECUTE FUNCTION reject_system_root_provenance_update();

-- Admission runs as the server. Other runtime roles receive no direct snapshot authority.
GRANT SELECT, INSERT ON TABLE public.system_root_provenance TO kdive_server;
