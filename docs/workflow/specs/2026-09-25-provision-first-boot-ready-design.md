# Local-libvirt provision reports ready after the first boot

Issue #2771 (split from #2757). Decision record:
[ADR-0680](../../adr/0680-local-libvirt-provision-ready-after-first-boot.md), which amends
[ADR-0272](../../adr/0272-provision-baseline-kernel-boot.md) Option 2.

## Problem

`LocalLibvirtProvisioning.provision` returns once `domain.create()` returns, and the
`systems.*` handler commits `ready`. The guest's first boot (cloud-init per-instance stage,
SELinux relabel, kdump arming) is still running, so a kdive step that powers the guest off can
interrupt it. `reprovision` calls `provision` and has the same gap.

## Requirements

1. With a readiness seam wired, `provision` waits after define/start until the probe answers.
   The wait runs `ceil(boot_window_polls() * tcg_deadline_multiplier(accel))` polls, where
   `boot_window_polls()` is `ceil(KDIVE_LIBVIRT_BOOT_WINDOW_S / 5)` and `accel` is the value
   `_resolve_guest_arch` already returned in that call. `reprovision` gets the wait through
   `provision`.
2. The probe answering `ok` returns the domain name as today. The window elapsing raises
   `CategorizedError(PROVISIONING_FAILURE)` with details `first_boot="timeout"`; the probe
   answering not-ok (crash signature before the marker, or the domain exited) raises the same
   category with `first_boot="not_ready"`. Both details also carry `system_id` and, when
   observed, the closed `probe_error` and `crash_signature` values `runs.boot` already emits.
3. A `CategorizedError` raised once `provision` has begun define/start destroys and undefines
   the System's domain (best effort: a teardown fault is logged and never replaces the original
   error), then runs the existing ADR-0435 overlay/baseline reclaim.
4. When the domain `defineXML` returns is already active, `provision` skips both the console
   truncate and `create()` and waits on the existing console log. Otherwise it truncates the
   console (ADR-0576) immediately before `create()`. A `create()` that still reports
   `VIR_ERR_OPERATION_INVALID` keeps today's already-running treatment.
5. `LocalLibvirtProvisioning(first_boot_readiness=None)` is the default and never waits;
   `from_env` wires `_real_readiness`. The authority composition does not wire it (ADR-0680 §6).
6. `LocalLibvirtBooter` and provisioning share one poll loop, `poll_readiness`, and one details
   builder, `readiness_failure_details`, in `boot/readiness.py`. `runs.boot` behaviour and its
   error contract are unchanged.
7. The `systems.provision` and `systems.reprovision` tool docstrings state that on local-libvirt
   the job ends, and the System reaches `ready`, only after the guest's first boot emits its
   readiness marker, and that a guest that does not ends `failed` with `provisioning_failure`.
   The generated reference is regenerated.
8. A `live_vm` test provisions a System, and at the moment it reads `ready` requires the
   System's console log to contain the `kdive-ready` marker and sshd to answer with non-empty
   host keys.

## Design

`boot/readiness.py` gains:

- `type Readiness = Callable[[UUID], ReadinessResult]` (moved from `install.py`).
- `boot_window_polls() -> int` (moved from `install.py`'s `_boot_window_polls`, which becomes an
  import alias so existing callers and tests keep the name).
- `class ReadinessOutcome(NamedTuple)`: `result: ReadinessResult | None` (`None` when the window
  elapsed with no answer) and `first_probe_error: ProbeFailure | None`.
- `poll_readiness(readiness, system_id, polls) -> ReadinessOutcome`: the loop body of today's
  `LocalLibvirtBooter._await_ready`, returning instead of raising.
- `readiness_failure_details(system_id, first_probe_error, crash_signature=None)`: today's
  `LocalLibvirtBooter._boot_failure_details`, which becomes a `staticmethod` alias of it.

`LocalLibvirtBooter._await_ready` maps the outcome to its existing `BOOT_TIMEOUT` /
`READINESS_FAILURE` errors.

`provisioning.py`:

- `_LibvirtDomain` gains `isActive() -> int`.
- `__init__` takes `first_boot_readiness: Readiness | None = None`.
- `provision` sets `started = True` immediately before `_define_and_start(xml, system_id)`,
  which now owns the console truncate (`self._files.prepare_console`) and the active check. After
  it, `_await_first_boot(system_id, accel)` runs when the seam is set. The `except
  CategorizedError` arm calls `_best_effort_teardown_domain(domain_name)` when `started`, then the
  existing reclaim.
- `_await_first_boot` computes the poll count, calls `poll_readiness`, and raises per
  requirement 2.

## Failure model

1. **Actors and deployments** — the lifecycle worker running `systems.provision` /
   `systems.reprovision` jobs on a local-libvirt host (x86_64 KVM, ppc64le under TCG); the
   provider-authority process on the same host class, whose provision path shares
   `_define_and_start`.
2. **Invariants and assets at stake** — the `ready` state other tools act on; host RAM and disk
   held by a failed System's domain; the job lease across a wait of up to the scaled window; the
   console log a retry reads.
3. **Accepted failure classes**
   - A guest that never emits the marker holds the job for the full scaled window (900 s KVM,
     9000 s at the default TCG multiplier) before failing. Bounded and stated in ADR-0680.
   - A domain started between the `isActive()` check and `create()` by another actor keeps a
     truncated console and can false-timeout. Not reachable in the named deployment: one job
     holds the System and ADR-0576 already refuses a foreign out-of-band start.
   - The best-effort domain teardown can itself fail; the domain then waits for the reconciler
     or `systems.teardown`, as an overlay does today (ADR-0435).
   - A retry whose earlier attempt already failed readiness and destroyed the domain starts a
     fresh boot; the handler has already recorded the System `failed`, so that retry exits early.
4. **Covered elsewhere**
   - Authority-lane TCG scaling of its `boot_ready` window and its 15-minute intent deadline —
     follow-up candidate, operator-excluded.
   - `ready` implying cloud-init's final stage — follow-up candidate, operator-excluded.
   - Durable writes across a power-off — ADR-0679 (#2757).
   - Remote-libvirt provision gating — out of scope.

## Validation

- Unit, `tests/providers/local_libvirt/test_provisioning.py`: ready on the first poll; ready
  after pending polls; timeout (`first_boot="timeout"`, domain destroyed and undefined, created
  overlay and baseline reclaimed); crash (`first_boot="not_ready"`, `crash_signature`); exit;
  the TCG multiplier scales the poll count; an already-active domain skips truncate and
  `create()` and still waits; no seam means no probe call; `reprovision` timeout ends
  `PROVISIONING_FAILURE` with the domain gone; a failing domain teardown does not mask the error.
- Unit, `tests/providers/local_libvirt/lifecycle/boot/`: `poll_readiness` returns the first
  answer, the first probe error, and `None` on exhaustion.
- Existing `test_install.py` booter tests stay green unchanged.
- Live, `tests/integration/test_first_boot_host_keys_live.py`: requirement 8 on a KVM host;
  the negative (shrunk `KDIVE_LIBVIRT_BOOT_WINDOW_S`) and retry arms are run by hand and recorded
  in the PR.
