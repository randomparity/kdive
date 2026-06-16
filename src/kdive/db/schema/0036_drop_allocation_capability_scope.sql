-- 0036: Drop the structurally-dead allocations.capability_scope column (ADR-0130, #465).
-- The destructive-op gate no longer reads it; admission always wrote '{}'. The grant layer
-- is replaced by the role + profile-opt-in two-check gate, not deprecated.
ALTER TABLE allocations DROP COLUMN capability_scope;
