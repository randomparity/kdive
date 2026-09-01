# Local-libvirt external-boot adapter implementation plan

Goal: replace `RealLocalExternalBootIO`'s semantic host delegation with one operation-scoped
session that composes ADR-0586 recovery capabilities and durable restart tables. Python 3.14 is
used on x86_64 and ppc64le; production advertisement, schemas, migrations, dependencies, and MCP
contracts remain unchanged.

## Global constraints

- Open exactly one authenticated `LocalExternalBootSession` for each six-port operation and close
  it after all evidence and resource work for that call.
- Reject substituted ownership, artifacts, XML, guest trees, or restart layouts before mutation.
- Bound recursive traversal before materializing entries; never expose or call libguestfs `find`.
- Record phase evidence before the next mutation and resume only from the existing closed restart
  tables.
- Keep production external-boot support unadvertised.
- Run focused tests, `just lint`, `just type`, `prek run`, and pre-push `just ci`.

## Task 1 — Prove the session-owned adapter boundary

Files: modify `tests/providers/local_libvirt/test_external_boot.py`; modify
`src/kdive/providers/local_libvirt/lifecycle/boot/external_boot.py`; modify
`src/kdive/providers/local_libvirt/lifecycle/boot/session.py`; modify
`tests/providers/local_libvirt/lifecycle/boot/test_session.py`.

Interfaces: replace fine-grained `LocalExternalBootIO` use with
`LocalExternalBootIO.open(authority: OpaqueProviderRef, expected: ExpectedOperationOwnership) ->
AbstractContextManager[LocalExternalBootOperation]`. The operation exposes
materialize, prepare/reopen, module publication, XML/power/readiness, phase recording,
cleanup-complete, cleanup, and recovery-reference operations without another authority argument.
Each public coordinator method enters this context once; no hidden operation survives context exit.
Replace `LocalExternalBootHost` with injected `ResolveOperationLease =
Callable[[OpaqueProviderRef], LocalExternalBootOperationLease]` plus the existing
`LocalExternalBootSessionFactory`. #2140 supplies the authenticated resolver in future production
composition; this adapter treats authority as opaque, passes the resolved live lease to
`factory.open(lease, expected)`, and never rereads the lease. Add immutable
`ExpectedOperationOwnership(system_id: UUID, run_id: UUID, activation_id: UUID | None)` to
`session.py`; the factory compares it with atomic `PinnedOperationOwnership` before opening any
libvirt or filesystem resource. `activation_id=None` means compare System/Run only for
materialization; preparation and recovery-point operations require exact activation.

1. Add table-driven tests whose operation/session fake records exactly one open and one attempted
   close for materialize, prepare, activate, observe, recover, and cleanup, on success and on each
   coordinator validation or operation failure. Run `just test-verbose
   tests/providers/local_libvirt/test_external_boot.py -k real_adapter` and `just test-verbose
   tests/providers/local_libvirt/lifecycle/boot/test_session.py -k expected_ownership`; expect
   failures because the coordinator still uses fine-grained IO calls and the factory accepts no
   expected ownership.
2. Change the coordinator/IO boundary to the operation context. Change
   `RealLocalExternalBootIO` construction and preparation to open the session, validate the complete
   binding and closed inspection, publish pre-stop intent, and close on success/error. Add the
   factory's atomic expected-ownership comparison before resource opens. Re-run both focused
   selections; expect them to pass.
3. Add same-System/cross-Run, same-Run/cross-activation, wrong-domain-state, and close-fault tests;
   ownership substitution must reject before resource open. Inject a binding comparison fault,
   confirm the cross-binding test fails, revert it, and re-run the selection. Add combined rows
   where coordinator validation or operation work fails and context exit also fails; assert the
   work error remains primary, the close error is attached according to the existing exception-note
   convention, and close is attempted exactly once. Expect all tests to pass.

Acceptance: preparation performs no privileged operation outside the session, publishes intent
before stopping, and closes every session while preserving the primary error.

## Task 2 — Compose bounded Task 2 guest capabilities

Files: modify `src/kdive/providers/local_libvirt/lifecycle/boot/session.py`; modify
`src/kdive/providers/local_libvirt/lifecycle/boot/external_boot.py`; modify
`tests/providers/local_libvirt/lifecycle/boot/test_session.py`; modify
`tests/providers/local_libvirt/test_external_boot.py`.

Interfaces: add `_Guest.find0(directory: str, files: str) -> None`, `_Guest.user_cancel() -> None`,
and `open_tree(path: str, *, limit: int) -> TreeCursor` to `InactiveGuest`,
where `TreeCursor` is an `AbstractContextManager[Iterator[InactiveGuestDirectoryEntry]]` with
mandatory `close()`. The implementation creates an owner-only temporary directory and FIFO,
starts one producer thread calling libguestfs's cancellable streaming
`find0(path, fifo_path)` FileOut API once for the whole recursive tree, and incrementally parses
NUL-delimited relative paths through end-of-stream or entry `limit + 1`. On a bounded stream it
validates and byte-sorts at most `limit` paths before yielding; the adapter never recursively
reopens discovered directories. Close shuts the FIFO read side, calls thread-safe `user_cancel()`, treats
`EINTR` as expected only after deliberate early termination, joins the cooperating producer, and
removes the FIFO/directory. A backend that violates libguestfs's cancellation contract retains the
operation pin rather than permitting unsafe cleanup. Adapt it
through `LibguestfsAuthenticatedGuestTree`; construct `RecoveryArchiveSink`,
`RecoveryArchiveSource`, and kernel-bundle sources from session-owned descriptors; call existing
`RealGuestRecoveryWriter.capture`, `observe`, `install`, and `restore` signatures unchanged.

