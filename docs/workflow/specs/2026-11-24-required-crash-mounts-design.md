# Required crash-data mounts

## Problem

The reader tolerates every non-root mount failure. Failed /var or /var/crash therefore looks
empty and can trigger missing-dump guidance. A direct capture-directory mount has the same
verified failure: healthy /var/crash/proof-boot yields a matching core; failed mount yields None.
Reader construction also occurs outside the coordinator cleanup block, orphaning its spool on error.

## Scope

Keep policy in _mount_guest_filesystems (C1). For the existing /var/crash/*/vmcore and
vmcore-incomplete layout, rethrow ordinary mount faults when the normal absolute mountpoint
is an ancestor/equal of /var/crash or its direct child capture directory. Use local
PurePosixPath comparisons; no prefix matching or new helper. Existing _mount owns categorized
infrastructure failure, original cause and failed-open handle cleanup.

Extend _real_wait_for_vmcore's existing try/finally to cover reader construction (C2). Initialize
an optional reader; close only a constructed reader. Existing no-core removal then owns failed-open
spools. Preserve allocation order, primary error and successful spool handoff; no new cleanup owner.

Keep read-only operations, devices/paths, sorted parent-first order and root fallback unchanged
(C2). Other mounts retain best effort: /home, /var/log, similarly prefixed siblings and deeper
non-capture directories. No caller migration or compatibility shim. ADR0861 records eligibility;
ADR0217/0251 retain settlement, complete/incomplete selection and genuine-absence behavior.

### Failure model

Configured guestfs supplies its usual absolute directory mountpoints and current capture layout.
Required storage failure is not successful observation of no dump. Unrelated mount failures
may be logged and skipped. Arbitrary malformed maps, alternate/recursive capture layouts and
symlink topology resolution are outside this bounded repair. Control exceptions and existing
best-effort close semantics remain unchanged; no guaranteed release on broken cleanup.
No public/schema, permission, concurrency, dependency or guest-write behavior changes.

## Success

Required root/ancestor/direct capture-directory faults become infrastructure errors with their
original cause and handle-close attempt; failed-open coordinator spools are removed, before a false
no-core outcome/download (C1).
Healthy core/incomplete/empty observations and unrelated failures retain their behavior (C2).
Commit locally, attribute the exact finding, then finish originating I/O outcome work (C4).

## Validation

focused-test: extend tests/providers/local_libvirt/test_retrieve.py with actual reader/harvester
and SDK-boundary fakes. Cover required faults, cause/cleanup, direct-child healthy versus failed
capture, unrelated/deeper/false-prefix controls, empty state, read-only and mount order.
Require baseline RED, controlled required-set/tolerance regressions and exact restoration.
Add actual reader/coordinator failed-open regression in test_retrieve_kdump_wait.py with
SDK-boundary fakes and temporary spool ownership assertions; retain success/empty/harvest-failure controls.
Run just test-verbose for this file plus test_retrieve_kdump.py, test_retrieve_kdump_wait.py,
test_capture_operation.py and retrieve/test_host_dump_capture.py in the same local provider test
directory. Run lint, whole-tree types/hooks and protected unrelated AST/198SQL checks (C3).

task-test-not-applicable: no prose/private-name snapshots or real-hardware claim. Existing
capture/provider tests preserve readiness and selection contracts. Scoped reviews, I/O outcome
and final campaign gates retain their existing owners (C4).
