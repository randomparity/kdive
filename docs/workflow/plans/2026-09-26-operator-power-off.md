# Operator power off implementation plan (#2801)

Goal: operator `PowerAction.OFF` requests clean shutdown and uses the ADR-0679 hard fallback.
The local-libvirt control provider reads its domain XML type, selects the existing KVM/TCG bound,
and calls the shared helper. The `Controller` port and other actions remain stable.
Tech stack: Python 3.14, libvirt, `defusedxml`, pytest.

Expected implementation size: 310–390 changed lines (M) — provider route, OFF fence, race tests, and agent-facing docs

The original 150–240 estimate missed the setup and cleanup needed for three deterministic
database-backed race cases in the existing adversarial test module. Those cases serve the frozen
OFF/force-crash and cancellation criteria; the added lines do not widen the approved surface.

Fixed design denominator: 250 changed lines (M), from #2801's control semantics, wait contract,
and accepted-ADR hazard. Base: `main`; branch:
`feat/clean-operator-power-off-2801`; scope token: `q2801-a6c91b4e`.

## Global Constraints

Python 3.14; x86_64 and ppc64le targets; reuse installed `defusedxml`; no new dependencies or
changes to the `Controller` port.

## File map

- `src/kdive/providers/local_libvirt/lifecycle/control.py`: owns local provider power dispatch;
  add only the off route, XML type validation, and injectable clock/sleep for deterministic tests.
- `tests/providers/local_libvirt/test_control.py`: owns control provider behavior proofs; extend
  existing fake-domain coverage without changing the shared fake.
- `src/kdive/jobs/handlers/control/control.py`: owns the power-job precheck and provider call;
  fence only OFF across those steps with the existing System advisory key. Other actions retain
  their route and signature.
- `tests/adversarial/test_provider_state_races.py`: already owns power/force-crash races; extend
  it to prove OFF and force-crash ordering plus release/autocommit restoration on exception or
  cancellation, reusing its database fixtures and System seeding.
- `docs/adr/0685-operator-power-off-clean-shutdown.md`: records the operator decision and
  amendment to ADR-0028. No caller migration or obsolete path is needed; the off branch replaces
  its hard destroy call, and the existing `Controller` signature is retained.
- `src/kdive/mcp/tools/lifecycle/control/registrar.py`: owns the tool schema text; describe the
  local-libvirt OFF wait and fallback in the wrapper and `action` Field, keeping other actions.
- `docs/guide/reference/control.md`: regenerate from that wrapper with `just docs`.
- `tests/mcp/lifecycle/test_control_registrar.py`: prove the published wrapper/Field contract.

## Task 1 — prove clean off and accelerator selection

**Interfaces:** `LocalLibvirtControl.power(domain_name: str, action: PowerAction) -> None` is
unchanged. Tests inject fake `sleep(seconds)` and `clock() -> float` through optional constructor
arguments; production keeps `time.sleep` and `time.monotonic`.

1. In `tests/providers/local_libvirt/test_control.py`, add tests for a running `kvm` and `qemu`
   domain, including a fake clock. Assert a cooperative guest receives `shutdown` and no
   `destroy`. For a guest ignoring shutdown, assert `destroy` at 60 seconds for KVM and at
   `60 * tcg_deadline_multiplier("tcg")` for TCG.
2. Run `uv run python -m pytest tests/providers/local_libvirt/test_control.py -q`; expect the
   new clean-stop assertion to fail while the branch still hard-destroys.
3. In `control.py`, add the optional clock/sleep seams, import the existing helper and bound,
   parse `domain.XMLDesc(0)` with `defusedxml.ElementTree.fromstring`, and accept only root
   `domain` types `kvm` and `qemu`. Return for a domain already in `SHUTOFF`; otherwise call
   `power_off(domain, domain_name, clean_shutdown_bound_s(accel), sleep, clock)`.
4. Run the same focused command; expect all tests to pass. Commit the behavior and tests.

