-- ADR-0135: free-form, agent-settable description for Investigation reporting.
ALTER TABLE investigations
    ADD COLUMN description text
        CONSTRAINT investigations_description_len
        CHECK (description IS NULL OR char_length(description) <= 4096);
