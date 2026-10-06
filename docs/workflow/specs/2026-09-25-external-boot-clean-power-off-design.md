# Clean power-off for local-libvirt external-boot stops

Issue #2780, a follow-up to #2757. Decision record:
[ADR-0681](../../adr/0681-external-boot-clean-power-off.md), which amends
[ADR-0679](../../adr/0679-local-libvirt-clean-power-off.md) and
[ADR-0583](../../adr/0583-external-run-boot-uses-prepared-recovery-points.md).

## Problem

`_ConcreteSession.stop_and_require_inactive()` in `lifecycle/boot/session.py` calls
`domain.destroy()` on an active domain. External-boot preparation uses it to stop the user's
running source System before reading its overlay into the recovery archive
(`_RealLocalExternalBootOperation.prepare`), and recovery uses it to stop the running target
before handing the same overlay back to the source (`_stop_for_recovery`). A hard kill loses
writes still in the guest page cache, the loss class ADR-0679 fixed for `runs.boot`.

## Requirements

1. `_power_off` and `_request_shutdown` move from `install.py` to a new leaf module
   `lifecycle/power.py` as `power_off(domain, domain_name, bound_s, sleep, clock)` and
   `clean_shutdown_bound_s(accel)`. The helper's state rules, re-send, fallback and log lines are
   unchanged. `install.py` calls `power_off(..., clean_shutdown_bound_s(accel), ...)`; its
   behaviour is unchanged and its tests change only the logger name they capture.
2. `LocalExternalBootSessionFactory.open` records `kvm = inactive_root.get("type") == "kvm"` and
   the session takes injected `sleep`/`clock` (defaults `time.sleep`/`time.monotonic`).
3. `stop_and_require_inactive(*, mode: StopMode)` with
   `StopMode = Literal["clean", "clean-on-kvm", "destroy"]`. On an active domain: `clean` calls
   `power_off` with `clean_shutdown_bound_s("kvm")` (60 s) on KVM and 120 s otherwise;
   `clean-on-kvm` does the same on KVM and otherwise logs `destroy-unaccelerated` (WARNING) and
   destroys; `destroy` logs `destroy-unready` (WARNING) and destroys. All paths then call
   `require_inactive()`. An inactive domain is untouched.
4. Preparation (`_RealLocalExternalBootOperation.prepare`) uses `mode="clean"`.
5. `_stop_for_recovery` uses `"clean-on-kvm"` when `metadata.phase == "target-defined"`, else
   `"destroy"`. Its existing post-stop checks are unchanged. (Operator decision, option B.)
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
   - the 5-minute recovery deadline; missing it ends `recovery_failed` (ADR-0583);
   - the session's post-stop XML/inactivity checks.
3. **Accepted failure classes**
   - A KVM guest that ignores the request costs 60 s, plus up to 60 s when `shutdown()` blocks in
     the guest-agent path (ADR-0679), before `destroy()`; at most 120 s of the recovery deadline.
   - A TCG recovery stop stays hard and can lose unflushed target writes: the pre-change
     behaviour, kept because no wait can be shown to fit the unscaled recovery deadline
     (ADR-0681; operator decision, option B).
   - A TCG preparation stop costs up to 120 s plus the agent overrun; a miss of the per-call
     deadline is a retryable `INFRASTRUCTURE_FAILURE`.
   - A domain definition without `type="kvm"` is treated as not KVM; libvirt always writes `type`.
   - A preparation abort while the source is still shutting down starts nothing and fails with
     `provider_conflict` (ADR-0681 Consequences); the window already existed before the change.
4. **Covered elsewhere**
   - operator `power off` (`lifecycle/control.py`, ADR-0028): operator-excluded, reported as a
     follow-up candidate;
   - crash harvest, customization and teardown behaviour: operator-excluded, comments only;
   - `ready` before first boot completes: #2771.

## Validation

- Unit, session (`tests/providers/local_libvirt/lifecycle/boot/test_session.py`, doubles in
  `session_support.py`): a KVM clean stop sends `shutdown` and no `destroy`; a KVM guest that
  ignores it is destroyed after 60 fake-clock seconds, a non-KVM one after 120 under `clean`;
  `destroy`, and `clean-on-kvm` on a non-KVM domain, each destroy with no `shutdown` and log
  their path; `restore_power()` starts an inactive domain and
  leaves an active one alone.
- Unit, provider (`tests/providers/local_libvirt/test_external_boot.py`): preparation passes
  `clean`; recovery passes `clean-on-kvm` in `target-defined` and `destroy` in
  `module-restored`.
- Unit, install (`tests/providers/local_libvirt/test_install.py`): the existing power-off tests
  pass with `_POWER_OFF_LOGGER` pointed at the `power` module.
- Live, direct-provider arm on a KVM host: a real provisioned System, guest writeback disabled
  (`vm.dirty_writeback_centisecs=0`, `vm.dirty_expire_centisecs=360000`) before an unsynced write,
  a real session `stop_and_require_inactive(mode="clean")`, then the file read from the overlay
  through the session's libguestfs guest: present. Negative control on the same System with
  `mode="destroy"`: the write is absent or empty. The full authority-carrier arm
  (`tests/live_vm/test_installed_local_authority.py`) runs where the authority service is
  installed; where it is not, the PR says so.
