-- 0201_system_devices_role_grants.sql — runtime authority for the system_devices ledger
-- (#3699, ADR-0823, ADR-0533). 0200 created the table with no grants; the handlers that use it land
-- here. Additive, forward-only (ADR-0015).
--
-- kdive_server admits a plug (INSERT) and an un-plug (UPDATE to unplugging) and serves
-- systems.list_devices (SELECT). kdive_worker settles a row (UPDATE), deletes it on a settled
-- un-plug, and clears a System's rows at teardown and reprovision (DELETE); it never inserts, the
-- admitting tool mints the row. kdive_reconciler fails a stalled row (UPDATE) and clears a
-- reclaimed System's rows (DELETE).
GRANT SELECT, INSERT, UPDATE ON TABLE public.system_devices TO kdive_server;
GRANT SELECT, UPDATE, DELETE ON TABLE public.system_devices TO kdive_worker;
GRANT SELECT, UPDATE, DELETE ON TABLE public.system_devices TO kdive_reconciler;
