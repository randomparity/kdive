-- 0067_system_accel.sql — record the resolved accelerator on the System (ADR-0339).
--
-- Systems admission validates a profile arch against the bound Resource's advertised
-- `guest_arches` (ADR-0338) and resolves the accelerator (`kvm`/`tcg`) at System mint. The
-- resolved value is persisted here so downstream timeout scaling, cost accounting, and the
-- domain-XML renderer key off a recorded fact rather than re-deriving host state.
--
-- Nullable with no default: NULL means "no host-derived accelerator was recorded" — a resource
-- that advertises no `guest_arches` (remote-libvirt, fault-inject, a host not re-discovered
-- since ADR-0338) provisions as before and records NULL, and pre-existing rows read back NULL.
ALTER TABLE systems
    ADD COLUMN accel text;
