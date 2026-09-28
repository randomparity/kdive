# Pre-target teardown recovery — implementation plan (#2880)

Goal: local recovery settles `pre-stop-intent` metadata (ADR-0707) so authority System teardown
completes, and the authority provider-boundary warning names the swallowed exception.

Architecture: one new branch in the concrete local operation's `recover_modules`, reached through
the coordinator's widened resumable set; the authority adapter is unchanged. The service's
`_provider_error` gains an optional exception argument rendered redacted into the log text.

Tech stack: Python 3, pytest (`anyio` marker for async), ruff, mypy/pyright via `just type`.

Expected implementation size: 150–220 changed lines (M) — two source files (~60 lines) and three
test files (~120 lines) from the task list below.

## Global Constraints

- Spec: `docs/workflow/specs/2026-09-28-pre-target-teardown-recovery-design.md`.
- Stay inside: `src/kdive/providers/local_libvirt/lifecycle/boot/external_boot.py`,
  `src/kdive/providers/external_boot_authority/service.py`, `tests/providers/`. No schema,
  migration, `src/kdive/jobs/worker.py`, or `src/kdive/jobs/queue.py` change; no new dependency.
- Guardrails: `just lint`, `just type`, `just records`, `just test-verbose <paths>`; exit codes
  read bare. Code may cite ADR-0707 (Accepted in this PR).

## Task 1 — recover settles `pre-stop-intent` (criteria 1–4)

Files: modify `src/kdive/providers/local_libvirt/lifecycle/boot/external_boot.py`; test in
`tests/providers/local_libvirt/test_external_boot.py` and
`tests/providers/local_libvirt/test_external_boot_authority.py`.

Interfaces: consumes existing `_restart_fixture`, `_FreshRestartHarness`, `_point`, `_SOURCE_XML`,
`_metadata`, `_FakeIO`, `_adapter`, `_system_teardown_request`, `_context`,
`_TEARDOWN_RESERVATION` (all present in those test modules). Adds
`_SessionModulePublicationIO.remove_staging() -> None` and
`_RealLocalExternalBootOperation._settle_unpublished_modules(metadata) -> None`.

Verification:
- Contract: pre-stop recovery with source or staged layout reaches `recovered`, retry converges.
  Mode: focused-test — `test_pre_stop_intent_recovery_*` in `test_external_boot.py`; red on main
  with `ValueError: external-boot recovery phase is not resumable`; green with
  `just test-verbose tests/providers/local_libvirt/test_external_boot.py -k pre_stop_intent_recovery`.
- Contract: conflicting host state or layout refuses before mutation. Mode: focused-test —
  `test_pre_stop_intent_recovery_refuses_*`; red on main (message differs); same command.
- Contract: adapter System teardown on `pre-stop-intent` completes in order. Mode: focused-test —
  `test_system_teardown_settles_pre_stop_intent_activation` in `test_external_boot_authority.py`;
  red on main with the `provider_conflict`/`ValueError`; green with
  `just test-verbose tests/providers/local_libvirt/test_external_boot_authority.py -k pre_stop`.

Steps:

1. Write the tests. Staging path:
   `f"/lib/modules/.kdive-{metadata.binding.activation_id}-staging"`; target state is
   `metadata.target_state.modules`.
   - `source_present` parametrized `[True, False]`: `_restart_fixture(tmp_path,
     phase="pre-stop-intent", source_present=...)`; `ports.recover(_point(metadata),
     OpaqueProviderRef(ref="authority/current"))`; assert phase `recovered`, `session.active`,
     `session.stops == []`, `guest.states` equals the source-only map, no `remove:` action.
   - staged: add `guest.states[staging] = target` before `recover`; assert staging removed,
     `remove:.kdive-<activation>-staging#1` in `guest.faults.actions`, phase `recovered`.
   - retry: `_FreshRestartHarness.create(tmp_path, phase="pre-stop-intent", source_present=True)`,
     stage the target, set `harness.faults.failures["remove:<staging name>#1"] = "after"`,
     `pytest.raises(_ProcessLost)` on `harness.recover()`, then `harness.recover()` reaches
     `recovered`.
   - refusals, parametrized: `active=True` (source XML); `xml=_metadata().target_xml`; an extra
     old name `/lib/modules/.kdive-<activation>-old` holding the target. Assert `ValueError`,
     phase `pre-stop-intent`, `session.stops == []`, and no `start`/`define`/`remove:` action.
   - adapter: `io = _FakeIO(_metadata("pre-stop-intent"))`, run
     `execute_system_teardown(_system_teardown_request(), _context(AuthorityOperation.TEARDOWN),
     _TEARDOWN_RESERVATION)`; assert `result.complete` and `recover-modules` <
     `finalize` < `teardown-system` in `io.actions`.
