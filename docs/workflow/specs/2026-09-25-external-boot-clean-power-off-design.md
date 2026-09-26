# Clean power-off for local-libvirt external-boot stops

Issue #2780, a follow-up to #2757. Decision record:
[ADR-0681](../../adr/0681-external-boot-clean-power-off.md), which amends
[ADR-0679](../../adr/0679-local-libvirt-clean-power-off.md) and
[ADR-0583](../../adr/0583-external-run-boot-uses-prepared-recovery-points.md).

## Problem

`_ConcreteSession.stop_and_require_inactive()` in `lifecycle/boot/session.py` calls
`domain.destroy()` on an active domain. External-boot preparation uses it to stop the user's
running source System before reading its overlay into the recovery archive
(`external_boot.py`, `prepare`), and recovery uses it to stop the running target before handing
the same overlay back to the source (`_stop_for_recovery`). A hard kill loses writes still in the
guest page cache, the loss class ADR-0679 fixed for `runs.boot`.

## Requirements

1. `_power_off` and `_request_shutdown` move from `install.py` to a new leaf module
   `lifecycle/power.py` as `power_off(domain, domain_name, bound_s, sleep, clock)` and
   `clean_shutdown_bound_s(accel)`. The helper's state rules, re-send, fallback and log lines are
   unchanged. `install.py` calls `power_off(..., clean_shutdown_bound_s(accel), ...)`; its
   behaviour is unchanged and its tests change only the logger name they capture.
2. `stop_and_require_inactive(*, clean: bool)`. On an active domain, `clean=True` calls
   `power_off` with the session bound and `clean=False` logs `destroy-unready` (WARNING) and calls
   `destroy()`. Both then call `require_inactive()`. An inactive domain is untouched.
3. The session bound is `min(clean_shutdown_bound_s(accel), 120.0)` seconds, where `accel` is
   `"kvm"` when the inactive definition read at `open()` has `type="kvm"` and `None` otherwise.
   The session takes injected `sleep`/`clock` (defaults `time.sleep`/`time.monotonic`).
4. Preparation (`_RealLocalExternalBootOperation.prepare`) calls
   `stop_and_require_inactive(clean=True)`.
5. `_stop_for_recovery` calls `stop_and_require_inactive(clean=metadata.phase ==
   "target-defined")`. Its existing post-stop checks are unchanged.
6. `restore_power(self) -> None` replaces `restore_power(prior)`: it refuses with an open guest
   context and starts the domain when inactive. The one caller (`_abort_preparation`) drops its
   argument; the Protocol changes to match.
7. One comment each, citing ADR-0679 and the reason the stop stays hard, at
   `retrieve/guestfs.py` `_force_off_domain`, `rootfs/customization_boot.py` `_force_off`,
   `session.py` `_ConcreteSystemTeardownSession.destroy`, and `provisioning.py`
   `_teardown_domain`. No behaviour change there.

## Failure model

1. **Actors and deployments** — the local-libvirt external-boot authority host process, driven by
   the external-boot activate and recover jobs, on x86_64 KVM hosts and ppc64le under TCG.
2. **Invariants and assets at stake**
   - guest writes to the persistent System overlay made before preparation or a release;
   - the recovery archive captured from that overlay;
   - the authority client deadline (default 5 min) that recovery must finish within;
   - the session's post-stop XML/inactivity checks.
3. **Accepted failure classes**
   - A ready target that ignores the request costs up to 60 s (KVM) or 120 s (other) before
     `destroy()`; bounded and stated in ADR-0681.
   - A TCG guest still shutting down at 120 s loses unflushed writes: the pre-change behaviour.
   - One `shutdown()` blocking in libvirt's guest-agent path can overrun the bound by up to 60 s
     (ADR-0679); the cap still leaves 120 s of the default deadline.
   - A domain without a `type` attribute takes the TCG multiplier, so the capped 120 s bound:
     libvirt always writes `type`, so only test doubles reach this.
4. **Covered elsewhere**
   - operator `power off` (`lifecycle/control.py`, ADR-0028): operator-excluded, reported as a
     follow-up candidate;
   - crash harvest, customization and teardown behaviour: operator-excluded, comments only;
   - `ready` before first boot completes: #2771.

## Validation

- Unit, session (`tests/providers/local_libvirt/lifecycle/boot/test_session.py`, doubles in
  `session_support.py`): clean stop sends `shutdown` and no `destroy`; `clean=False` destroys with
  no `shutdown` and logs `destroy-unready`; a guest that ignores the request is destroyed at
  60 s for `type="kvm"` and at 120 s otherwise on a fake clock; `restore_power()` starts an
  inactive domain and leaves an active one alone.
- Unit, provider (`tests/providers/local_libvirt/test_external_boot.py`): preparation passes
  `clean=True`; recovery passes `clean=True` in `target-defined` and `clean=False` in an earlier
  phase.
- Unit, install (`tests/providers/local_libvirt/test_install.py`): the existing power-off tests
  pass with `_POWER_OFF_LOGGER` pointed at the `power` module.
- Live on a KVM host: provision a System, write a file in the guest without `sync`, run
  external-boot preparation, then release and recover; the file is present on the restored
  source and the authority host log shows `power-off ...: clean`. A recovery against a target
  that never became ready logs `destroy-unready` and finishes inside the recovery deadline.
