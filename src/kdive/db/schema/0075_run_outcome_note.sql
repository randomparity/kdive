-- 0075_run_outcome_note.sql — optional post-hoc outcome note on a Run (ADR-0415, #1386).
-- Additive, forward-only (ADR-0015). `outcome_note` is a distinct field from the write-once
-- `label` (the create-time client handle): it is a free-form, anytime-editable account of the
-- Run's outcome ("UBSAN reproduced, not a panic"), set/updated via runs.set at any time after
-- creation — including on a terminal Run — never fixed at create. NULL until an agent records
-- one, and for the historical rows that predate this migration.
ALTER TABLE runs
    ADD COLUMN outcome_note text
        CONSTRAINT runs_outcome_note_len
        CHECK (outcome_note IS NULL OR char_length(outcome_note) <= 4096);
