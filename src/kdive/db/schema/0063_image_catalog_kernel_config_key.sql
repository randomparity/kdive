-- 0063_image_catalog_kernel_config_key.sql — image kernel-config offer (ADR-0317, #1051).
-- Additive, forward-only (ADR-0015). Object-store key of the image's extracted
-- /boot/config-<ver>, a sibling object of the qcow2. NULL when no config was captured
-- (a staged path/volume image, a pre-feature row, or a best-effort config-write failure).
-- Independent of the object_key/volume/path exactly-one invariant: not part of that CHECK.

ALTER TABLE image_catalog ADD COLUMN kernel_config_key text;