1. Add session tests with an instrumented concrete `find0` FileOut producer proving a multilevel
   tree emits every entry exactly once, entry `limit + 1` is the over-limit signal and is not
   retained, early close calls `user_cancel()` and joins a cooperating producer blocked before its
   first write without an unbounded regular output file, cleanup occurs on success/error, two
   producer orders yield identical byte-sorted paths and recovery identity, and
   list-returning `find`, `readdir`, `ls`, and glob APIs are never invoked. Prove context exit closes
   a caller-abandoned cursor. Add an event-driven non-cooperating producer test: after
   `user_cancel()` the producer stays live until the test releases it; assert session close remains
   pending and the operation pin is not released, then release the producer and assert cleanup and
   pin release complete in order. Add adapter tests for present, present-empty,
   absent, duplicate, hostile, hard-linked, and over-limit trees. Run the two focused test files;
   expect the missing primitive/capability failures.
2. Implement the minimum bounded traversal and descriptor adapters, checking entry/byte bounds
   before retention. Re-run the focused tests; expect all tests to pass.
3. Add source substitution, short-read, cross-owner, wrong-release, and cleanup-on-error tests.
   Re-run the focused tests; expect all tests to pass.

Acceptance: Task 2 receives only authenticated tree/source/sink capabilities, recursive work is
bounded before materialization, and every rejection before Task 2 writes leaves the guest unchanged.

## Task 3 — Wire activation and exact restart recovery

Files: modify `src/kdive/providers/local_libvirt/lifecycle/boot/external_boot.py`; modify
`tests/providers/local_libvirt/test_external_boot.py`.

Interfaces: implement session-backed `ModulePublicationIO`; retain
`advance_module_publication`, `advance_absence_publication`, `record_phase`, and
`RecoveryMetadataStore` signatures. The adapter owns deterministic staging/old names and constructs
the mutable staging capability only after durable intent and inactivity.

1. Add table-driven integration tests for present and absent activation/recovery, restart before
   and after each move, sync, phase write, and removal, plus fail-before-effect/fail-after-effect
   results. Include recovery requested after activation's `module-restored` fsync but before target
   XML definition; it must restore exact source modules, record `source-restored`, and continue the
   prior-power matrix. Run the focused file; expect failures at the delegated host calls.
2. Implement staging preparation, layout observation, state-table dispatch, and the spec's exact
   XML/power restart matrices. Before define, start, stop, readiness, or a terminal phase write,
   classify the observed exact XML identity and power state and perform only the matrix action.
   Re-run the focused file; expect all tests to pass.
3. Add fail-before-effect and fail-after-effect faults around define, start, stop, readiness, and
   each following phase fsync, plus every unlisted/substituted XML/power combination,
   running-kernel mismatch, and retry. Readiness advances only for
   `ReadinessResult(True, True, None)`; cover unanswered, negative, and probe-error values. Re-run
   the focused file; expect all tests to pass.

Acceptance: every listed durable phase resumes with one permitted action; every unlisted state
fails inactive with no further mutation; XML, power, readiness, and observation agree exactly with
stored metadata.

## Task 4 — Wire cleanup while leaving production composition excluded

Files: modify `src/kdive/providers/local_libvirt/lifecycle/boot/external_boot.py`; modify matching
focused tests under `tests/providers/local_libvirt/`.

Interfaces: retain `cleanup`, `cleanup_complete`, and `finalize_tombstone`, but call
`cleanup_complete` only after opening and validating the operation session. Keep the concrete
adapter dependency-injected; #2140 owns the authenticated resolver and production construction.
Do not modify provider composition, attach the ports to `ProviderRuntime`, or change
`ProviderSupport`.

1. Add cleanup success/retry/substitution tests; present-tombstone finalization; absent-tombstone
   replay with the same exact `mutation-started` proof; stale, cross-binding, cross-point, and
   malformed proof rejection; stale/foreign authority against an already-complete tombstone;
   session-close failure after tombstone publication; and a composition
   assertion that support remains unadvertised. Assert finalization opens no libvirt or guest
   session. Run the focused files; expect the delegated cleanup test to fail.
2. Bind cleanup to the dependency-injected operation context and existing provider-local roots.
   Re-run focused tests; expect all tests to pass.
3. Run `just test-verbose tests/providers/local_libvirt/test_external_boot.py`, `just
   test-verbose tests/providers/local_libvirt/lifecycle/boot/test_recovery.py`, and `just
   test-verbose tests/providers/local_libvirt/lifecycle/boot/test_session.py`; expect all to pass.

Acceptance: cleanup publishes and finalizes only exact tombstones, construction is lazy, and
production capability advertisement is unchanged.

## Task 5 — Guardrails and commits

Files: this spec, this plan, and implementation/test files named above.

1. Run `just format`, stage the exact changed paths, run `prek run`, and re-add only those paths if
   hooks rewrite them.
2. Run `just lint`, `just type`, and `just ci`; expect zero warnings and all gates to pass. Do not
   run live VM tiers without the required operator host.
3. Re-read the diff for naming, scope, restart ordering, and advertisement exclusion. Commit design
   and implementation as separate Conventional Commits no longer than 72 characters.

Acceptance: focused and full PR gates pass, the diff remains inside the frozen scope, and commit
history separates design from implementation.
