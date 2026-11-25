# Explicit provider console ownership

## Problem

SysRq, crash-watch and boot evidence infer local storage from a missing provider port.
The boot handler also uses a diagnostic operation string to decide which read errors are fatal.

## Scope

Move local console reading and snapshot mechanics into local-libvirt composition and an adapter.
Rename the internal RemoteConsoleReader protocol to ConsoleReader; remote behavior stays intact.
The neutral port describes internal buffers: local control reads retain raw bytes and byte offsets;
remote reads retain seam redaction. Consumers still redact before persistence or response.
Keep local cumulative reads at start_index=0; reject nonzero local part-window indices explicitly.
Local snapshots keep mark0, shared redacted reads, empty/missing-file behavior, System ownership,
Run attribution, object names, etag refresh and caller transaction boundaries.
Wire ConsoleCapabilities in local composition with its store and registry; require a reader when
SysRq/crash-watch is advertised. No-port boot produces no evidence; controls fail unavailable.
Replace operation-string inspection with ConsoleSnapshotReadError carrying the original categorized
local read failure's category, details, terminal flag and cause. Boot propagates that type and
degrades other snapshot/registration faults. Remote snapshot failures remain best-effort.
Move store/upsert out of boot_evidence; use ConsoleSnapshot directly. Keep caller policies there.
Migrate all affected provider doubles, imports and tests; remove fallback code and obsolete args.
Unrelated diagnostics keep their existing shared console helper. Outcome assessments remain in
console-provider-boundary step3. See [ADR-0864](../../adr/0864-provider-owned-console-collaborators.md).

### Failure model

Local strict read errors propagate, without changing raw byte offsets or exception category.
Missing files still read as empty through the existing helper. No-port runtime never reads a file.
SysRq rejects pumped=False; watch retries; boot mark failures fall back to0. A typed fatal snapshot
read preserves operator remediation while store/DB faults cannot fail an otherwise successful boot.
No new thread task, cleanup owner, persistence transaction or externally callable raw path is added.

## Success

All three handlers select explicit provider collaborators without local-path knowledge. Existing
growth/truncation, cross-mark secrets, current-registry masking and post-redaction caps remain valid.
Remote slicing/leader ordering and157scheduling proof remain intact. Applied SQL bytes are unchanged.

## Validation

Mode: focused-test. Add local adapter cases for raw cumulative offsets, strict error,
empty/missing source and unsupported index; reader-capability assembly rejection must fail before
the runtime change. Existing control tests prove masking, byte seams, pumped policies and truncation.
Mode: focused-test. Before deleting fallback, add no-port boot/control tests that fail when a local
file is read. Cover typed fatal source failure independently of its diagnostic operation string,
and best-effort storage/registration faults. Run per-Run snapshot/etag tests against disposable
Postgres as the worker role, including two Runs and same-Run refresh; skips are not DB proof.
Run `just test-verbose` with these exact paths (one invocation), plus `just lint` and `just type`:
tests/providers/local_libvirt/test_console.py tests/providers/test_runtime_descriptor.py
tests/providers/local_libvirt/test_composition.py tests/providers/remote_libvirt/test_composition.py
tests/providers/remote_libvirt/console/test_console_read.py
tests/providers/remote_libvirt/console/test_console_snapshot.py
tests/jobs/handlers/test_diagnostic_sysrq_handler.py tests/jobs/handlers/control/test_watch_for_crash.py
tests/jobs/handlers/test_runs_boot.py tests/jobs/handlers/test_boot_evidence_run_id.py
tests/jobs/handlers/runs/test_ports.py tests/mcp/lifecycle/test_runs_tools.py
Use controlled faults for capability admission, absent-port local fallback and typed-failure routing;
observe meaningful failures, restore exact bytes and rerun affected cases. Independent code/security
review precedes a conventional local source commit; no extra test is needed for a pure import move.
