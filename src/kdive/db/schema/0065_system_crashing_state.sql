-- 0065_system_crashing_state.sql — add the transient `crashing` System state (ADR-0325, #1078).
-- Forward-only (ADR-0015), additive: widens the CHECK to allow 'crashing', the pre-NMI marker
-- force_crash sets before firing the physical NMI so the power path's non-READY guard refuses it.
-- No existing row is 'crashing', so there is no data backfill.
ALTER TABLE systems DROP CONSTRAINT systems_state_check;
ALTER TABLE systems ADD CONSTRAINT systems_state_check
    CHECK (state IN ('defined', 'provisioning', 'ready', 'reprovisioning',
                     'crashing', 'crashed', 'torn_down', 'failed'));
