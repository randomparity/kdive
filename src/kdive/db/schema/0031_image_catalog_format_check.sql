-- 0031_image_catalog_format_check.sql — close the qcow2-only image format contract.
-- Runtime image publishing, image build jobs, and fixture/catalog contracts only support
-- qcow2; keep persisted catalog rows on the same closed value set.
ALTER TABLE image_catalog ADD CONSTRAINT image_catalog_format_check CHECK (format = 'qcow2');
