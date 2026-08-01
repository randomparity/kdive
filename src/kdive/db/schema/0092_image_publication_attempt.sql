-- 0092_image_publication_attempt.sql — nullable expand phase for attempt-aware publication
-- recovery (ADR-0525, #1789). Legacy/predecessor rows remain NULL until the contract phase.
ALTER TABLE image_catalog
    ADD COLUMN publication_attempt_id uuid,
    ADD COLUMN publication_principal text;

CREATE FUNCTION image_catalog_publication_compat_update() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.state = 'pending' AND OLD.publication_attempt_id IS NOT NULL
       AND NEW.publication_attempt_id IS NOT DISTINCT FROM OLD.publication_attempt_id THEN
        IF NEW.state <> 'pending' THEN
            RAISE EXCEPTION 'stale predecessor cannot finish publication attempt %',
                OLD.publication_attempt_id
                USING ERRCODE = '55000';
        END IF;
        IF ROW(NEW.object_key, NEW.kernel_config_key, NEW.volume, NEW.path, NEW.digest,
               NEW.provenance, NEW.provenance_attested, NEW.size_bytes)
           IS DISTINCT FROM
           ROW(OLD.object_key, OLD.kernel_config_key, OLD.volume, OLD.path, OLD.digest,
               OLD.provenance, OLD.provenance_attested, OLD.size_bytes) THEN
            NEW.publication_attempt_id := NULL;
            NEW.publication_principal := NULL;
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER image_catalog_publication_compat_update
    BEFORE UPDATE ON image_catalog
    FOR EACH ROW EXECUTE FUNCTION image_catalog_publication_compat_update();

CREATE FUNCTION image_catalog_publication_compat_delete() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.state = 'pending' AND OLD.publication_attempt_id IS NOT NULL THEN
        RETURN NULL;
    END IF;
    RETURN OLD;
END;
$$;

CREATE TRIGGER image_catalog_publication_compat_delete
    BEFORE DELETE ON image_catalog
    FOR EACH ROW EXECUTE FUNCTION image_catalog_publication_compat_delete();
