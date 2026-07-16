-- 0070_system_resolved_cpu.sql — record the resolved guest CPU baseline on the System (ADR-0368).
--
-- Surface 2 of #980. Resolved at System mint from the bound Resource's advertised `host_cpu`
-- capability (the same mint-time mechanism ADR-0339 `accel` uses), so `systems.get` reports the
-- CPU baseline a System was minted against as a cheap row read (no live libvirt call).
--
-- Nullable with no default: NULL means "no CPU baseline recorded" — a pre-migration System, a
-- local-libvirt/fault-inject System, or a remote host that advertises no `host_cpu` (not
-- re-registered since this feature shipped). Consumers treat NULL as unknown, never crash.
ALTER TABLE systems
    ADD COLUMN resolved_cpu jsonb;
