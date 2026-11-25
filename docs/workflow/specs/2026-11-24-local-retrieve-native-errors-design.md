# Local retrieve native failure boundaries

## Problem

Three libvirt lookup catches turn infrastructure failure into absence/quiescence. Host-dump
metadata errors escape categorization, and connection-close failure can hide a primary error
or interrupt delivery of a completed spool. The private29case reproduction has25failures.

## Scope

Implement C1–C4 under [ADR-0859](../../adr/0859-local-retrieve-native-failure-boundaries.md).
In existing `retrieve/guestfs.py`, one private context manager `_retrieve_domain` opens the
configured connection, looks up the System domain, yields domain-or-None, and attempts close.
Only lookup `VIR_ERR_NO_DOMAIN` yields None. Other expected libvirt errors from open, lookup
or caller native operations become chained `INFRASTRUCTURE_FAILURE` with fixed operation and
System context. Preserve core-dump's existing category/message/cause and benign missing/inactive
outcomes. Migrate its settlement/force-off callers and `host_dump_capture.py`; remove their
repeated connection/lookup blocks. Host-dump already imports guestfs helpers; no new module,
class, dependency or compatibility export. Operational programming/control faults propagate.
Ordinary close exceptions emit a warning containing only fixed operation and exception type,
then preserve the operation outcome. Control BaseExceptions propagate. Host-dump retains spool
ownership until the connection context exits; existing best-effort removal covers undelivered
spools, including close interruption. Categorize metadata OSError with bounded context/cause.
Keep preparation, size ceiling, streaming, provenance, URI and public signatures unchanged.

### Failure model

- Actors/assets: worker/native libvirt, live guest disk, sensitive core and configured URI.
- Never infer quiescence from non-absence failure or reach overlay reads/persistence afterward.
- Preserve primary identity/category and successful file bytes; no raw URI/path/native text
  enters new error messages or cleanup warnings. Only expected native operations are converted.
- Accept best-effort connection release after close error and existing best-effort spool removal;
  boundary tests/test-driver absence do not prove real VM capture or hardware behavior.

## Success

C1–C4 hold at one three-caller native owner. Valid absence/capture and secure spool contracts
remain intact. C5–C6 provide reproducible failure/preservation evidence and a reviewed local
commit resolving both exact native-boundary findings.

## Validation

- focused-test C1/C4: `test_retrieve_kdump_wait.py::test_native_retrieve_domain_boundary`,
  `test_native_retrieve_operation_failures`, `test_harvest_stops_on_native_failure` and
  `test_host_dump_capture.py::test_host_dump_provider_preserves_native_failure`. RED: RPC
  becomes benign/readiness, native operation faults escape, and harvest reaches reader sentinel.
- focused-test C2/C3: extend `test_active_capture_preserves_bytes_cleanup_and_connection`.
  Add `test_host_dump_close_control_cleans_undelivered_core`. RED: uncategorized metadata,
  replaced primary or undelivered spool. Retarget raw-stat expectation and preserve causes.
- focused-test C5: GREEN command `just test-verbose
  tests/providers/local_libvirt/retrieve/test_host_dump_capture.py
  tests/providers/local_libvirt/test_retrieve_kdump_wait.py
  tests/providers/local_libvirt/test_retrieve_kdump.py
  tests/providers/remote_libvirt/retrieve/test_retrieve_host_dump.py`; rerun private reproduction,
  native test-driver absence controls and a deliberate classification fault/restoration.
- focused-test C5/C6: prep/other AST, signatures and198SQL hashes; `just lint`, whole-tree
  `just type`, hooks and fresh design/scope/code/security reviews before conventional commit.
- task-test-not-applicable: no public schema/migration/remote/live capture change; no new
  hardware claim. Warranted docstrings need no prose snapshots; native faults use boundary seams.
