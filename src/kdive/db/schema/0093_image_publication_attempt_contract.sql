-- 0093_image_publication_attempt_contract.sql — contract publication attempt state
-- after phase-two coexistence (ADR-0525, #1790).
DROP TRIGGER image_catalog_publication_compat_update ON image_catalog;
DROP TRIGGER image_catalog_publication_compat_delete ON image_catalog;
DROP FUNCTION image_catalog_publication_compat_update();
DROP FUNCTION image_catalog_publication_compat_delete();

UPDATE image_catalog
SET publication_attempt_id = gen_random_uuid()
WHERE state = 'pending' AND publication_attempt_id IS NULL;

CREATE FUNCTION image_catalog_phase_two_recovery_disarm() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.state = 'pending'
       AND OLD.publication_attempt_id IS NOT NULL
       AND NEW.state = 'pending'
       AND NEW.publication_attempt_id IS NULL
       AND NEW.publication_principal IS NULL THEN
        DELETE FROM image_catalog WHERE id = OLD.id;
        RETURN NULL;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER image_catalog_phase_two_recovery_disarm
    BEFORE UPDATE ON image_catalog
    FOR EACH ROW EXECUTE FUNCTION image_catalog_phase_two_recovery_disarm();

ALTER TABLE image_catalog
    ADD CONSTRAINT image_catalog_publication_attempt_check CHECK (
        ((state = 'pending') = (publication_attempt_id IS NOT NULL))
        AND (
            publication_principal IS NULL
            OR (state = 'pending' AND visibility = 'private')
        )
    );
