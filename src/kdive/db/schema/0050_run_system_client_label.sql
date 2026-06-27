-- 0050_run_system_client_label.sql — optional client-supplied label (ADR-0264, #867).
-- Additive, forward-only (ADR-0015). Nullable; existing rows read as NULL ("no label").
-- Length/character validation is the service-layer validate_label job, so the column is a
-- plain nullable text with no CHECK.
ALTER TABLE runs ADD COLUMN label text;
ALTER TABLE systems ADD COLUMN label text;
