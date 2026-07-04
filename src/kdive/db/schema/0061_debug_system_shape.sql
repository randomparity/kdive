-- 0061_debug_system_shape.sql — seed the curated 'debug' system shape (#985, ADR-0312).
-- Forward-only (ADR-0015), additive to the 0013 seed. Generous disk for runtime tracer
-- installs (trace-cmd/bpftrace/gcc/headers) alongside build artifacts and a captured vmcore,
-- so a debug System is sized by one name instead of a custom triple. memory_mb is a whole-GB
-- multiple (the 0013 system_shapes_memory_whole_gb_check). Idempotent: ON CONFLICT keeps a
-- re-run a no-op and never re-sizes a preset an operator may have redefined.
INSERT INTO system_shapes (name, vcpus, memory_mb, disk_gb) VALUES
    ('debug', 4, 8192, 60)
ON CONFLICT (name) DO NOTHING;
