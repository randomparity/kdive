-- 0066_job_dispatch_lane.sql — explicit queue dispatch lane for worker claims.
--
-- Existing deployments run one generic worker pool, so historical and newly-admitted jobs start
-- in the `default` lane. Worker processes can opt into a narrower lane set at claim time without
-- claiming work meant for a different provider/resource pool.
ALTER TABLE jobs
    ADD COLUMN dispatch_lane text NOT NULL DEFAULT 'default';

ALTER TABLE jobs
    ADD CONSTRAINT jobs_dispatch_lane_nonempty CHECK (dispatch_lane <> '');
