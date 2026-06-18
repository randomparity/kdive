-- 0044_component_upload_state_check.sql — narrow component_uploads.state to the
-- two values its lifecycle owns (ADR-0172, #563). 0009_provider_components.sql
-- admitted ('pending', 'finalized', 'failed'), but ComponentUploadState defines
-- only PENDING and FINALIZED: create_component_upload_intent writes 'pending' and
-- finalize_component_upload writes 'finalized'. No code path ever wrote 'failed';
-- it existed only at the schema/test level, owned by no service code.
--
-- Removing a value from an existing constraint requires a drop+recreate (a plain
-- ADD CONSTRAINT cannot narrow it). Validation of existing rows cannot fail because
-- no writer ever produced 'failed'. The recreated CHECK mirrors ComponentUploadState
-- exactly; test_migrate.py pins it to the enum (CHECK_ENUMS plus a dedicated
-- bidirectional test), which fails in either direction if the two drift.
ALTER TABLE component_uploads
    DROP CONSTRAINT component_uploads_state_check;
ALTER TABLE component_uploads
    ADD CONSTRAINT component_uploads_state_check CHECK (state IN ('pending', 'finalized'));