## Task 2 — prove failure and idempotence paths

**Interfaces:** `Controller.power` and `ErrorCategory.CONTROL_FAILURE` remain unchanged;
`power_off` retains the ADR-0679 state-race contract.

1. In `test_control.py`, add tests for already-off with malformed XML, active malformed or
   unsupported XML, unreadable XML, refused shutdown/destroy fallback, and failed destroy.
   Assert malformed/unreadable XML causes `CONTROL_FAILURE` before stop mutation, while
   already-off succeeds. Assert libvirt failure maps to `CONTROL_FAILURE`.
2. Run `uv run python -m pytest tests/providers/local_libvirt/test_control.py -q`; expect the
   new failure assertions to fail until `control.py` maps parse/type errors. Implement that
   mapping without logging or returning XML contents, then rerun; expect pass.
3. Run `uv run python -m pytest tests/providers/local_libvirt/test_control.py
   tests/providers/local_libvirt/lifecycle/test_power.py -q`; expect pass. Commit the tests and
   final error mapping.

## Task 3 — fence OFF against force-crash

**Interfaces:** `power_handler(conn: AsyncConnection, job: Job, *, resolver: ProviderResolver)`
keeps its signature. The OFF branch uses `scoped_session_advisory_lock(conn,
LockScope.SYSTEM, system_id)` while the other actions keep their current path. The connection's
prior autocommit mode is restored in `finally`, following `publication_fence`.

1. Extend `tests/adversarial/test_provider_state_races.py` with a blocked OFF fake controller and
   concurrent force-crash job. Verify CRASHING cannot commit until OFF returns, and that an OFF
   started after CRASHING is refused before provider IO. Add provider-raise and task-cancellation
   cases. In cancellation, hold the fake provider thread blocked after cancelling the handler and
   prove the crash marker still cannot commit; then unblock it and assert cancellation propagates,
   the session lock releases, and original autocommit mode is restored.
   `focused-test`: expect the race test to fail before the fence; run
   `uv run python -m pytest tests/adversarial/test_provider_state_races.py -q`.
2. In `control.py`, for OFF only, require an idle top-level connection, temporarily set
   autocommit true, enter the scoped session advisory lock, run the existing `_power_target`
   precheck and provider call, then release the lock and restore autocommit in `finally`. Run the
   complete fenced operation in a shielded task. If the handler is cancelled, continue awaiting
   that task until provider IO, lock release, and autocommit restoration finish, then propagate
   cancellation. Keep the audit transaction after the provider call. Do not change other actions.
3. Run `uv run python -m pytest tests/adversarial/test_provider_state_races.py -q`; expect pass
   and confirm the existing pre-marker contract for other actions remains intact. Commit.

## Task 4 — publish the OFF wait contract

**Interfaces:** `control.power` tool name and parameters are unchanged. The decorated wrapper's
docstring and `action` Field are the agent-facing schema; generated `control.md` derives from it.

1. In `tests/mcp/lifecycle/test_control_registrar.py`, assert wrapper/Field descriptions name
   clean shutdown, KVM's 60-second bound, and hard destroy fallback.
   `focused-test`: run `uv run python -m pytest
   tests/mcp/lifecycle/test_control_registrar.py::test_register_publishes_control_tool_contracts
   -q`; expect failure before the text change.
2. Update only OFF text in `src/kdive/mcp/tools/lifecycle/control/registrar.py`, stating unit,
   monotonic reference clock, per-job scope, fallback, and `jobs.wait` polling. Run `just docs`.
3. Re-run the focused test and `just docs-check`; expect both pass. Commit wrapper, generated
   reference, and test.

## Ship checks

Run `just lint`, `just type`, and `just test-changed` on the assembled branch, then the mandatory
pre-push `just ci`. Review the branch, simplify only behavior-preserving noise, and let PR CI run.
Live VM proof is not applicable in the ordinary unit-test checkout because that tier requires a
provisioned operator VM; report that limitation in the PR.
