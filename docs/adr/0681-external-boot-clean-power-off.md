# 0681 — Local-libvirt external-boot stops use the clean power-off

## Status

Accepted (2026-09-25)

Amends [ADR-0679](0679-local-libvirt-clean-power-off.md) Consequences (the external-boot sessions
are no longer unchanged) and [ADR-0583](0583-external-run-boot-uses-prepared-recovery-points.md)
Decision (how recovery stops the domain).

## Context

ADR-0679 made `runs.boot` and the module-injecting install ask a running guest to shut down before
falling back to `destroy()`, because a hard kill loses writes still in the page cache (#2757). The
local-libvirt external-boot session still calls `destroy()` in `stop_and_require_inactive()`. Two
steps use it, and both keep the overlay the guest was writing:

- preparation stops the user's running source System, then reads `/lib/modules/<release>` from
  its overlay into the recovery archive;
- recovery stops the running target, then hands the same overlay back to the source definition.

Neither request carries the System's accelerator, and both run under the external-boot authority
client deadline, which defaults to 5 minutes (`recovery_readiness_timeout`,
`activation_readiness_timeout`). The ADR-0679 bound under TCG is 600 s, so it cannot be reused
unchanged. Recovery also runs after `BOOT_TIMEOUT`, when the target kernel may be hung: it stays
`RUNNING` and ignores the request.

## Decision

1. The ADR-0679 helper moves to `lifecycle/power.py` and takes its bound as an argument.
   `install.py` passes `60 s × tcg_deadline_multiplier(accel)`, so its behaviour is unchanged.
2. `stop_and_require_inactive(clean=...)` on the external-boot session either calls the helper
   (`clean=True`) or destroys at once (`clean=False`, logged `destroy-unready`).
3. The session reads the accelerator from the domain definition it already holds
   (`<domain type="kvm">` is KVM; any other type takes the TCG multiplier). Its bound is
   `min(60 s × tcg_deadline_multiplier(accel), 120 s)`. The 120 s cap leaves at least 180 s of
   the default 5-minute deadline for the capture, restore and boot that follow.
4. Preparation always stops cleanly. Recovery stops cleanly only when the recovery metadata is
   in phase `target-defined`, the phase recorded after the target passed readiness. In any
   other phase a running target never proved ready (the `BOOT_TIMEOUT` case), so it is destroyed
   at once.
5. `restore_power` loses its `prior` argument and its unused `"inactive"` branch: it starts an
   inactive domain, which is the only thing its one caller asks for.
6. Crash harvest, the customization boot, and System teardown stay hard. Each carries a comment
   that cites ADR-0679 and says why.

## Consequences

- A guest write not yet flushed when external-boot preparation or a release starts survives into
  the recovery archive and the restored source.
- A ready target that ignores the request (a kernel that hung after readiness, or one without an
  ACPI button or EPOW handler) costs up to 60 s on KVM and 120 s otherwise before `destroy()`.
- A TCG guest that needs more than 120 s to shut down is destroyed at the cap and can lose writes,
  as every external-boot stop did before this change.
- The operator `power off` in `lifecycle/control.py` (ADR-0028) is still a hard stop.

## Considered & rejected

- **Reuse the ADR-0679 bound unchanged.** verified: `recovery_readiness_timeout` defaults to
  `timedelta(minutes=5)` (`src/kdive/jobs/handlers/external_boot/ports.py`), below the 600 s
  TCG bound; a recovery that outlives its deadline ends `recovery_failed` (ADR-0583).
- **Carry `accel` on the external-boot request or port.** judgment: a schema change for a fact the
  session already holds in the domain XML, and it changes the lifecycle identity every native
  runner pins.
- **Clean stop on every recovery.** judgment: after `BOOT_TIMEOUT` the target never ran user work,
  and a hung kernel would spend the whole bound before the same `destroy()`.
- **Destroy on every recovery.** judgment: a release after the user wrote to the target overlay
  would lose those writes, which is the #2757 loss class this change removes.
- **Pass the remaining authority deadline to the provider.** judgment: it needs a request field
  the remote provider does not use, for a bound a constant cap already meets.
