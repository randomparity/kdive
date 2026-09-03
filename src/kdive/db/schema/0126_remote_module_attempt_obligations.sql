-- 0126_remote_module_attempt_obligations.sql — durable remote-module attempt obligations
-- (ADR-0588, amending ADR-0585).
--
-- One row per module attempt, keyed on the attempt tuple (system_id, run_id, operation_nonce)
-- and never on the volume kind: an obligation is a property of the attempt, and one attempt owns
-- several volumes. The row is written before any of that attempt's volumes are created, so a
-- volume can never exist whose attempt has no row (ADR-0588, "Durable intent precedes the
-- volume"). A row with no volume is the ordinary crash residue and is benign.
--
-- Two obligations, deliberately independent:
--
--   * the mutation obligation covers source.ext4 and scratch.ext4. It opens with the row and
--     discharges at ADR-0585's durable `restored`, at baseline commitment, or on ADR-0585's
--     terminal escape — System teardown, or an operator-acknowledged close of a parked recovery
--     conflict. Without that third reason nothing discharges for a worker killed mid-mutation,
--     and the attempt retains its recovery material forever.
--   * the reap obligation covers reaping.journal and reaped.journal. It opens when the reap
--     sequence starts and discharges when that sequence reaches its own terminal state.
--
-- No constraint orders the two against each other. The journal volumes are created after
-- `restored` in practice, so the mutation obligation is in fact already discharged when the reap
-- obligation opens, but ADR-0588 states that as a property of the calling sequence rather than of
-- the row, and the sweep reads each obligation separately. What *is* a property of the row is
-- that the reap obligation cannot open without the terminal evidence its readers need: the reap
-- marker builder refuses to run without a terminal operation and result.
--
-- The terminal evidence is the payload the discarded `attempt-reap` volume metadata element
-- carried. Libvirt does not persist <metadata> on a storage volume (ADR-0588), so this row is the
-- only source its readers have. The recovery reference is here for the same reason: ADR-0588
-- moved ownership into the volume name, and a 135-byte name cannot carry a nine-field document,
-- so after that decision the reference has no durable home unless this row gives it one.
--
-- The three documents are jsonb, following 0121's `materialization` / `recovery_point` /
-- `terminal_evidence` columns, so this layer never has to know their field sets — a virtue while
-- their serialization is still being settled elsewhere.
--
-- Read this before trusting a readback: **jsonb stores a normalized value, not bytes.** PostgreSQL
-- reorders keys, drops insignificant whitespace, and canonicalizes numbers on ingest, so a
-- document read out of these columns is NOT the byte string that was written. That matters because
-- `RemoteModuleDocument.from_canonical_json` re-serializes what it parsed and rejects anything
-- that differs, byte for byte.
--
-- The round trip is nonetheless exact for these three documents, for reasons that are properties
-- of the documents rather than of jsonb: the canonical form sorts keys and uses compact
-- separators, so neither PostgreSQL's key ordering nor its whitespace can matter; every field is a
-- string, bool, int or nested object, with no float for `numeric` to reformat; and a canonical
-- document carries no nulls, so nothing survives ingest for `exclude_none` to disagree about.
-- Rebuild with `Model.model_validate(readback)` and take `to_canonical_json()` from the result;
-- never assume `json.dumps(readback)` reproduces the original bytes.
--
-- The four identity digests are therefore their own columns, computed over the original bytes at
-- write time and never re-derived from a readback. A digest recomputed from normalized jsonb would
-- be checking the normalization, not the evidence.
-- `test_real_documents_survive_the_jsonb_round_trip_byte_for_byte` holds all of this against the
-- real document classes, so the day the argument above stops being true, it fails.

CREATE TABLE remote_module_attempt_obligations (
    system_id                   uuid NOT NULL REFERENCES systems (id) ON DELETE CASCADE,
    run_id                      uuid NOT NULL,
    operation_nonce             text NOT NULL CONSTRAINT remote_module_attempt_nonce
                                    CHECK (operation_nonce ~ '^[0-9a-f]{32}$'),
    mutation_discharged_at      timestamptz,
    mutation_discharge_reason   text CONSTRAINT remote_module_attempt_mutation_reason
                                    CHECK (mutation_discharge_reason IS NULL
                                           OR mutation_discharge_reason
                                              IN ('restored', 'baseline_committed',
                                                  'terminal_escape')),
    reap_opened_at              timestamptz,
    reap_discharged_at          timestamptz,
    terminal_operation          jsonb,
    terminal_operation_identity text,
    terminal_result             jsonb,
    terminal_result_identity    text,
    baseline_operation_identity text,
    baseline_result_identity    text,
    installed_entry_count       integer,
    installed_content_bytes     bigint,
    recovery_reference          jsonb,
    created_at                  timestamptz NOT NULL DEFAULT now(),
    updated_at                  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT remote_module_attempt_obligations_pkey
        PRIMARY KEY (system_id, run_id, operation_nonce),
    CONSTRAINT remote_module_attempt_run_system_fk
        FOREIGN KEY (run_id, system_id) REFERENCES runs (id, system_id) ON DELETE CASCADE,
    CONSTRAINT remote_module_attempt_mutation_discharge
        CHECK ((mutation_discharged_at IS NULL) = (mutation_discharge_reason IS NULL)),
    CONSTRAINT remote_module_attempt_reap_discharge
        CHECK (reap_discharged_at IS NULL
               OR (reap_opened_at IS NOT NULL AND reap_discharged_at >= reap_opened_at)),
    -- All nine evidence columns are written in one group or not at all: a reader that finds the
    -- terminal operation must find every field it needs beside it.
    CONSTRAINT remote_module_attempt_evidence_group CHECK (
        num_nonnulls(terminal_operation, terminal_operation_identity, terminal_result,
                     terminal_result_identity, baseline_operation_identity,
                     baseline_result_identity, installed_entry_count, installed_content_bytes,
                     recovery_reference) IN (0, 9)
    ),
    CONSTRAINT remote_module_attempt_reap_needs_evidence
        CHECK (reap_opened_at IS NULL OR terminal_operation IS NOT NULL),
    CONSTRAINT remote_module_attempt_evidence_digests CHECK (
        (terminal_operation_identity IS NULL
            OR terminal_operation_identity ~ '^sha256:[0-9a-f]{64}$')
        AND (terminal_result_identity IS NULL
            OR terminal_result_identity ~ '^sha256:[0-9a-f]{64}$')
        AND (baseline_operation_identity IS NULL
            OR baseline_operation_identity ~ '^sha256:[0-9a-f]{64}$')
        AND (baseline_result_identity IS NULL
            OR baseline_result_identity ~ '^sha256:[0-9a-f]{64}$')
    ),
    -- The bounds RemoteModuleRecoveryRefV1 already places on the baseline counts.
    CONSTRAINT remote_module_attempt_installed_counts CHECK (
        (installed_entry_count IS NULL OR installed_entry_count BETWEEN 0 AND 200000)
        AND (installed_content_bytes IS NULL
             OR installed_content_bytes BETWEEN 0 AND 8589934592)
    ),
    CONSTRAINT remote_module_attempt_evidence_schema CHECK (
        (terminal_operation IS NULL OR terminal_operation ->> 'protocol'
            IS NOT DISTINCT FROM 'remote-module-operation-v1')
        AND (terminal_result IS NULL OR terminal_result ->> 'protocol'
            IS NOT DISTINCT FROM 'remote-module-result-v1')
        AND (recovery_reference IS NULL OR recovery_reference ->> 'protocol'
            IS NOT DISTINCT FROM 'remote-module-recovery-ref-v1')
    ),
    -- A document may only be stored against the attempt it names. RemoteModuleResultV1 leaves its
    -- ownership fields optional, so those are checked only when present.
    CONSTRAINT remote_module_attempt_evidence_ownership CHECK (
        (terminal_operation IS NULL OR (
            terminal_operation ->> 'system_id' IS NOT DISTINCT FROM system_id::text
            AND terminal_operation ->> 'run_id' IS NOT DISTINCT FROM run_id::text
            AND terminal_operation ->> 'operation_nonce' IS NOT DISTINCT FROM operation_nonce))
        AND (terminal_result IS NULL OR (
            (terminal_result ->> 'system_id' IS NULL
                OR terminal_result ->> 'system_id' = system_id::text)
            AND (terminal_result ->> 'run_id' IS NULL
                OR terminal_result ->> 'run_id' = run_id::text)
            AND (terminal_result ->> 'operation_nonce' IS NULL
                OR terminal_result ->> 'operation_nonce' = operation_nonce)))
        AND (recovery_reference IS NULL OR (
            recovery_reference ->> 'system_id' IS NOT DISTINCT FROM system_id::text
            AND recovery_reference ->> 'run_id' IS NOT DISTINCT FROM run_id::text
            AND recovery_reference ->> 'operation_nonce' IS NOT DISTINCT FROM operation_nonce))
    ),
    -- The recovery reference carries the same two terminal digests the readers verify the
    -- payloads against; a row where they disagree has no single answer.
    CONSTRAINT remote_module_attempt_evidence_identity CHECK (
        recovery_reference IS NULL
        OR (recovery_reference ->> 'operation_identity'
                IS NOT DISTINCT FROM terminal_operation_identity
            AND recovery_reference ->> 'result_identity'
                IS NOT DISTINCT FROM terminal_result_identity)
    ),
    CONSTRAINT remote_module_attempt_evidence_size CHECK (
        (terminal_operation IS NULL OR pg_column_size(terminal_operation) <= 65536)
        AND (terminal_result IS NULL OR pg_column_size(terminal_result) <= 65536)
        AND (recovery_reference IS NULL OR pg_column_size(recovery_reference) <= 65536)
    )
);

-- The sweep reads only the un-discharged rows, and reads them on every tick for the life of the
-- deployment, so the discharged majority is kept out of the scan.
CREATE INDEX remote_module_attempt_obligations_retained
    ON remote_module_attempt_obligations (system_id, run_id, operation_nonce)
    WHERE mutation_discharged_at IS NULL
       OR (reap_opened_at IS NOT NULL AND reap_discharged_at IS NULL);

CREATE TRIGGER remote_module_attempt_obligations_set_updated_at
    BEFORE UPDATE ON remote_module_attempt_obligations
    FOR EACH ROW WHEN (OLD.* IS DISTINCT FROM NEW.*) EXECUTE FUNCTION set_updated_at();

-- Every field here is evidence about something that has already happened, so each is write-once.
-- Re-writing an identical value is the ordinary crash-resume replay and is allowed; changing one
-- is not, because the deletion decision the sweep makes from it is irreversible.
CREATE FUNCTION reject_remote_module_attempt_rewrite() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.system_id, NEW.run_id, NEW.operation_nonce)
       IS DISTINCT FROM (OLD.system_id, OLD.run_id, OLD.operation_nonce) THEN
        RAISE EXCEPTION 'remote module attempt identity is immutable';
    END IF;
    IF OLD.mutation_discharged_at IS NOT NULL
       AND (NEW.mutation_discharged_at, NEW.mutation_discharge_reason)
           IS DISTINCT FROM (OLD.mutation_discharged_at, OLD.mutation_discharge_reason) THEN
        RAISE EXCEPTION 'discharged mutation obligation is immutable';
    END IF;
    IF OLD.reap_opened_at IS NOT NULL
       AND NEW.reap_opened_at IS DISTINCT FROM OLD.reap_opened_at THEN
        RAISE EXCEPTION 'opened reap obligation is immutable';
    END IF;
    IF OLD.reap_discharged_at IS NOT NULL
       AND NEW.reap_discharged_at IS DISTINCT FROM OLD.reap_discharged_at THEN
        RAISE EXCEPTION 'discharged reap obligation is immutable';
    END IF;
    IF OLD.terminal_operation IS NOT NULL
       AND (NEW.terminal_operation, NEW.terminal_operation_identity, NEW.terminal_result,
            NEW.terminal_result_identity, NEW.baseline_operation_identity,
            NEW.baseline_result_identity, NEW.installed_entry_count,
            NEW.installed_content_bytes, NEW.recovery_reference)
           IS DISTINCT FROM
           (OLD.terminal_operation, OLD.terminal_operation_identity, OLD.terminal_result,
            OLD.terminal_result_identity, OLD.baseline_operation_identity,
            OLD.baseline_result_identity, OLD.installed_entry_count,
            OLD.installed_content_bytes, OLD.recovery_reference) THEN
        RAISE EXCEPTION 'remote module attempt terminal evidence is immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER remote_module_attempt_obligations_write_once
    BEFORE UPDATE ON remote_module_attempt_obligations
    FOR EACH ROW EXECUTE FUNCTION reject_remote_module_attempt_rewrite();

-- A new function is EXECUTE-to-PUBLIC by default, and the authority host's readiness check counts
-- any such grant outside its allowlist as excess privilege. A trigger function needs no EXECUTE
-- grant to fire — the privilege is checked when the trigger is created, not when it runs — so the
-- grant is removed rather than the allowlist widened.
REVOKE ALL ON FUNCTION reject_remote_module_attempt_rewrite() FROM PUBLIC;

GRANT SELECT, INSERT, UPDATE ON TABLE remote_module_attempt_obligations TO kdive_server;
REVOKE ALL ON TABLE remote_module_attempt_obligations FROM kdive_worker, kdive_reconciler;
GRANT SELECT ON TABLE remote_module_attempt_obligations TO kdive_worker, kdive_reconciler;
