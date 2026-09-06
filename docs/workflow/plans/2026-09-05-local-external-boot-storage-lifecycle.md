# Local external-boot storage lifecycle implementation plan

**Goal.** Make local external-boot storage cleanup exact, retryable, and provisioned with a
pre-advertisement capacity contract.

**Architecture.** Authenticated recovery metadata flows into one descriptor-relative bounded
reclaimer. Host-only artifact cleanup is independent of guest power, while existing guest mutation
guards stay unchanged. The fixed-worker and Ansible seams pass and validate one per-slot root.

**Tech stack.** Python 3.14, stdlib filesystem descriptors, pytest, Ansible.

Design: [spec](../specs/2026-09-05-local-external-boot-storage-lifecycle-design.md),
[ADR-0602](../../adr/0602-local-external-boot-storage-is-reclaimed-by-owned-identities.md).

Expected implementation size: 850–1150 changed lines (L) — derived from production/deployment
surfaces and focused destructive-path, gate, and provisioning regressions.

## Global Constraints

- Python 3.14; targets x86_64 and ppc64le; native proof in this campaign is x86_64 only.
- Ruff line length 100 and strict whole-tree `ty`; no new dependency or migration.
- All deletion is descriptor-relative, no-follow, exact-name, owner/mode checked, and nonrecursive.
- Host paths and private identifiers never enter errors, tests, commits, or public artifacts.
- Run focused tests red then green, `just lint`, `just type`, `just test-changed`, and pre-push
  `just ci > <file> 2>&1 < /dev/null` without masking its exit status.
- Stage Markdown/YAML before `prek run`, then re-add exactly the staged paths.

## Task 1 — make artifact ownership activation-exclusive

**Files:** `external_boot.py`, `session_mechanisms.py`, and their focused tests.

**Interfaces:** replace `_projection_ref` and `_artifact_ref_parts` with the v2 six-component
reference including `activation_id`; change `LocalArtifactRoot.open` to return the activation
directory; make `TargetProjectionStore` publish/reopen below that same component.

**Verification:** Mode: focused-test. Add v1 refusal, v2 ownership round-trip, and two-activation
same-Run/same-digest isolation tests. Observe the current shared path and deletion; run
`uv run python -m pytest tests/providers/local_libvirt/test_external_boot.py tests/providers/local_libvirt/lifecycle/boot/test_session_mechanisms.py -q`
and expect green.

Steps:

1. Add the two-activation collision and reference-version tests; observe shared ownership fail.
2. Add the activation directory and v2 parser/publisher.
3. Run focused tests and commit `refactor(local-libvirt): isolate activation artifacts`.

## Task 2 — make cleanup metadata-bound and power-independent

**Files:** `session.py`, `external_boot.py`, `session_mechanisms.py`, and their focused tests.

**Interfaces:** change `CleanupPayloads` to
`Callable[[int, LocalRecoveryMetadataV1], None]`; change
`LocalExternalBootSession.cleanup_payloads(metadata) -> None`; consume the existing metadata passed
to `_RealLocalExternalBootOperation.cleanup`.

**Verification:** Mode: focused-test. Add restored-running and inactive cleanup tests plus retained
guest/overlay mutation-gate tests. Observe the running case fail at `require_inactive`; run
`uv run python -m pytest tests/providers/local_libvirt/lifecycle/boot/test_session.py tests/providers/local_libvirt/lifecycle/boot/test_session_mechanisms.py -q`
and expect green.

Steps:

1. Add the running/inactive contract tests and record the expected running-arm failure.
2. Carry metadata through the callback and remove only cleanup's inactive check.
3. Run the focused command and commit `fix(local-libvirt): separate artifact cleanup from guest power`.

## Task 3 — reclaim the exact activation hierarchy

**Files:** `session_mechanisms.py`, `external_boot.py`, and
`test_session_mechanisms.py`.

**Interfaces:** extend `LocalPayloadCleanup.cleanup(run_fd, metadata) -> None`; reuse
`_artifact_ref_parts`, private-directory helpers, canonical record readers, and fixed artifact
names. No recursive deletion API is introduced.

**Verification:** Mode: focused-test. Add exact projection/Run/System removal, sibling preservation,
partial-residue taxonomy, and parametrized fault-at-each-removal retry tests. First expect leaked
directories or unsafe acceptance; then run
`uv run python -m pytest tests/providers/local_libvirt/lifecycle/boot/test_session_mechanisms.py -q`
and expect green.

Steps:

1. Add successful exact-hierarchy cleanup and sibling-preservation tests; observe the leak.
2. Implement descriptor-relative exact projection plus activation/Run/System empty-parent pruning.
3. Add matching partial and malformed/foreign/symlink/mode/non-directory/ambiguous refusal tests.
4. Implement bounded partial validation and exact removal with redacted failures.
5. Inject interruption at every removal, retry, and assert convergence without sibling changes.
6. Run focused tests and commit `feat(local-libvirt): reclaim external-boot artifacts`.

