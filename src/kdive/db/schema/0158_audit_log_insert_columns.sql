-- Limit runtime audit writers to their populated columns (#2712).
REVOKE INSERT ON TABLE public.audit_log FROM kdive_worker, kdive_reconciler;
GRANT INSERT (principal, agent_session, project, tool, object_kind, object_id,
              transition, args_digest)
ON TABLE public.audit_log TO kdive_worker, kdive_reconciler;
