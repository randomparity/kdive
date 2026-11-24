# Remote console redaction scheduling

## Problem

RemoteLibvirtConsoleReader redacts assembled console bytes synchronously in its async read.
The neighboring boot snapshotter offloads the same CPU work. Long console data can occupy
the worker's event loop during redaction.

## Scope

Offload the existing redaction call after the awaited leader probe, using asyncio.to_thread.
Keep the reader, registry and caller interfaces. Provider collaborator rewiring belongs to
the existing console-provider-boundary step 2; its outcome assessment belongs to step 3.
No new dependency, provider operation, persistence or public contract is introduced.
Scheduling follows [ADR-0863](../../adr/0863-remote-console-redaction-scheduling.md).

### Failure model

Store, leader-probe and redactor failures propagate without a successful console result.
Cancellation propagates; an already-running redactor thread may finish. It reads a locked
registry snapshot and local bytes only, and does not persist or publish output.
Fresh secrets registered during probing must be observed. Registrations after the redactor's
snapshot retain the existing snapshot semantics; this change promises no atomic read/registry view.

## Success

The real redactor runs on a worker thread after the leader probe. The event loop advances
while redaction is blocked. UTF-8 replacement, current-registry masking, cursor and freshness
remain intact. Errors preserve their original identity and no redaction starts after probe failure.

## Validation

Mode: focused-test. Extend tests/providers/remote_libvirt/console/test_console_read.py with
thread/event coordination and an actual Redactor call, registering a secret during the probe.
Before the fix, worker-thread assertion fails; after it, event-loop progress and redaction pass.
Add probe/redactor failure and cancellation cases; retain all existing store/cursor/UTF-8 cases.
Run just test-verbose tests/providers/remote_libvirt/console/test_console_read.py, just lint,
and just type. Apply a controlled synchronous-redaction fault and observe the regression fail.
