-- 0199_capture_predicates_string_bodies.sql — #3612, ADR-0818.
-- Forward-only (ADR-0015). Databases that applied the original 0112 hold these two predicates
-- as new-style SQL bodies, which pin their argument row types: PostgreSQL 17.12+ then refuses
-- ALTER TABLE ... ADD COLUMN ... DEFAULT on capture_operations and worker_incarnations. This
-- replaces them with the string bodies 0112 now carries; OR REPLACE keeps owner and grants.

CREATE OR REPLACE FUNCTION public.capture_launch_abort_evidence_valid(
    p_operation public.capture_operations,
    p_evidence jsonb
) RETURNS boolean
LANGUAGE sql
IMMUTABLE
SET search_path = ''
AS $fn$ SELECT jsonb_typeof(p_evidence) = 'object'
   AND (p_evidence ->> 'evidence_kind') = 'closed_gate_boundary_token_scan_v1'
   AND (p_evidence -> 'gate_closed') = 'true'::jsonb
   AND (p_evidence -> 'boundary_scan_complete') = 'true'::jsonb
   AND (p_evidence -> 'boundary_processes_absent') = 'true'::jsonb
   AND jsonb_typeof(p_evidence -> 'host_instance') = 'string'
   AND (p_evidence ->> 'host_instance') = p_operation.host_instance
   AND jsonb_typeof(p_evidence -> 'launch_token') = 'string'
   AND (p_evidence ->> 'launch_token') = p_operation.launch_token
   AND (p_evidence -> 'launch_token_absent') = 'true'::jsonb $fn$;

CREATE OR REPLACE FUNCTION public.capture_recovery_authorized(
    p_owner public.worker_incarnations,
    p_replacement public.worker_incarnations
) RETURNS boolean
LANGUAGE sql
IMMUTABLE
SET search_path = ''
AS $fn$ SELECT p_owner.state = 'terminated'
   AND p_owner.terminated_at IS NOT NULL
   AND p_owner.outcome IS NOT NULL
   AND p_owner.authority_kind = p_replacement.authority_kind
   AND (
       public.capture_worker_host_instance(
           p_owner.authority_kind, p_owner.authority_binding
       ) = public.capture_worker_host_instance(
           p_replacement.authority_kind, p_replacement.authority_binding
       )
       OR (
           public.capture_worker_authority_scope(
               p_owner.authority_kind, p_owner.authority_binding
           ) IS NOT NULL
           AND public.capture_worker_authority_scope(
               p_owner.authority_kind, p_owner.authority_binding
           ) = public.capture_worker_authority_scope(
               p_replacement.authority_kind, p_replacement.authority_binding
           )
       )
   ) $fn$;
