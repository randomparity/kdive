# Local external-boot deadline implementation plan (#2800)

Base `main`; branch `feat/external-boot-deadlines-2800`; full-spec lane. Fixed design denominator:
1000 changed lines (L), from issue #2800's persisted and worker deadline surfaces. Estimated
diff: 450–750 changed lines. ADR 0684 and the design spec govern. Use `just lint`, `just type`,
`just test-changed`, focused pytest, generated config checks, and pre-push `just ci`. No
unrelated remote-provider or authority-provision changes.

## Task 1 — Bounded timing snapshot

Add `LocalExternalBootTimingV1` in a dependency-light module that both payload and authority
protocol may import. Compute rounded-up integer console-window and total-budget seconds from
the persisted accelerator and existing settings. The function rejects non-finite values and
values above `timedelta.max` whole seconds before serialization. At every absolute deadline
addition, reject calendar overflow against that creator's clock as `configuration_error` instead of
an uncaught exception. Add `server` to the two settings' `processes` declarations, and test
server startup validation for invalid values. Add optional `local_timing` to
`BootPayload` and `RecoveryRequestV1`, preserving legacy deserialization.

Contract: default KVM 900/1200, TCG 9000/12000, unknown TCG-safe; arbitrary positive base
configuration either produces representable `budget > window` or an actionable refusal.
Mode: focused-test — extend `tests/providers/local_libvirt/test_deadlines.py` and payload tests;
an unscaled, overflowing, or nonpositive calculation must fail. Include a server validation
case for each setting. Run direct pytest on those files.

## Task 2 — Server admission and replay

At centralized activation payload creation, read `System.accel` and save local timing for the
local provider. For release and conflict requests, retain the early keyed replay lookup, then
create fresh metadata only after activation and binding admission, using the DB clock and one
local timing snapshot. Keep orphan metadata at five minutes. Do not recompute either deadline
or snapshot for replay. Update agent-facing wrappers and generated config reference.

Contract: new local activation and recovery jobs persist timing; KVM/TCG configured windows
fit inside deadlines; replay retains the first budget after config changes; remote and orphan
jobs retain five minutes; oversized absolute deadline returns a configuration error.
Mode: focused-test — extend `tests/services/external_boot/test_recovery_requests.py`, the
activation admission tests, and wrapper-schema tests; fixed deadlines or recomputed replays
must fail. Run direct pytest on those files.

## Task 3 — Worker and journal

Resolve timing from the activation payload or recovery metadata once at worker entry. Use its
budget for a new activation deadline after preparation. Verify the existing `deadline`
intermediate makes the worker immediately re-enter the handler; on that resumed invocation,
anchor the fresh authority client to the committed activation deadline plus 30 seconds of
transport-return headroom rather than the preparation client's spent clock. Cap recovery
client time to the persisted deadline's remaining duration. Keep historical and remote
five-minute paths. Add optional timing to `AuthorityMutationRequestV1` and `JournalRecordV1`; bind it in
`_operation_matches` and preserve it through journal admission/replay. A retry must not renew
an already committed activation or recovery attempt deadline.

Contract: worker sends the exact persisted local snapshot; a timing change for an admitted
operation is a journal conflict; legacy records deserialize and replay; an expired recovery
request cannot acquire another five-minute client window. Delayed preparation still leaves a
fresh active-client window containing the committed readiness deadline; a resumed activation
does not renew that deadline. Mode: focused-test — extend
`tests/jobs/handlers/external_boot/test_runner.py`, `test_lifecycle.py`, and authority journal
service tests; original fixed or renewed deadlines must fail. Run direct pytest on those files.

## Task 4 — Local authority and provider poll

Thread the optional snapshot through the local authority adapter, coordinator, IO factory,
and session to the console-window creator. Before mutation, validate explicit accelerator
against the owned inactive XML and require the host's effective configured window, computed
with the snapshot accelerator, to equal the snapshot window. A nullable accelerator accepts
either XML type with the TCG-safe scale. Use the validated snapshot window for the poll;
without a snapshot, keep historical host configuration behavior. Keep ADR-0681's TCG hard
recovery stop.

Contract: a host-window mismatch and XML disagreement both refuse before mutation; a
matching host configuration cannot extend a snapshotted poll; KVM/TCG/unknown and legacy
paths work. Mode: focused-test — extend
`tests/providers/local_libvirt/lifecycle/boot/test_session.py`,
`test_session_mechanisms.py`, local authority adapter tests, and journal replay tests; a
host-configured longer poll, XML mismatch, or changed replay snapshot must fail. Run direct
pytest on those files.

## Verification and handoff

After each task run its focused tests, then `just lint`, `just type`, `just test-changed`, doc
and generated-artifact checks. Review architecture, code, security, and simplification within
the routed `iterating` depth. Commit the design, implementation, and each review correction
separately after green relevant gates. Push through the full pre-push `just ci`, wait for all PR
checks, and hand off a green mergeable head without merging. No live VM claim without a real
fixture-backed run. No deferrals are planned.
