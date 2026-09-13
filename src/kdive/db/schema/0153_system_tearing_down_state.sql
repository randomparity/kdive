-- Add the retryable ordinary-teardown admission fence (#2370).
ALTER TABLE systems DROP CONSTRAINT systems_state_check;
ALTER TABLE systems ADD CONSTRAINT systems_state_check
    CHECK (state IN ('provisioning', 'ready', 'reprovisioning', 'restoring', 'paused',
                     'crashing', 'crashed', 'tearing_down', 'torn_down', 'failed'));
