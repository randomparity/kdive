-- 0068_allocation_requested_arch.sql — record the requested guest arch on the Allocation (ADR-0362).
--
-- The allocation request may name a guest architecture (ADR-0362). It drives two things that must
-- survive a queued (`on_capacity=queue`) request across the promotion sweep: architecture-aware
-- placement (route the request to a host that can boot the arch) and accelerator-differentiated
-- reserve pricing (a TCG-emulated guest reserves above a native KVM one). Persisting it here lets
-- the promotion sweep re-resolve placement and re-price the reserve identically to the synchronous
-- admit path, rather than re-deriving it from a request that no longer exists.
--
-- Nullable with no default: NULL means "architecture-blind request" — the pre-ADR-0362 behavior,
-- priced at the native baseline and placed without an arch filter. Pre-existing rows read back NULL.
ALTER TABLE allocations
    ADD COLUMN requested_arch text;
