-- Bound durable recovery evidence and retain the operator's bounded justification.
ALTER TABLE investigation_build_use_recoveries
    ADD COLUMN reason text NOT NULL DEFAULT 'legacy recovery';

ALTER TABLE investigation_build_use_recoveries
    ADD CONSTRAINT investigation_build_use_recoveries_holder_bounded
        CHECK (octet_length(holder_worker_id) <= 512),
    ADD CONSTRAINT investigation_build_use_recoveries_actor_bounded
        CHECK (octet_length(recovered_by) <= 255),
    ADD CONSTRAINT investigation_build_use_recoveries_evidence_bounded
        CHECK (octet_length(evidence) <= 1024),
    ADD CONSTRAINT investigation_build_use_recoveries_reason_nonempty
        CHECK (length(btrim(reason)) > 0),
    ADD CONSTRAINT investigation_build_use_recoveries_reason_bounded
        CHECK (octet_length(reason) <= 512);
