-- 0195_stranded_activation_observation.sql — #3574, ADR-0816 (2026-11-05).
-- Forward-only (ADR-0015). The observation job and the authority's own read of its target.
ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check
    CHECK (kind IN ('provision', 'reprovision', 'teardown', 'build', 'install',
                    'boot', 'force_crash', 'power', 'capture_vmcore', 'image_build',
                    'diagnostics_worker_check', 'build_install_boot', 'authorize_ssh_key',
                    'console_rotate', 'diagnostic_sysrq', 'check_ssh_reachable',
                    'watch_for_crash', 'snapshot', 'restore', 'delete_snapshot',
                    'capture_traffic', 'reclaim_investigation_rootfs',
                    'remote_module_volume_reap', 'resolve_recovery_orphan',
                    'observe_stranded_activation'));

CREATE FUNCTION public.read_stranded_activation_target(
    p_activation_id uuid, p_authority_instance text
) RETURNS TABLE (
    system_id uuid, run_id uuid, domain_name text, object_refs text[],
    journal_sequence bigint, journal_digest text
) LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_activation public.external_boot_activations%ROWTYPE;
    v_domain text;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_provider_authority', 'member') THEN
        RAISE EXCEPTION 'provider authority is required' USING ERRCODE = '42501';
    END IF;
    SELECT * INTO v_activation FROM public.external_boot_activations WHERE id = p_activation_id;
    IF NOT FOUND OR v_activation.state <> 'preparing' THEN RETURN; END IF;
    IF NOT EXISTS (SELECT 1 FROM public.external_boot_authorities AS authority
                   WHERE authority.activation_id = p_activation_id
                     AND authority.authority_instance = p_authority_instance)
       AND NOT EXISTS (SELECT 1 FROM public.jobs AS job
                       WHERE job.kind = 'boot'
                         AND job.payload #>> '{external_boot_authority_v1,activation_id}'
                             = p_activation_id::text
                         AND job.payload #>> '{external_boot_authority_v1,authority_instance}'
                             = p_authority_instance) THEN
        RETURN;
    END IF;
    IF EXISTS (SELECT 1 FROM public.authority_system_ownership AS owner
               WHERE owner.system_id = v_activation.system_id) THEN
        RETURN;
    END IF;
    SELECT system.domain_name INTO v_domain FROM public.systems AS system
    WHERE system.id = v_activation.system_id;
    IF v_domain IS NULL THEN RETURN; END IF;
    RETURN QUERY
    SELECT v_activation.system_id, v_activation.run_id, v_domain,
           ARRAY(SELECT ref FROM unnest(ARRAY[
               v_activation.materialization #>> '{artifacts,kernel,ref}',
               v_activation.materialization #>> '{artifacts,modules,ref}',
               v_activation.materialization #>> '{artifacts,initrd,ref}',
               v_activation.materialization #>> '{artifacts,debuginfo,ref}',
               v_activation.recovery_point #>> '{recovery_ref,ref}',
               v_activation.pre_recovery_evidence #>> '{recovery_object,ref}'
           ]) AS ref WHERE ref IS NOT NULL),
           head.sequence, head.digest
    FROM (SELECT 1) AS one
    LEFT JOIN public.external_boot_authority_journal_heads AS head
      ON head.authority_instance = p_authority_instance
     AND head.system_id = v_activation.system_id;
END $$;

REVOKE ALL ON FUNCTION public.read_stranded_activation_target(uuid, text)
FROM PUBLIC, kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness,
     kdive_provider_authority;
GRANT EXECUTE ON FUNCTION public.read_stranded_activation_target(uuid, text)
    TO kdive_provider_authority;
