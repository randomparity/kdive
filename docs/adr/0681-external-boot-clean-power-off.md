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

Neither request carries the System's accelerator. Each authority call has a 5-minute client
deadline, and a release sets a persisted recovery deadline 5 minutes after the request is
accepted; a recovery that misses it ends `recovery_failed`, whose only exit is teardown
(ADR-0583). None of these budgets is scaled for TCG, and the external-boot readiness window
(`KDIVE_LIBVIRT_BOOT_WINDOW_S`, 900 s by default) is already longer than the deadline, so no
clean-shutdown wait can be shown to fit a TCG recovery. Preparation is different: it runs before
the activation deadline is set, and overrunning its per-call client deadline is a retryable
`INFRASTRUCTURE_FAILURE` that resumes from the pre-stop intent. Recovery also runs after
`BOOT_TIMEOUT`, when the target kernel may be hung: it stays `RUNNING` and ignores the request.
The operator chose the TCG treatment below (option B, 2026-09-25, #2780).

## Decision

1. The ADR-0679 helper moves to `lifecycle/power.py` and takes its bound as an argument.
   `install.py` passes `60 s × tcg_deadline_multiplier(accel)`, so its behaviour is unchanged.
2. The session reads the accelerator from the inactive definition it opens: `<domain type="kvm">`
   is KVM, any other type is not.
3. `stop_and_require_inactive(mode=...)` on an active domain takes one of three modes. `clean`
   calls the helper with 60 s on KVM (the ADR-0679 KVM bound) and 120 s otherwise.
   `clean-on-kvm` calls it with 60 s on KVM and otherwise destroys, logged
   `destroy-unaccelerated`. `destroy` destroys at once, logged `destroy-unready`.
4. Preparation uses `clean`. Recovery uses `clean-on-kvm` in phase `target-defined`, the phase
   recorded once activation defined the target, after readiness when the source was running, and
   `destroy` in an earlier phase, where a running target never proved ready (the `BOOT_TIMEOUT`
   case).
5. `restore_power` loses its `prior` argument and its unused `"inactive"` branch: it starts an
   inactive domain, which is all its one caller asks for.
6. Crash harvest, the customization boot, and System teardown stay hard. Each carries a comment
   that cites ADR-0679 and says why.

## Consequences

- A guest write not yet flushed when preparation starts survives into the recovery archive, and
  on KVM one made before a release survives into the restored source.
- A KVM guest that ignores the request (hung after readiness, no ACPI handler) costs 60 s before
  `destroy()`, plus up to 60 s more when a `shutdown()` call blocks in libvirt's guest-agent path,
  which every external-boot domain has. That is at most 120 s of the 5-minute recovery deadline,
  less whatever queue latency has already passed since the release was accepted. A hung source
  System pays the same cost at preparation, or 120 s plus the same overrun on TCG; a TCG
  preparation that then misses its per-call deadline is retried.
- A TCG recovery stop stays hard and can lose unflushed target writes, as before.
- A preparation abort that runs while the source is still shutting down (the authority restarted
  mid-wait) finds the domain active, starts nothing, and fails its readiness check with
  `provider_conflict`. A crash between the pre-stop intent and `destroy()` reached the same state
  before; the clean wait widens that window to the shutdown bound.
- The operator `power off` in `lifecycle/control.py` (ADR-0028) is still a hard stop.

## Considered & rejected

- **A capped clean stop for TCG recovery too.** verified: the recovery deadline is
  `timedelta(minutes=5)` from acceptance (`mcp/tools/external_boot/recovery_idempotency.py`) while
  the unscaled readiness window defaults to 900 s (`providers/local_libvirt/settings.py`), so no
  cap can be shown to leave room for the TCG boot that follows, and a miss is terminal.
- **Keep TCG preparation hard as well.** judgment: preparation overruns are retryable, so the
  wait costs time, not the activation, and it protects the user's own System.
- **Carry `accel` on the external-boot request or port.** judgment: a schema change for a fact the
  session already holds in the domain XML.
- **Clean stop on every recovery.** judgment: after `BOOT_TIMEOUT` the target never ran user work,
  and a hung kernel would spend the whole bound before the same `destroy()`.
- **Destroy on every recovery.** judgment: a release after the user wrote to the target overlay
  would lose those writes, the #2757 loss class this change removes.
- **Pass the remaining deadline to the provider.** judgment: it needs a request field the remote
  provider does not use, and on KVM the fixed 60 s bound already leaves most of the deadline.
