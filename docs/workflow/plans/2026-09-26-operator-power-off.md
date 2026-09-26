# Operator power off implementation plan (#2801)

Goal: operator `PowerAction.OFF` requests clean shutdown and uses the ADR-0679 hard fallback.
The local-libvirt control provider reads its domain XML type, selects the existing KVM/TCG bound,
and calls the shared helper. The `Controller` port and other actions remain stable.
Tech stack: Python 3.14, libvirt, `defusedxml`, pytest.

Expected implementation size: 150–240 changed lines (M) — provider route, OFF fence, and focused race tests

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
- `tests/jobs/handlers/control/test_power_off.py`: proves OFF and force-crash ordering plus
  release/autocommit restoration on exception or cancellation.
- `docs/adr/0685-operator-power-off-clean-shutdown.md`: records the operator decision and
  amendment to ADR-0028. No caller migration or obsolete path is needed; the off branch replaces
  its hard destroy call, and the existing `Controller` signature is retained.

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

1. Add `tests/jobs/handlers/control/test_power_off.py` with a blocked OFF fake controller and
   concurrent force-crash job. Verify CRASHING cannot commit until OFF returns, and that an OFF
   started after CRASHING is refused before provider IO. Add provider-raise and task-cancellation
   cases. In cancellation, hold the fake provider thread blocked after cancelling the handler and
   prove the crash marker still cannot commit; then unblock it and assert cancellation propagates,
   the session lock releases, and original autocommit mode is restored.
   `focused-test`: expect the race test to fail before the fence; run
   `uv run python -m pytest tests/jobs/handlers/control/test_power_off.py -q`.
2. In `control.py`, for OFF only, require an idle top-level connection, temporarily set
   autocommit true, enter the scoped session advisory lock, run the existing `_power_target`
   precheck and provider call, then release the lock and restore autocommit in `finally`. Run the
   provider call in a shielded task. If the handler is cancelled, continue awaiting that task
   inside the fence until it finishes, then propagate cancellation. Keep the audit transaction
   after the provider call. Do not change the other action path.
3. Run `uv run python -m pytest tests/jobs/handlers/control/test_power_off.py -q`; expect pass.
   Run `uv run python -m pytest tests/adversarial/test_provider_state_races.py -q`; expect pass
   and confirm the existing pre-marker contract for other actions remains intact. Commit.

## Ship checks

Run `just lint`, `just type`, and `just test-changed` on the assembled branch, then the mandatory
pre-push `just ci`. Review the branch, simplify only behavior-preserving noise, and let PR CI run.
Live VM proof is not applicable in the ordinary unit-test checkout because that tier requires a
provisioned operator VM; report that limitation in the PR.
