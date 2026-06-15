-- 0033_allocation_failure_category.sql — record the terminal cause of a failed
-- allocation so a waiting agent (allocations.wait, #430 / ADR-0118) can tell a
-- queue_timeout (retryable) from a budget terminate (allocation_denied, terminal).
-- Additive, forward-only (ADR-0015). NULL for every existing failed row and for any
-- failed path that does not yet set it; the response envelope falls back to
-- infrastructure_failure when NULL.
ALTER TABLE allocations
    ADD COLUMN failure_category text;
