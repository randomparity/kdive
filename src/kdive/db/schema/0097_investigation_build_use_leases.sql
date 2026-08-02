-- Crash-recoverable reusable-build fences follow the worker job lease heartbeat.
ALTER TABLE investigation_build_uses
    ADD COLUMN holder_worker_id text,
    ADD COLUMN lease_expires_at timestamptz;

UPDATE investigation_build_uses u
SET holder_worker_id = j.worker_id,
    lease_expires_at = j.lease_expires_at
FROM jobs j
WHERE j.id = u.job_id
  AND j.state = 'running'
  AND j.attempt = u.attempt
  AND j.worker_id IS NOT NULL
  AND j.lease_expires_at IS NOT NULL;

DELETE FROM investigation_build_uses
WHERE holder_worker_id IS NULL OR lease_expires_at IS NULL;

ALTER TABLE investigation_build_uses
    ALTER COLUMN holder_worker_id SET NOT NULL,
    ALTER COLUMN lease_expires_at SET NOT NULL,
    DROP CONSTRAINT investigation_build_uses_job_attempt_key;

CREATE INDEX investigation_build_uses_live_generation_idx
    ON investigation_build_uses (investigation_id, generation, lease_expires_at);
