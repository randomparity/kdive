-- 0197_settled_teardown_route_fence.sql — #3598, ADR-0816 amendment (2026-11-14).
-- Forward-only (ADR-0015). Settlement supersedes only `allocating` and `current` authority rows and
-- ends the activation `abandoned`, so a surviving `retired` row still gave the 0167 resolver a
-- route. A server built before #3551 never reads the settlement, and its authority teardown would
-- mark the System `torn_down` while the worker's domain runs. A settled newest activation now has
-- no route, so every server build refuses with `external_boot_teardown_authority_unresolved`. The
-- marker branch already needs `preparing`; the predicate there states the fence instead of relying
-- on that. CREATE OR REPLACE keeps the signature, owner and the 0147 grants.
CREATE OR REPLACE FUNCTION public.resolve_external_boot_system_teardown_dispatch_binding(
    p_system_id uuid
) RETURNS TABLE (activation_id uuid, run_id uuid, plan_identity text, provider_kind text,
                 authority_instance text)
LANGUAGE sql SECURITY DEFINER SET search_path = '' STABLE AS $$
    WITH newest AS (
        SELECT activation.id, activation.run_id, activation.plan_identity, activation.state
        FROM public.external_boot_activations AS activation
        WHERE activation.system_id = p_system_id
        ORDER BY activation.created_at DESC, activation.id DESC
        LIMIT 1
    )
    SELECT newest.id, newest.run_id, newest.plan_identity,
           authority.provider_kind, authority.authority_instance
    FROM newest
    JOIN LATERAL (
        SELECT a.provider_kind, a.authority_instance
        FROM public.external_boot_authorities AS a
        WHERE a.activation_id = newest.id
          AND a.system_id = p_system_id
          AND a.run_id = newest.run_id
          AND a.plan_identity = newest.plan_identity
          AND a.state IN ('current', 'retired')
        ORDER BY a.generation DESC
        LIMIT 1
    ) AS authority ON true
    WHERE newest.state <> 'torn_down'
      AND NOT EXISTS (
          SELECT 1 FROM public.external_boot_activation_settlements AS settlement
          WHERE settlement.activation_id = newest.id AND settlement.settled_at IS NOT NULL
      )
    UNION ALL
    SELECT newest.id, newest.run_id, newest.plan_identity,
           job.payload #>> '{external_boot_authority_v1,provider_kind}',
           job.payload #>> '{external_boot_authority_v1,authority_instance}'
    FROM newest
    JOIN public.jobs AS job
      ON job.kind = 'boot'
     AND job.payload ->> 'run_id' = newest.run_id::text
     AND job.payload #>> '{external_boot_authority_v1,activation_id}' = newest.id::text
     AND job.payload #>> '{external_boot_authority_v1,run_id}' = newest.run_id::text
     AND job.payload #>> '{external_boot_authority_v1,system_id}' = p_system_id::text
     AND job.payload #>> '{external_boot_authority_v1,plan_identity}' = newest.plan_identity
     AND job.payload #>> '{external_boot_authority_v1,purpose}' = 'activate'
    WHERE newest.state = 'preparing'
      AND NOT EXISTS (
          SELECT 1 FROM public.external_boot_authorities AS a WHERE a.activation_id = newest.id
      )
      AND NOT EXISTS (
          SELECT 1 FROM public.external_boot_activation_settlements AS settlement
          WHERE settlement.activation_id = newest.id AND settlement.settled_at IS NOT NULL
      )
$$;
