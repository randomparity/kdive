-- 0047_image_catalog_staged_path.sql — local-libvirt staged-path image source (ADR-0228, #732).
-- Additive, forward-only (ADR-0015). A registered local-libvirt rootfs may be an operator-staged
-- host file under the provider allowed_roots, carried as `path` instead of an S3 `object_key` or a
-- storage-pool `volume`. Reworks image_object_present from the 2-way object_key/volume exactly-one
-- (migration 0030) to a 3-way object_key/volume/path exactly-one for non-'defined' rows; a
-- 'defined' row still carries none of the three. Existing rows satisfy the new CHECK unchanged
-- (path defaults NULL): a registered s3 row has object_key only, a registered staged row has
-- volume only, a defined row has none.

ALTER TABLE image_catalog ADD COLUMN path text;

ALTER TABLE image_catalog DROP CONSTRAINT image_object_present;
ALTER TABLE image_catalog ADD CONSTRAINT image_object_present CHECK (
    (state = 'defined' AND object_key IS NULL AND volume IS NULL AND path IS NULL)
    OR (
        state <> 'defined'
        AND (
            (object_key IS NOT NULL)::int + (volume IS NOT NULL)::int + (path IS NOT NULL)::int = 1
        )
    )
);
