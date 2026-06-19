-- 0045_allocation_requested_pool.sql — pool selection axis (ADR-0186, #561). Additive,
-- forward-only (ADR-0015). A queued by-pool allocations.request persists its target pool here
-- so the FIFO promotion sweep (ADR-0069) can re-resolve candidates; mirrors requested_kind
-- (0016). NULL for by-id / by-kind requests. The "exactly one target selector" invariant among
-- requested_resource_id / requested_kind / requested_pool is enforced in the service layer (as
-- 0016 did for requested_kind), not a SQL XOR CHECK.
ALTER TABLE allocations
    ADD COLUMN requested_pool text;
