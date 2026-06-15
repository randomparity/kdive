-- 0032_remote_cost_class_coefficient.sql — price the 'remote' cost class (ADR-0007 §1).
-- Additive to 0002 (forward-only, ADR-0015). The shipped systems.toml registers remote-libvirt
-- hosts with cost_class = 'remote', but 0002 only seeded 'local'. Admission resolves the
-- coefficient fail-closed (configuration_error if absent), so every remote allocation was denied
-- the moment it cleared the size-ceiling check. Seed a baseline coefficient so remote hosts are
-- grantable out of the box; operators retune it with ops.set_cost_class_coeff. ON CONFLICT keeps
-- this idempotent and never clobbers an operator-set value.
INSERT INTO cost_class_coefficients (cost_class, coeff) VALUES ('remote', 1.0)
    ON CONFLICT (cost_class) DO NOTHING;
