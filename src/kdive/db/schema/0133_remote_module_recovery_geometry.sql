-- Permit recovery-ref-v2 and require its authenticated source-volume geometry.
ALTER TABLE remote_module_attempt_obligations
    DROP CONSTRAINT remote_module_attempt_evidence_schema;

ALTER TABLE remote_module_attempt_obligations
    ADD CONSTRAINT remote_module_attempt_evidence_schema CHECK (
        (terminal_operation IS NULL OR terminal_operation ->> 'protocol'
            IS NOT DISTINCT FROM 'remote-module-operation-v1')
        AND (terminal_result IS NULL OR terminal_result ->> 'protocol'
            IS NOT DISTINCT FROM 'remote-module-result-v1')
        AND (recovery_reference IS NULL OR recovery_reference ->> 'protocol'
            IN ('remote-module-recovery-ref-v1', 'remote-module-recovery-ref-v2'))
    ),
    ADD CONSTRAINT remote_module_attempt_recovery_geometry CHECK (
        recovery_reference IS NULL
        OR recovery_reference ->> 'protocol' = 'remote-module-recovery-ref-v1'
        OR CASE
            WHEN jsonb_typeof(recovery_reference -> 'source_capacity_bytes') = 'number'
            THEN (recovery_reference ->> 'source_capacity_bytes')::numeric
                BETWEEN 4096 AND 10499653632
                AND mod((recovery_reference ->> 'source_capacity_bytes')::numeric, 4096) = 0
            ELSE FALSE
        END
    );
