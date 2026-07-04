-- 0060_image_description.sql — operator-attested image description (ADR-0311, #1017).
-- Additive nullable column reconciled from the inventory [[image]].description hint.
-- Advisory operator context surfaced by images.list/describe; never a capability claim.
ALTER TABLE image_catalog ADD COLUMN description text;
