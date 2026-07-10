-- 0064_image_provenance_attested.sql — mark operator-attested image provenance (ADR-0323, #1065).
-- Forward-only (ADR-0015), additive. Build/publish/sidecar-verified provenance keeps the default
-- false; the reconciler sets true when it synthesizes an s3 image's operator-declared
-- [image.attested] operands into provenance, so images.describe's capability_signals can label a
-- present operand operator_attested vs build_verified (the ADR-0286 honesty invariant).
ALTER TABLE image_catalog ADD COLUMN provenance_attested boolean NOT NULL DEFAULT false;
