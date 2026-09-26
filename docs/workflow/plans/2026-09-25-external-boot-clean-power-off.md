# Clean power-off for local-libvirt external-boot stops — plan

Goal: the local-libvirt external-boot preparation and recovery stops ask a running guest to shut
down before `destroy()`, within a bound that fits the external-boot authority deadline (#2780).

Architecture: the ADR-0679 helper moves from `lifecycle/install.py` to a leaf module
`lifecycle/power.py` and takes an explicit bound. The external-boot session calls it from
`stop_and_require_inactive(clean=True)`, with a bound capped at 120 s and an accelerator read
from the domain XML. Recovery passes `clean=False` unless the target reached `target-defined`.
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
| `src/kdive/providers/local_libvirt/lifecycle/boot/session.py` | `_Domain` gains `state`/`shutdown`; session `accel`, `sleep`, `clock`; `stop_and_require_inactive(*, clean)`; `restore_power()`; teardown comment |
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

## Task 2 — session stops cleanly

Interfaces: consumes Task 1's `power_off` and `clean_shutdown_bound_s`. Provides
`LocalExternalBootSession.stop_and_require_inactive(self, *, clean: bool) -> None` and
`restore_power(self) -> None`; `LocalExternalBootSessionFactory(..., sleep=time.sleep,
clock=time.monotonic)`.

**Verification** (all `Mode: focused-test`, file
`tests/providers/local_libvirt/lifecycle/boot/test_session.py`, green
`just test-verbose tests/providers/local_libvirt/lifecycle/boot/test_session.py`):
- clean stop — `test_clean_stop_requests_shutdown`: active domain, `clean=True`; events contain
  `domain.shutdown`, not `domain.destroy`; domain inactive. Red: `TypeError` on `clean=`.
- unready stop — `test_unready_stop_destroys_at_once`: `clean=False`; `domain.destroy`, no
  `domain.shutdown`; caplog WARNING `destroy-unready`.
- bound — `test_stop_bound_is_capped`, parametrized `(xml type, expected sleeps)`:
  `("kvm", 60)`, `("qemu", 120)`: domain with `honours_shutdown=False`; fake clock whose `sleep`
  advances it; `clean=True`; `len(clock.sleeps) == expected` and the last event is
  `domain.destroy`. Red before the cap: `("qemu", 600)`.
- `restore_power()` — the existing lifecycle test drops its `"inactive"` calls and asserts
  `restore_power()` on an active domain records no `domain.create`.

Steps:
1. `session_support.Domain`: add `honours_shutdown: bool = True` (constructor kwarg),
   `state(self, flags=0)` returning `[libvirt.VIR_DOMAIN_RUNNING if self.active else
   libvirt.VIR_DOMAIN_SHUTOFF, 0]`, and `shutdown()` appending `domain.shutdown` and clearing
   `active` when `honours_shutdown`. Let `_xml(domain_type=None)` emit `<domain type="...">`.
2. Write the tests above; run; expect red.
3. `session.py`: `_Domain` gains `shutdown` and `state`. `factory.open` reads
   `accel = "kvm" if inactive_root.get("type") == "kvm" else None` and passes `accel`, `sleep`,
   `clock` into `_ConcreteSession`. Add `_EXTERNAL_BOOT_STOP_CAP_S = 120.0` with a comment citing
   ADR-0681, and `_log = logging.getLogger(__name__)` (the module has no logger yet). Replace
   `stop_and_require_inactive`:

   ```python
   def stop_and_require_inactive(self, *, clean: bool) -> None:
       domain = self._require_open_domain()
       if _active(domain):
           name = domain_name_for(self._system_id)
           if clean:
               bound = min(clean_shutdown_bound_s(self._accel), _EXTERNAL_BOOT_STOP_CAP_S)
               power_off(domain, name, bound, self._sleep, self._clock)
           else:
               _log.warning("power-off %s: destroy-unready", name)
               domain.destroy()
       self.require_inactive()
   ```

   Replace `restore_power` with a no-argument method that calls `_require_no_guest_context()` and
   `_start_domain()` when inactive. Update the Protocol signatures. Add the teardown comment on
   `_ConcreteSystemTeardownSession.destroy`: the overlay is reclaimed, so unflushed writes are
   not read again (ADR-0679).
4. Green command passes. Commit `fix(local-libvirt): stop external-boot guests cleanly`.

## Task 3 — callers choose the stop

Interfaces: consumes Task 2's `stop_and_require_inactive(*, clean)` and `restore_power()`.

**Verification** (`Mode: focused-test`, `tests/providers/local_libvirt/test_external_boot.py`,
green `just test-verbose tests/providers/local_libvirt/test_external_boot.py`):
- preparation — the preparation double records `clean`; a preparation test asserts `[True]`.
  Red: `TypeError` from the double while `prepare` passes no argument.
- recovery — a recovery test through the restart double with the target running asserts
  `clean is True` in `target-defined` and `False` in `module-restored`.
- hard-site comments — `Mode: task-test-not-applicable`: comments only; no executable behaviour
  changes, so no test can fail on them.

Steps:
1. Doubles: both `stop_and_require_inactive` doubles take `*, clean: bool` and append it to a
   `stops` list; the `restore_power` double drops its argument; the two monkeypatched wrappers
   (`original_stop`) forward `**kwargs`.
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

`Mode: task-test-not-applicable` for automation: the proof drives a live stack by hand and is
recorded in the PR. Redeploy HEAD, confirm the deployed build contains `power.py`. Provision a
System, write `/root/kdive-2780` without `sync`, run external-boot preparation, then release and
recover; the file exists on the restored source and the authority log shows
`power-off ...: clean`. If a target can be made to miss readiness, its recovery logs
`destroy-unready` inside the deadline; otherwise record that arm as not run.