## Task 4 — reclaim interrupted preparation through teardown

**Files:** `external_boot.py`, `external_boot_authority.py`, and their focused tests.

**Interfaces:** add the typed shared
`AuthorityRecoveryObservationContextV1` and
`AuthorityMutationAdapter.observe_recovery(request, context)` recovery-only call; add provider-local
`LocalLibvirtExternalBoot.abort_preparation(binding, request identities, authority) -> PartialAbortResult`; add
store inspection/deletion helpers that accept only canonical matching receipts/intents or prove
canonical absence without mutation. No `ExternalBootPorts` method changes.

**Verification:** Mode: focused-test. Drive authority-adapter teardown through receipt-only,
pre-stop-before-stop, and archive-before-rename partials; assert source power restoration, retry
convergence, and fail-closed foreign/ambiguous cases. First expect `provider_conflict`; run
`uv run python -m pytest tests/providers/local_libvirt/test_external_boot.py tests/providers/local_libvirt/test_external_boot_authority.py -q`
and expect green.

Steps:

1. Add failing real-caller crash-window and malformed-owner tests.
2. Implement canonical partial inspection and explicit bounded unlink/rmdir helpers.
3. Implement abort preparation, including source-state verification and prior-power restoration.
4. Route TEARDOWN to abort preparation only when complete recovery-point resolution is absent.
5. Record `removed` or `absent` in a bounded adapter-local handoff containing the exact validated
   request; consume it only for the equal request to return stable terminal `absent`. Keep
   `not-partial` on normal RecoveryPoint handling and failures nonterminal.
6. Add the service-created recovery context and recovery-only adapter call. On handoff eviction or
   adapter restart, prove canonical partial absence without deletion; reject every request/context
   mismatch and every present or unreadable state.
7. Add first-call, lost-response, already-absent, mismatched-request, bounded-eviction,
   real-service adapter-restart, malformed, and I/O-failure observation tests, plus shared contract
   tests proving ordinary observation remains read-only and only verified recovery dispatches the
   new call.
8. Inject interruption at each removal, including activation-directory pruning, and prove request
   retry converges without changing sibling activation directories.
9. Run focused tests and commit `feat(local-libvirt): reclaim interrupted preparation`.

## Task 5 — propagate the per-slot recovery root

**Files:** `deploy/systemd/bin/kdive-live-worker-gate`,
`deploy/ansible/roles/live_vm_host/tasks/main.yml`, `tests/deploy/test_live_worker_gate.py`, and
`tests/deploy/test_live_worker_provisioning.py`.

**Interfaces:** `_WORKER_ENV_NAMES` adds `KDIVE_LIBVIRT_RECOVERY_ROOT`; each generated slot
environment uses `<live_vm_host_worker_recovery_root>/<worker-account>`.

**Verification:** Mode: focused-test. Extend the executable gate environment assertion and
provisioning structural test. First expect the key absent; run
`uv run python -m pytest tests/deploy/test_live_worker_gate.py tests/deploy/test_live_worker_provisioning.py -q`
and expect green.

Steps:

1. Add failing child-environment and per-slot rendering assertions.
2. Add the allowlist name and exact slot environment entry.
3. Run focused tests and commit `feat(deploy): pass external-boot recovery roots to workers`.

## Task 6 — enforce and document capacity

**Files:** local-libvirt settings/materializer/capacity module and tests; fixed-worker gate; Ansible
defaults/tasks/verify files; deployment tests; generated config docs; and
`docs/operating/runbooks/live-testing.md`.

**Interfaces:** `KDIVE_LIBVIRT_EXTERNAL_BOOT_CAPACITY_BYTES` is a positive per-activation byte
ceiling written once per slot and consumed by runtime admission and Ansible's
`ceiling * concurrent activations` minimum. An argv-form filesystem probe feeds a pre-release
assertion. Local materialization reservation uses closed plan sizes and fixed recovery overhead.

**Verification:** Mode: focused-test. Add structural tests for formula inputs, argv use, positive
validation, insufficient-capacity failure, and ordering before worker release. First expect missing
variables/tasks; run
`uv run python -m pytest tests/deploy/test_live_worker_provisioning.py -q` and expect green.

Steps:

1. Add runtime equality/one-byte-over tests and assert failure precedes artifact creation.
2. Implement the setting, reservation calculation, metadata upper bounds, and runtime admission.
3. Add failing capacity-default, validation, probe, and ordering tests.
4. Implement defaults and preflight/verify tasks; pass the same ceiling to the worker and keep paths
   out of diagnostics.
5. Regenerate config docs and document unit, reference state, per-worker scope, failure consequence,
   and recovery action.
6. Run `just lint-ansible`, the focused tests, and a clean-host Ansible check/syntax proof.
7. Run the authorized x86_64 local-libvirt provisioning proof and verify each slot's environment and
   available-byte gate; clean any fixtures created by the proof.
8. Run changed tests and guardrails, then commit `feat(deploy): gate external-boot recovery capacity`.
