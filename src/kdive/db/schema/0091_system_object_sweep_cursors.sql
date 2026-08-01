-- 0091_system_object_sweep_cursors.sql — persist fair scan positions for the
-- two rowless System-object cleanup lanes (ADR-0524, #1751).
--
-- Object versions remain the durable deletion worklist. These two rows store
-- only the last fully considered key, allowing each bounded pass to move past
-- ineligible or temporarily fenced histories and later wrap to retry survivors.
CREATE TABLE system_object_sweep_cursors (
    lane text PRIMARY KEY CHECK (lane IN ('local', 'remote')),
    after_key text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO system_object_sweep_cursors (lane) VALUES ('local'), ('remote');
