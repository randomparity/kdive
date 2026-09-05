# Implement local external-boot readiness windows

Goal: give every local external-boot start a fresh bounded console window and reject legacy
channel-less definitions before mutation.

Architecture: the operation session owns prepare-before-create sequencing; a dedicated mechanism
owns the complete bounded polling loop; factory opening owns the inactive-definition migration gate.
Production composition binds these mechanisms but leaves runtime advertisement to #2246.

Tech stack: Python 3.14, libvirt-python, pytest, Ruff, ty, uv, and just.

## Global constraints

- Preserve ADR-0576's worker-owned truncate-before-start console inode; do not restore byte offsets.
- Use `KDIVE_LIBVIRT_BOOT_WINDOW_S` as one monotonic deadline and the existing five-second cadence.
- Keep `ProviderRuntime.external_boot` unbound until #2246.
- Do not redefine an existing System as a migration side effect.
- Return no console bytes, host paths, or raw libvirt errors to an agent.
- Support declared x86_64 and ppc64le paths; run native live proof only on authorized x86_64.
- Guardrails: focused pytest selections, `just lint`, `just type`, and pre-push `just ci`.

Expected implementation size: 180–300 changed lines (M) — derived from three production modules and
their focused session, readiness-mechanism, and composition regressions.

## Task 1: Reject legacy definitions before resource mutation

Files:

- Modify `src/kdive/providers/local_libvirt/lifecycle/boot/session.py`.
- Modify `tests/providers/local_libvirt/lifecycle/boot/test_session.py`.

Interfaces:

- Consume `_parse_owned_xml(xml: str, system_id: UUID, expected_overlay: str) -> ET.Element`.
- Add a private channel validator consuming inactive XML parsed with
  `libvirt.VIR_DOMAIN_XML_INACTIVE` and the System ID.
- Preserve `LocalExternalBootSessionFactory.open(...) -> LocalExternalBootSession`.
- Task 2 relies on factory opening only channel-capable definitions.

Verification:

- Mode: focused-test. Contract: missing, duplicate, or malformed standard guest-agent channels raise
  terminal `READINESS_FAILURE` before artifact-root creation or any domain mutation. Cases:
  `test_factory_rejects_legacy_definition_before_resource_mutation` and
  `test_factory_rejects_ambiguous_guest_agent_channel`. Expected red: current factory opens the
  artifact root. Green command:
  `uv run python -m pytest tests/providers/local_libvirt/lifecycle/boot/test_session.py -q`.
- Mode: focused-test. Contract: a valid channel proceeds without `defineXML`. Case:
  `test_factory_accepts_one_standard_guest_agent_channel_without_redefine`. Expected red: fixture XML
  currently omits the channel. Same green command.
- Mode: focused-test. Contract: live/inactive disagreement is decided only by the inactive
  definition, and an unreadable inactive definition becomes bounded `INFRASTRUCTURE_FAILURE`.
  Cases: `test_factory_uses_inactive_channel_when_live_xml_disagrees` in both directions and
  `test_factory_bounds_inactive_xml_failure`. Expected red: current `XMLDesc(0)` reads live XML.
  Same green command.

Steps:

1. Extend the test XML fixture so the default owned definition has one standard virtio channel and
   accepts explicit absent, duplicate, and malformed variants.
2. Add the three failing ordering and compatibility tests; record the focused red result.
3. Retrieve `XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)`, validate its ownership, then validate exactly
   one standard virtio target before overlay/artifact opens. Bound retrieval errors as static
   `INFRASTRUCTURE_FAILURE`; raise a static reprovisioning `READINESS_FAILURE` for channel defects.
4. Run the focused command and commit as `fix(local-libvirt): gate legacy external boot domains`.

Acceptance: both prior-power states share the gate because factory opening precedes their branch;
the event list proves no artifact, guest, redefine, destroy, or create occurred.

Rollback: revert the task commit; no persisted definition is modified.

## Task 2: Own prepare-before-create and bounded polling

Files:

- Modify `src/kdive/providers/local_libvirt/lifecycle/boot/session.py`.
- Modify `src/kdive/providers/local_libvirt/lifecycle/boot/readiness.py`.
- Modify `src/kdive/providers/local_libvirt/lifecycle/boot/session_mechanisms.py`.
- Modify `tests/providers/local_libvirt/lifecycle/boot/test_session.py`.
- Modify `tests/providers/local_libvirt/lifecycle/boot/test_session_mechanisms.py`.

Interfaces:

- Change `ReadinessProbe` to a callable accepting the pinned System ID and a
  `ConsoleReadinessWindow` for one complete bounded poll.
