# Clean power-off for local-libvirt external-boot stops — plan

Goal: the local-libvirt external-boot preparation and recovery stops ask a running guest to shut
down before `destroy()` on KVM, within a bound that fits the external-boot recovery deadline
(#2780).

Architecture: the ADR-0679 helper moves from `lifecycle/install.py` to a leaf module
`lifecycle/power.py` and takes an explicit bound. The external-boot session calls it from
`stop_and_require_inactive(clean=True)` with the 60 s KVM bound when the domain XML says
`type="kvm"`, and destroys otherwise. Recovery passes `clean=False` unless the target reached
`target-defined`.
Spec: [2026-09-25-external-boot-clean-power-off-design.md](../specs/2026-09-25-external-boot-clean-power-off-design.md);
decision: [ADR-0681](../../adr/0681-external-boot-clean-power-off.md).

Tech stack: Python 3.14, libvirt-python, pytest; `uv` + `just`.

Expected implementation size: 190–260 changed lines (M) — power module ~75 (moved), install
wiring ~10, session ~35, external_boot ~6, comments ~8, session doubles ~20, session tests ~70,
provider-test doubles and tests ~30.

## Global Constraints

- Ruff line length 100; `just format`, then `just lint`, `just type` (whole tree),
  `just test-changed` green before each commit. Conventional commits.
- No new dependency, setting or env var. No request, port or MCP schema change.
- Prose avoids "critical", "robust", "comprehensive", "elegant".
- Deferrals: none.

## File map

| File | Change |
|------|--------|
| `src/kdive/providers/local_libvirt/lifecycle/power.py` | new: `PowerDomain`, `clean_shutdown_bound_s`, `power_off` (moved) |
| `src/kdive/providers/local_libvirt/lifecycle/install.py` | drop the helper and its constants; call `power.power_off` |
| `src/kdive/providers/local_libvirt/lifecycle/boot/session.py` | `_Domain` gains `state`/`shutdown`; session `kvm`, `sleep`, `clock`; `stop_and_require_inactive(*, clean)`; `restore_power()`; teardown comment |
| `src/kdive/providers/local_libvirt/lifecycle/boot/external_boot.py` | pass `clean`; `restore_power()` |
| `retrieve/guestfs.py`, `rootfs/customization_boot.py`, `lifecycle/provisioning.py` | one comment each |
| `tests/providers/local_libvirt/test_install.py` | `_POWER_OFF_LOGGER` names the power module |
| `tests/providers/local_libvirt/lifecycle/boot/session_support.py` | `Domain.state`/`shutdown`, `honours_shutdown` |
| `tests/providers/local_libvirt/lifecycle/boot/test_session.py` | stop and `restore_power` tests |
| `tests/providers/local_libvirt/test_external_boot.py` | doubles take `clean`; recovery/preparation `clean` tests |

## Task 1 — move the helper to `lifecycle/power.py`

Interfaces (later tasks rely on these exact signatures):

```python
class PowerDomain(Protocol):
    def destroy(self) -> int: ...
    def shutdown(self) -> int: ...
    def state(self, flags: int = 0) -> Sequence[object]: ...

def clean_shutdown_bound_s(accel: str | None) -> float  # 60.0 * tcg_deadline_multiplier(accel)
def power_off(domain: PowerDomain, domain_name: str, bound_s: float,
              sleep: Callable[[float], None], clock: Callable[[], float]) -> None
```

**Verification**
- Contract: install power-off behaviour unchanged. `Mode: focused-test` — the existing
  `test_install.py` power-off tests (`-k "power_off or shutdown or bound"`). Red: after the move
  and before the logger constant changes, `test_boot_shuts_down_cleanly_then_creates` fails on
  its `clean after 1.0 s` caplog assertion. Green:
  `just test-verbose tests/providers/local_libvirt/test_install.py`.

Steps:
1. Create `power.py`: module docstring citing ADR-0679 and ADR-0681; move `_CLEAN_SHUTDOWN_BASE_S`,
   `_SHUTDOWN_POLL_S`, `_SHUTDOWN_RESEND_S`, `_HONOURS_SHUTDOWN`, `_request_shutdown` verbatim;
   move `_power_off` as `power_off`, replacing `accel` with `bound_s` and deleting the line that
   computed the bound. Add `clean_shutdown_bound_s`. Logger: `logging.getLogger(__name__)`.
2. In `install.py` delete the moved code, import `clean_shutdown_bound_s, power_off`, and at both
   call sites write `power_off(domain, domain_name, clean_shutdown_bound_s(accel), self._sleep,
   self._clock)`. Drop the now-unused `tcg_deadline_multiplier` import only if nothing else uses
   it (`boot()` still does).
3. Run the green command: expect the caplog failure; set `_POWER_OFF_LOGGER =
   "kdive.providers.local_libvirt.lifecycle.power"`; rerun, all pass. Commit
   `refactor(local-libvirt): move the clean power-off helper to a leaf module`.

## Task 2 — session stops cleanly on KVM

Interfaces: consumes Task 1's `power_off` and `clean_shutdown_bound_s`. Provides
`LocalExternalBootSession.stop_and_require_inactive(self, *, clean: bool) -> None` and
`restore_power(self) -> None`; `LocalExternalBootSessionFactory(..., sleep=time.sleep,
clock=time.monotonic)`.

**Verification** (all `Mode: focused-test`, file
`tests/providers/local_libvirt/lifecycle/boot/test_session.py`, green
`just test-verbose tests/providers/local_libvirt/lifecycle/boot/test_session.py`):
- KVM clean stop — `test_kvm_clean_stop_requests_shutdown`: `type="kvm"` domain, active,
  `clean=True`; events contain `domain.shutdown`, not `domain.destroy`. Red: `TypeError` on
  `clean=`.
- KVM bound — `test_kvm_stop_destroys_after_60_s`: `honours_shutdown=False`; a fake clock whose
  `sleep` advances it; `len(clock.sleeps) == 60` and the last event is `domain.destroy`.
- unready — `test_unready_stop_destroys_at_once`: KVM, `clean=False`; `domain.destroy`, no
  `domain.shutdown`; caplog WARNING `destroy-unready`.
- not KVM — `test_unaccelerated_stop_destroys_at_once`: no `type` attribute, `clean=True`;
  `domain.destroy`, no `domain.shutdown`; caplog WARNING `destroy-unaccelerated`.
- `restore_power()` — five existing call sites change: `restore_power("running")` at the
  guest-context refusal and in the lifecycle test become `restore_power()`; the two
  `restore_power("inactive")` calls before `cleanup_payloads` and in
  `test_session_snapshots_ownership_after_lane_pin` are deleted (they were no-ops on an inactive
  domain); the one that powered the domain off before `assert not domain.active` becomes
  `stop_and_require_inactive(clean=False)`. Add `assert events.count("domain.create") == 1` after
  a second `restore_power()` on the active domain.

Steps:
1. `session_support.Domain`: add constructor kwarg `honours_shutdown: bool = True`,
   `state(self, flags=0)` returning `[libvirt.VIR_DOMAIN_RUNNING if self.active else
   libvirt.VIR_DOMAIN_SHUTOFF, 0]`, and `shutdown()` appending `domain.shutdown` and clearing
   `active` when `honours_shutdown`. `_xml(domain_type: str | None = None)` emits
   `<domain type="...">` when given.
2. Write the tests above; run; expect red.
3. `session.py`: add `import logging`, `import time`, `_log = logging.getLogger(__name__)`, and
   import `clean_shutdown_bound_s, power_off` from `..power`. `_Domain` gains `shutdown()` and
   `state(flags: int = 0) -> Sequence[object]`. The factory takes `sleep`/`clock`; `open` passes
   `kvm=inactive_root.get("type") == "kvm"`, `sleep`, `clock` into `_ConcreteSession`. Replace
   `stop_and_require_inactive`:

   ```python
   def stop_and_require_inactive(self, *, clean: bool) -> None:
       domain = self._require_open_domain()
       if _active(domain):
           name = domain_name_for(self._system_id)
           if clean and self._kvm:  # ADR-0681: TCG keeps the hard stop
               power_off(domain, name, clean_shutdown_bound_s("kvm"), self._sleep, self._clock)
           else:
               path = "destroy-unaccelerated" if clean else "destroy-unready"
               _log.warning("power-off %s: %s", name, path)
               domain.destroy()
       self.require_inactive()
   ```

   Replace `restore_power` with a no-argument method: `_require_no_guest_context()`, then
   `_start_domain()` when inactive. Update the Protocol signatures. Comment on
   `_ConcreteSystemTeardownSession.destroy`: the overlay is reclaimed next, so no unflushed
   write is read again (ADR-0679).
4. Green command passes. Commit `fix(local-libvirt): stop external-boot guests cleanly on KVM`.

## Task 3 — callers choose the stop

Interfaces: consumes Task 2's `stop_and_require_inactive(*, clean)` and `restore_power()`.

**Verification** (`Mode: focused-test`, `tests/providers/local_libvirt/test_external_boot.py`,
green `just test-verbose tests/providers/local_libvirt/test_external_boot.py`):
- preparation — the preparation double records `clean`; a preparation test asserts `[True]`.
  Red: `TypeError` from the double while `prepare` passes no argument.
- recovery — through the restart double with the target running, `recover_modules` records
  `clean=True` for phase `target-defined` and `clean=False` for `module-restored`.
- hard-site comments — `Mode: task-test-not-applicable`: comments only; no executable behaviour
  changes, so no test can fail on them.

Steps:
1. Doubles: both `stop_and_require_inactive` doubles take `*, clean: bool` and append it to a
   `stops` list; the `restore_power` double drops its argument; the two monkeypatched wrappers
   of `original_stop` forward `**kwargs`.
2. Write the two tests; run; expect red.
3. `external_boot.py`: `prepare` calls `stop_and_require_inactive(clean=True)`;
   `_stop_for_recovery` calls
   `stop_and_require_inactive(clean=metadata.phase == "target-defined")` with a one-line comment
   citing ADR-0681; `_abort_preparation` calls `restore_power()`.
4. Comments at `guestfs._force_off_domain`, `customization_boot._force_off`,
   `provisioning._teardown_domain`, each citing ADR-0679 and its reason (crash harvest after the
   kdump wait; discarded transient build domain; reclaimed overlay).
5. Green command, then `just lint`, `just type`, `just test-changed`. Commit
   `fix(local-libvirt): recovery stops a ready target cleanly`.

## Task 4 — live proof (fed44-big, KVM)

`Mode: task-test-not-applicable` for automation: the proof drives real hosts by hand and is
recorded in the PR. Redeploy HEAD and confirm the deployed checkout contains `lifecycle/power.py`.
Stop the reconciler for the direct arm and restart it afterwards.
1. Provision a System through MCP and wait for `ready`.
2. In the guest: `sysctl -w vm.dirty_writeback_centisecs=0 vm.dirty_expire_centisecs=360000`,
   then write `/root/kdive-2780-clean` without `sync`.
3. From a scratch script on the host (not committed), open a real
   `LocalExternalBootSessionFactory` session on that System with a stub lane pin and artifact
   root, call `stop_and_require_inactive(clean=True)`, and read the file through
   `session.guest()`. Expect it present with its content, and a `power-off ...: clean` log line.
4. Start the domain, repeat 2 with `/root/kdive-2780-destroy`, and call
   `stop_and_require_inactive(clean=False)`. Expect the file absent or empty (negative control).
5. Tear the System down. If the authority service is installed on the host, also run
   `tests/live_vm/test_installed_local_authority.py`; otherwise record that arm as not run.
