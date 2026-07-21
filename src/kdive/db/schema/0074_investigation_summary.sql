-- 0074_investigation_summary.sql — terminal close-time summary for an Investigation (ADR-0416, #1349).
-- Additive, forward-only (ADR-0015). `summary` is a distinct field from the anytime-editable
-- `description`: it records the agent's account of the work at the moment the investigation is
-- driven to `closed`, captured on the close transition. NULL for open/active rows and for the
-- historical closed rows that predate this migration (no summary was ever collected for them).
ALTER TABLE investigations
    ADD COLUMN summary text
        CONSTRAINT investigations_summary_len
        CHECK (summary IS NULL OR char_length(summary) <= 4096);