- Add `PrepareConsole = Callable[[UUID], ConsoleReadinessWindow]` to the factory. The window retains
  the opened descriptor, `(device, inode)`, prior observed prefix, and pre-create deadline, with
  bounded `read()` and idempotent `close()` methods.
- Add `LocalExternalBootReadiness`, constructed from the existing console path, classifier,
  `_domain_exit_probe`, monotonic clock, sleep, and configured boot-window setting.
- `_ConcreteSession.start()` and `restore_power("running")` use one private prepare/create operation;
  `readiness()` requires its successful generation.
- Task 3 binds the production implementations through the existing builder.

Verification:

- Mode: focused-test. Contract: every session create is immediately preceded by preparation, active
  starts prepare nothing, retries never reuse a prior generation, and failed create leaves readiness
  unavailable. Cases: `test_start_prepares_each_fresh_readiness_window`,
  `test_start_refuses_active_domain_before_preparation`, and
  `test_failed_start_invalidates_readiness`, `test_delayed_readiness_does_not_renew_deadline`, and
  `test_repeated_readiness_returns_cached_result`. Expected red: current event list contains only create.
  Green command: `uv run python -m pytest tests/providers/local_libvirt/lifecycle/boot/test_session.py -q`.
- Mode: focused-test. Contract: one monotonic deadline classifies ready, crash, terminal, probe
  failure, later success after a transient probe failure, timeout, and exact/oversize bounds without sleeping beyond expiry. Cases grouped under
  `TestExternalBootReadiness`. Expected red: the class does not exist. Green command:
  `uv run python -m pytest tests/providers/local_libvirt/lifecycle/boot/test_session_mechanisms.py -q`.
- Mode: focused-test. Contract: removing preparation permits a stale marker to mask a new panic, so
  the regression test fails under a controlled fault. Case:
  `test_new_boot_panic_cannot_be_hidden_by_prior_ready_marker`. Expected red: current whole-log
  single probe returns ready. Same green command.

Steps:

1. Add session ordering/generation tests and deterministic readiness tests; observe the named reds.
2. Add the narrow window-preparation dependency and one private inactive prepare/create path.
   Invalidate/close the prior window and cached result before preparation, expose the new window only
   after successful create, and close it on create failure and session close.
3. Implement the retained-descriptor window with path-identity and prefix-continuity checks, then the
   readiness mechanism with injected clock/sleep/probe seams and an explicit byte cap. Preserve the
   first `ProbeFailure` only for an eventual failed result, let later console verdicts win, and
   perform one final read after terminal state.
4. Add the stale-marker controlled-fault test and verify it bites when the preparation callback is
   replaced with a no-op, then restore it.
5. Run both focused commands and commit as `fix(local-libvirt): anchor external boot readiness`.

Acceptance: all session-owned create paths prepare; a previous marker cannot satisfy a later boot;
every loop exit is bounded and classified without exposing evidence.

Rollback: revert the task commit; the fail-closed unconfigured readiness default remains available.

## Task 3: Bind production mechanisms without advertising the port

Files:

- Modify `src/kdive/providers/local_libvirt/composition.py`.
- Modify `tests/providers/local_libvirt/test_composition.py`.

Interfaces:

- `build_external_boot_session_factory(...)` accepts and forwards `prepare_console` and readiness.
- `build_external_boot_session_mechanisms()` supplies `_prepare_console_log` through a System-ID
  adapter and a constructed `LocalExternalBootReadiness`.
- Preserve `build_runtime(...).external_boot is None` until #2246.

Verification:

- Mode: focused-test. Contract: production binds both mechanisms and opens no host resource during
  construction. Cases: replace `test_production_builder_leaves_readiness_unconfigured` with
  `test_production_builder_binds_external_boot_readiness` and add
  `test_production_builder_binds_console_preparation`. Expected red: readiness is the unconfigured
  sentinel and no preparation field exists. Green command:
  `uv run python -m pytest tests/providers/local_libvirt/test_composition.py -q`.
- Mode: focused-test. Contract: runtime advertisement remains absent. Existing case:
  `test_mechanisms_builder_does_not_advertise_external_boot`. Expected red under an accidental bind;
  green under the intended change. Same command.

Steps:

1. Add failing production-binding tests and retain the existing no-advertisement assertion.
2. Thread both dependencies through the builder and bind concrete production implementations.
3. Run the focused command, then `just lint` and `just type`.
4. Commit as `feat(local-libvirt): bind external boot readiness mechanisms`.

Acceptance: production construction is complete for this child issue and remains dormant until
#2246 binds the port.

Rollback: revert the task commit; no host resource or persisted state was created by construction.
