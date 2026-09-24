-- 0156_reconciler_audit_log_insert.sql — append-only reconciler transition auditing (#2686).
-- record_system runs INSERT ... RETURNING id for every reconciler allocation release and expiry,
-- so the role needs INSERT plus SELECT on the id column, as 0118 granted kdive_worker.
GRANT INSERT ON TABLE public.audit_log TO kdive_reconciler;
GRANT SELECT (id) ON TABLE public.audit_log TO kdive_reconciler;
