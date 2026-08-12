-- Let workers append the state-transition audit rows their job handlers own (#1926).
-- Migration 0107 omitted this grant even though provision, install, boot, control, and
-- artifact handlers record their completed or failed transitions through AuditRecorder.
GRANT INSERT ON TABLE public.audit_log TO kdive_worker;
