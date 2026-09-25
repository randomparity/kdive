# Clean power-off for local-libvirt boot and install

Issue #2757 (piece A). Decision record: [ADR-0679](../../adr/0679-local-libvirt-clean-power-off.md),
which amends [ADR-0030](../../adr/0030-install-boot-plane.md) §6.

## Problem

`LocalLibvirtBooter._power_cycle` (`runs.boot`) and `LocalLibvirtBooter.force_off_if_active`
(the install force-off before module injection) call `domain.destroy()` on a running domain. A
hard kill discards guest writes still in the page cache. On a new System the first boot writes
its SSH host keys and cloud-init's once-per-instance markers; a `runs.boot` a few seconds later
leaves the keys as 0-byte files with the markers intact, so sshd never starts on that System
again (reproduced 3 of 3 in the PR #2741 root-cause comment).

## Requirements

1. Both sites stop a running domain through one helper that requests `domain.shutdown()` and
   waits, bounded, for `state()` to read `SHUTOFF`.
2. A domain in a state that cannot honour the request goes straight to `destroy()`, with no
   wait. Honouring states: `RUNNING`, `BLOCKED`, `SHUTDOWN`. `SHUTOFF` needs no action. Every
   other `virDomainState` value destroys at once.
3. The wait expiring, or `shutdown()` raising `libvirtError`, falls back to `destroy()`.
4. The wait is `60 s × tcg_deadline_multiplier(accel)` (ADR-0341), polled at 1 s, with the
   request re-sent every 10 s. `boot()` passes the System accel it already receives; the install
   passes the new `InstallRequest.accel`, which the `runs.install` handler fills from the System
   row. A `None` accel takes the TCG multiplier.
5. One log line per power-off names the path: `clean` (INFO, with seconds taken),
   `destroy-state` (WARNING, with the state), `destroy-timeout` (WARNING, with the bound), or
   `destroy-refused` (WARNING).
6. `_power_cycle` keeps the ADR-0576 order: domain off, then `prepare_console`, then `create()`.
7. Error mapping is unchanged: a `libvirtError` from `state()` or `destroy()` in `_power_cycle`
   is `INFRASTRUCTURE_FAILURE` "libvirt error power-cycling domain"; in `force_off_if_active` it
   is the existing force-off `INFRASTRUCTURE_FAILURE`. An absent domain in `force_off_if_active`
   stays a no-op.
8. A `live_vm` regression test provisions a System, runs `runs.install` with the `console`
   method and `runs.boot` at once, then asserts sshd answers over the loopback SSH forward and
   every `/etc/ssh/ssh_host_*` file is non-empty.

## Design

`install.py` gains module constants `_CLEAN_SHUTDOWN_BASE_S = 60.0`,
`_SHUTDOWN_POLL_S = 1.0`, `_SHUTDOWN_RESEND_EVERY = 10` (polls), and a private
`_power_off(domain, domain_name, accel, sleep) -> None` that implements requirements 1–5. It
raises `libvirtError` from `state()`/`destroy()` to its caller, which maps it (requirement 7).
The loop is probe-count bounded (`ceil(bound / poll)` probes), like
`retrieve/guestfs.py::_poll_until_settled`, so the injected `sleep` is the only clock and the
unit tests run without real waits.

`LocalLibvirtBooter` and `LocalLibvirtInstall` take `sleep: Callable[[float], None] = time.sleep`.
`force_off_if_active(system_id, *, accel=None)` gains the accel argument;
`LocalLibvirtInstaller._inject_built_modules` passes `request.accel`. The `_LibvirtDomain`
protocol gains `shutdown()` and `state()`.

`InstallRequest` gains `accel: str | None = None` as its last field. The remote-libvirt and
fault-inject installers ignore it.

## Failure model

1. **Actors and deployments** — the lifecycle worker on a local-libvirt host (x86_64 KVM,
   ppc64le under TCG, native POWER), driven by `runs.boot` and `runs.install` jobs.
2. **Invariants and assets at stake** — guest filesystem state written before a kdive
   power-off; the boot readiness window and job latency; the ADR-0576 console window.
3. **Accepted failure classes**
   - A running guest that ignores the request (panicked or hung kernel, no ACPI button or EPOW
     handler) costs the full bound before `destroy()`: bounded at 60 s KVM / 600 s default TCG
     and logged as `destroy-timeout`.
   - A guest that is still shutting down at the bound loses whatever it had not flushed; that
     is the pre-change behaviour, bounded to that guest.
   - The domain going `SHUTOFF` between two libvirt calls can make `destroy()` raise
     `OPERATION_INVALID`, a retryable `INFRASTRUCTURE_FAILURE`; the same race exists today
     between `isActive()` and `destroy()`.
4. **Covered elsewhere**
   - `ready` before first boot completes — #2771.
   - Other local-libvirt `destroy()` sites (external boot `session.py`, `retrieve/guestfs.py`,
     `rootfs/customization_boot.py`) — follow-up candidates, operator-excluded.
   - Remote-libvirt power paths — out of scope; they already use a guest-initiated reboot.

## Validation

- Unit (`tests/providers/local_libvirt/test_install.py`, fake in `fakes.py`): clean path
  (`shutdown` then `prepare`, `create`, no `destroy`); each non-honouring state destroys with no
  `shutdown`; timeout destroys after exactly `ceil(bound/poll)` probes with a re-send every 10
  probes; `shutdown()` raising destroys; `SHUTOFF` does nothing; TCG accel scales the probe
  count; force-off takes the same paths; log path names asserted with `caplog`.
- Handler (`tests/jobs/handlers/test_runs_install.py`): `InstallRequest.accel` carries the
  System's accel.
- Live (`tests/integration/test_first_boot_host_keys_live.py`, `live_vm`): requirement 8,
  run on a KVM host; the worker log shows the `clean` path.