2. Run the two commands; expect the red failures named above.
3. Implement. In `LocalLibvirtExternalBoot.recover` add `"pre-stop-intent"` to the resumable set.
   In `_SessionModulePublicationIO` add:

   ```python
   def remove_staging(self) -> None:
       self._guest.rm_rf(self._staging)
   ```

   At the top of `_RealLocalExternalBootOperation.recover_modules`:

   ```python
   if metadata.phase == "pre-stop-intent":
       self._settle_unpublished_modules(metadata)
       return
   ```

   and the new method beside it:

   ```python
   def _settle_unpublished_modules(self, metadata: LocalRecoveryMetadataV1) -> None:
       """ADR-0707: an activation that never published keeps only its own staging name."""
       if self._host_state(metadata) != ("source", False):
           raise ValueError("external-boot pre-stop recovery requires inactive source XML/power")
       self._session.require_inactive()
       prior = _layout_component(metadata.source_state.modules)
       desired = _present_component(metadata.target_state.modules, "target module state")
       with self._session.guest() as opened_guest:
           publication = _SessionModulePublicationIO(
               cast(_GuestfsTreeHandle, opened_guest),
               metadata,
               self._recovery_root,
               self._recovery_writer,
               self._session,
           )
           layout = publication.observe_layout()
           if layout == ModuleLayout(prior, desired, None):
               publication.remove_staging()
               publication.guest_sync()
               layout = publication.observe_layout()
           if layout != ModuleLayout(prior, None, None):
               raise ValueError("external-boot pre-stop module layout conflicts with metadata")
           observed = self._observe_modules(opened_guest, metadata)
       self.record_phase(metadata, "module-restored", inactive_modules=observed)
   ```

4. Re-run both commands; expect all selected tests to pass. Commit
   `fix: recover an unpublished pre-stop-intent local activation`.

## Task 2 — provider-boundary warning names the exception (criterion 5)

Files: modify `src/kdive/providers/external_boot_authority/service.py`; test in
`tests/providers/external_boot_authority/test_service_teardown.py`.

Interfaces: consumes `Redactor`, `redact_url_credentials`
(`kdive.security.secrets.redaction`) and `SecretRegistry`
(`kdive.security.secrets.secret_registry`). Changes
`_provider_error(self, request, error: Exception | None = None) -> AuthorityServiceError`.

Verification:
- Contract: redacted, bounded type and message in the warning; wire unchanged. Mode: focused-test
  — `test_provider_failure_log_names_the_redacted_exception`; red on main (message lacks
  `ValueError`); green with
  `just test-verbose tests/providers/external_boot_authority/test_service_teardown.py -k redacted`.

Steps:

1. Add `self.failure: Exception | None = None` to `_TeardownAdapter.__init__` and raise it in
   `execute_system_teardown` right after `await self.release.wait()` when set. Test: set
   `adapter.failure = ValueError(...)` whose text holds a URL with a userinfo password, a
   `password=<value>` pair, and 600 filler characters (mark the literal with
   `# pragma: allowlist secret`); `caplog.at_level(logging.WARNING)`; assert `AuthorityServiceError` matching
   `provider_conflict`; exactly one record starting `authority provider boundary failed: ValueError:`;
   neither secret value present; `record.error_type == "ValueError"`;
   `len(record.error_message) == 512`.
2. Run the command; expect the red failure.
3. Implement: module constants `_ERROR_MESSAGE_MAX = 512` and
   `_REDACTOR = Redactor(registry=SecretRegistry())` with a comment that the authority host holds
   no secret registry. In `_provider_error`, when `error` is not `None` compute
   `error_type = type(error).__qualname__` and
   `message = _REDACTOR.redact_text(redact_url_credentials(str(error)))[:_ERROR_MESSAGE_MAX]`,
   log `"authority provider boundary failed: %s: %s"` with those args and add both to `extra`;
   otherwise keep today's call. Change each `except Exception:` whose body is
   `raise self._provider_error(request) from None` to
   `except Exception as error: raise self._provider_error(request, error) from None`.
4. Re-run; expect pass. Run `just test-verbose tests/providers/external_boot_authority`; expect
   green. Commit `fix: log the swallowed authority provider exception`.

## Task 3 — live settle (criterion 6)

Mode: task-test-not-applicable — needs the operator host's retained fixture. After #2881 merges
and the branch is refreshed: deploy, confirm the installed build has
`_settle_unpublished_modules`, start the stack and worker, let the reclaimed teardown attempt run,
then record terminal state, domain absence, cleanup evidence, a single 32 GiB release row, and a
retry that adds none. Fallback: one new public `systems.teardown`. Never edit the DB or journal.
