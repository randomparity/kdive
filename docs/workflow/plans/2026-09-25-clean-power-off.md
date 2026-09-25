# Clean power-off for local-libvirt boot and install — plan

Goal: `runs.boot` and the module-injecting install stop a running local-libvirt domain with a
bounded clean shutdown instead of a hard `destroy()` (issue #2757, piece A).

Architecture: one private helper `_power_off` in `providers/local_libvirt/lifecycle/install.py`,
called by `LocalLibvirtBooter._power_cycle` and `LocalLibvirtBooter.force_off_if_active`.
`InstallRequest` carries the System accel so both sites scale the wait by ADR-0341.
Spec: [2026-09-25-clean-power-off-design.md](../specs/2026-09-25-clean-power-off-design.md);
decision: [ADR-0679](../../adr/0679-local-libvirt-clean-power-off.md).

Tech stack: Python 3.14, libvirt-python, pytest; `uv` + `just`.

Expected implementation size: 200–290 changed lines (M) — helper ~45, booter/installer wiring
~25, fake ~15, unit tests ~130 (incl. three existing ordering tests), handler + port ~10, live
test ~70. Actual: about 640. The unit tests (~260) cover every path on a fake clock, and the live
test (~220) carries its own env gating, profile, SSH retry and bootstrap-key plumbing because
the equivalent helpers in `test_console_parts_live.py` are private to that module.

## Global Constraints

- Ruff line length 100; `just lint`, `just type` (whole tree), `just test-changed` green before
  each commit; `just format` first. Conventional commits.
- No new dependency, setting, or env var. No MCP schema change.
- Prose avoids "critical", "robust", "comprehensive", "elegant".
- Deferrals: none.

## File map

| File | Change |
|------|--------|
| `src/kdive/providers/local_libvirt/lifecycle/install.py` | add `_power_off`; protocol gains `shutdown`/`state`; booter and facade take `sleep`; `force_off_if_active` takes `accel`; docstring |
| `src/kdive/providers/ports/lifecycle.py` | `InstallRequest.accel: str \| None = None` |
| `src/kdive/jobs/handlers/runs/install.py` | pass `accel=system.accel` |
| `tests/providers/local_libvirt/fakes.py` | `FakeDomain.shutdown`, `state`, `run_state`, `honours_shutdown` |
| `tests/providers/local_libvirt/test_install.py` | power-off unit tests; `_install` passes a no-op sleep |
| `tests/jobs/handlers/test_runs_install.py` | assert `request.accel` |
| `tests/integration/test_first_boot_host_keys_live.py` | new `live_vm` regression test |
| `docs/adr/0030-install-boot-plane.md`, `docs/adr/0206-modules-in-guest-shared-contract.md` | one `Amended by` blockquote each (done with the design) |

## Task 1 — `_power_off` and the booter

**Verification**
- Contract: clean path. `Mode: focused-test` — `test_boot_shuts_down_cleanly_then_creates`: calls
  are `["shutdown", "prepare", "create"]`. Red: current code records `destroy`. Green:
  `just test-verbose tests/providers/local_libvirt/test_install.py -k power_off_or_boot`.
- Contract: non-honouring state. `Mode: focused-test` — parametrized
  `test_boot_destroys_at_once_in_a_state_that_cannot_shut_down` over `PAUSED`, `CRASHED`,
  `PMSUSPENDED`, `NOSTATE`: calls `["destroy", "prepare", "create"]`, no `shutdown`. This is a
  new-behaviour guard, green on current code; it fails if the helper sends `shutdown` to a
  non-honouring state. Same green command.
- Contract: timeout. `Mode: focused-test` — `test_boot_destroys_after_the_bound`: a fake that
  ignores shutdown with `accel="kvm"` records 6 `shutdown` calls (t = 0, 10, …, 50 s on the fake
  clock), 60 sleeps of 1.0, then `destroy`; `caplog` holds `destroy-timeout`. TCG variant
  `test_tcg_scales_the_shutdown_bound`: `accel=None` → 600 sleeps.
- Contract: refused request. `Mode: focused-test` — `test_boot_destroys_when_shutdown_is_refused`:
  `raise_on={"shutdown": VIR_ERR_OPERATION_FAILED}` → `destroy`; log `destroy-refused`.
- Contract: re-send refusal is not fatal. `Mode: focused-test` —
  `test_refused_resend_keeps_waiting`: a `FakeDomain` subclass in `test_install.py` whose
  `shutdown` raises on its second call and whose `state()` reads `SHUTOFF` after 12 reads → no
  `destroy`, log `clean`.
- Contract: blocking `shutdown()` counts against the bound. `Mode: focused-test` —
  `test_blocking_shutdown_counts_against_the_bound`: a fake whose ignored `shutdown` advances the
  fake clock 30 s → `destroy` after 2 requests, not 6.
- Contract: install force-off still precedes the rw mount. `Mode: focused-test` — the existing
  ordering tests at `test_install.py` (two `events.index("destroy") < events.index("inject")`
  sites and one `events[0] == "destroy"` site) now read `"shutdown"`: `_EventDomain` also appends `"shutdown"` to
  `events`. Red: `ValueError` from `events.index("destroy")` once the clean path lands.
- Contract: force-off. `Mode: focused-test` — `test_force_off_shuts_down_cleanly` (running →
  `["shutdown"]`), `test_force_off_skips_a_shut_off_domain` (→ `[]`),
  `test_force_off_destroy_error_is_infrastructure_failure` (paused + `raise_on destroy`).
- Contract: error mapping. Existing `test_boot_powercycle_error_is_infrastructure_failure_naming_the_verb`
  switches the fake to `run_state=VIR_DOMAIN_PAUSED` so it still reaches `destroy`.

Steps
1. `fakes.py` — add to `FakeDomain`:

```python
run_state: int = libvirt.VIR_DOMAIN_RUNNING  # state() while active
honours_shutdown: bool = True  # shutdown() takes the domain off at once


def shutdown(self) -> int:
    self.calls.append("shutdown")
    self._maybe_raise("shutdown")
    if self.honours_shutdown:
        self.active = False
    return 0


def state(self, flags: int = 0) -> list[int]:
    return [self.run_state if self.active else libvirt.VIR_DOMAIN_SHUTOFF, 0]
```

2. Write the Task 1 tests. Add a `_Clock` test helper (`now: float = 0.0`, `sleeps: list[float]`;
   `sleep(s)` appends `s` and adds it to `now`; `__call__` returns `now`). `_install` gains
   `clock: _Clock | None = None`, defaults it to a fresh `_Clock()`, and passes
   `sleep=clock.sleep, clock=clock` to `LocalLibvirtInstall`. Update
   `test_boot_powercycles_running_domain_then_readiness` and
   `test_boot_truncates_console_only_after_destroy` to expect `shutdown` in place of `destroy`.
   Run the green command; expect failures naming `shutdown`/`state`.
3. `install.py` — protocol gains `def shutdown(self) -> int: ...` and
   `def state(self, flags: int = 0) -> Sequence[object]: ...` (the binding annotates `state` as
   `str` but returns `[state, reason]`; `Sequence[object]` admits both). Add:

```python
# ADR-0679: request a clean shutdown so the guest flushes its writes; destroy is the fallback.
_CLEAN_SHUTDOWN_BASE_S = 60.0
_SHUTDOWN_POLL_S = 1.0
_SHUTDOWN_RESEND_S = 10.0
_HONOURS_SHUTDOWN = frozenset(
    {libvirt.VIR_DOMAIN_RUNNING, libvirt.VIR_DOMAIN_BLOCKED, libvirt.VIR_DOMAIN_SHUTDOWN}
)


def _power_off(
    domain: _LibvirtDomain,
    domain_name: str,
    accel: str | None,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> None:
    """Stop the domain, cleanly when the guest can honour a request, else by ``destroy``.

    Raises:
        libvirt.libvirtError: from ``state()`` or ``destroy()``; the caller maps it.
    """
    state = domain.state()[0]
    if state == libvirt.VIR_DOMAIN_SHUTOFF:
        return
    if state not in _HONOURS_SHUTDOWN:
        _log.warning("power-off %s: destroy-state (domain state %s)", domain_name, state)
        domain.destroy()
        return
    bound_s = _CLEAN_SHUTDOWN_BASE_S * tcg_deadline_multiplier(accel)
    start = clock()
    requested_at: float | None = None
    while clock() - start < bound_s:
        if requested_at is None or clock() - requested_at >= _SHUTDOWN_RESEND_S:
            first = requested_at is None
            requested_at = clock()
            if not _request_shutdown(domain, domain_name, first=first):
                domain.destroy()
                return
        sleep(_SHUTDOWN_POLL_S)
        if domain.state()[0] == libvirt.VIR_DOMAIN_SHUTOFF:
            _log.info("power-off %s: clean after %.1f s", domain_name, clock() - start)
            return
    _log.warning("power-off %s: destroy-timeout after %.1f s", domain_name, clock() - start)
    domain.destroy()


def _request_shutdown(domain: _LibvirtDomain, domain_name: str, *, first: bool) -> bool:
    """Send a shutdown request; only a refused first request is a failure."""
    try:
        domain.shutdown()
    except libvirt.libvirtError:
        if first:
            _log.warning("power-off %s: destroy-refused", domain_name, exc_info=True)
            return False
        _log.debug("power-off %s: shutdown re-send refused; still waiting", domain_name)
    return True
```
4. `LocalLibvirtBooter.__init__` and `LocalLibvirtInstall.__init__` gain
   `sleep: Callable[[float], None] = time.sleep` and `clock: Callable[[], float] =
   time.monotonic` (the facade forwards both). `boot()` calls
   `self._power_cycle(domain, domain_name, system_id, accel)`; `_power_cycle` replaces
   `if domain.isActive(): domain.destroy()` with `_power_off(domain, domain_name, accel,
   self._sleep, self._clock)` inside the same `try`. `force_off_if_active(self, system_id, *, accel=None)`
   replaces its `isActive`/`destroy` with the same call inside its existing `try`. Update the
   module docstring and `boot()`'s to describe the clean shutdown and cite ADR-0679.
5. Green command passes; `just lint`, `just type`; commit
   `fix(local-libvirt): shut guests down cleanly before a power-cycle (#2757)`.

## Task 2 — carry accel to the install force-off

**Verification**
- Contract: install force-off scales by the System accel. `Mode: focused-test` —
  `test_install_force_off_uses_request_accel` in `test_install.py`: a kdump install with an
  ignoring `_EventDomain` and `accel="kvm"` records 60 sleeps on the `_Clock` (red:
  `force_off_if_active` gets no accel, so 600). Green: `just test-verbose tests/providers/local_libvirt/test_install.py -k accel`.
- Contract: handler fills `InstallRequest.accel`. `Mode: focused-test` —
  `test_install_plan_checks_measured_modules_before_provider` builds its System as
  `SimpleNamespace(id=system_id, accel="kvm")` and asserts `plan.request.accel == "kvm"`. Red:
  `InstallRequest` has no `accel`. Green: `just test-verbose tests/jobs/handlers/test_runs_install.py`.

Steps
1. Write both tests; run; see them fail.
2. `ports/lifecycle.py`: add `accel: str | None = None` after `artifact_versions`, with a
   one-line comment citing ADR-0341/0679.
3. `jobs/handlers/runs/install.py` `_build_install_plan`: `accel=system.accel` in `InstallRequest`.
4. `install.py` `_inject_built_modules` gains `accel: str | None` and calls
   `self._booter.force_off_if_active(system_id, accel=accel)`; its caller passes `request.accel`.
5. Green; lint; type; commit `feat(runs): pass the System accel to the install force-off (#2757)`.

## Task 3 — live regression test

**Verification**
- Contract: after provision → install (console) → boot, sshd answers and host keys are non-empty.
  `Mode: task-test-not-applicable` for CI — it is a `live_vm` test that needs a KVM host, the live
  stack, `KDIVE_GUEST_IMAGE` and `KDIVE_KERNEL_SRC`; its proof is the operator run recorded in
  the PR: 3 runs on the branch plus one required control run on `main` on the same host. If
  `main` passes there, the PR says the test is a smoke test on that host class.

Steps
1. Create `tests/integration/test_first_boot_host_keys_live.py`, reusing the preflight, profile,
   allocate/provision/run/upload/install/boot phases of
   `tests/integration/test_console_parts_live.py` (project `first-boot-keys-proof`; the profile
   carries no `crashkernel` and no `debug`, so the method resolves to `console`). Do not wait
   between provision `ready` and `runs.install`.
2. After boot, SSH as `root` over the recorded loopback port with the System's bootstrap key
   (same argv as `_emit_proof_lines`), retrying for up to 60 s, and run
   `ls /etc/ssh/ssh_host_*_key >/dev/null && ! find /etc/ssh -maxdepth 1 -name 'ssh_host_*' -size 0 | grep -q .`.
   Assert exit 0 with a message naming both failure readings (sshd did not answer / a 0-byte key).
3. Release the allocation. `just lint`, `just type`; collect-only check:
   `uv run python -m pytest tests/integration/test_first_boot_host_keys_live.py --collect-only -q -m live_vm`
   → `1 test collected`. Commit `test(live): prove host keys survive the first runs.boot (#2757)`.
4. `docs/adr/0030-install-boot-plane.md` §6: append one `> **Amended by ADR-0679 (#2757):**`
   blockquote, linked like the repo's other amendment banners, saying boot stops a running domain
   with a bounded clean shutdown and `destroy` is the fallback. Committed with the design docs.
