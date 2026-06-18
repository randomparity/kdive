-- 0043_run_steps_state_check.sql — database-enforce the run_steps.state machine
-- (ADR-0171, #562). run_steps.state is the idempotency-ledger state machine with
-- exactly two values (_RunStepState: 'running', 'succeeded'), but 0001_init.sql left
-- the column an unconstrained text. A row with any other value makes claim_run_step
-- poll forever (it returns only on 'succeeded' and treats every other value as a live
-- 'running' claim to wait on). Add the named CHECK the durable lifecycle tables use.
--
-- Validating (not NOT VALID): the only writers (run_step / claim_run_step /
-- complete_run_step) only ever write 'running' or 'succeeded', so validation of
-- existing rows cannot fail. The CHECK mirrors _RunStepState exactly; the named
-- constraint is pinned to the enum by test_migrate.py (CHECK_ENUMS plus a dedicated
-- bidirectional test), which fails if it drifts.
ALTER TABLE run_steps
    ADD CONSTRAINT run_steps_state_check CHECK (state IN ('running', 'succeeded'));
