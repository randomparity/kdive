# 0681 — Local-libvirt external-boot stops use the clean power-off on KVM

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
clean-shutdown wait can be shown to fit a TCG recovery. Recovery also runs after `BOOT_TIMEOUT`,
when the target kernel may be hung: it stays `RUNNING` and ignores the request.

## Decision

1. The ADR-0679 helper moves to `lifecycle/power.py` and takes its bound as an argument.
   `install.py` passes `60 s × tcg_deadline_multiplier(accel)`, so its behaviour is unchanged.
2. The session reads the accelerator from the inactive definition it opens: `<domain type="kvm">`
   is KVM, any other type is not.
3. `stop_and_require_inactive(clean=...)` on an active domain calls the helper with the ADR-0679
   KVM bound of 60 s when `clean` is true and the domain is KVM. Otherwise it destroys at once and
   logs `destroy-unready` (`clean` false) or `destroy-unaccelerated` (not KVM).
4. Preparation passes `clean=True`. Recovery passes `clean=True` only in phase `target-defined`:
   the phase recorded once activation defined the target, after readiness when the source was
   running. In an earlier phase a running target never proved ready (the `BOOT_TIMEOUT` case).
5. `restore_power` loses its `prior` argument and its unused `"inactive"` branch: it starts an
   inactive domain, which is all its one caller asks for.
6. Crash harvest, the customization boot, and System teardown stay hard. Each carries a comment
   that cites ADR-0679 and says why.

## Consequences

- On KVM, a guest write not yet flushed when preparation or a release starts survives into the
  recovery archive and the restored source.
- A KVM guest that ignores the request (hung after readiness, no ACPI handler) costs 60 s before
  `destroy()`, plus up to 60 s more when a `shutdown()` call blocks in libvirt's guest-agent path,
  which every external-boot domain has. That is at most 120 s of the 5-minute recovery deadline,
  less whatever queue latency has already passed since the release was accepted. A hung source
  System pays the same cost at preparation.
- External-boot stops of a TCG domain stay hard and can lose unflushed writes, as before.
- The operator `power off` in `lifecycle/control.py` (ADR-0028) is still a hard stop.

## Considered & rejected

- **Scale the bound for TCG, capped (for example 120 s).** verified: the recovery deadline is
  `timedelta(minutes=5)` from acceptance (`mcp/tools/external_boot/recovery_idempotency.py`) while
  the unscaled readiness window defaults to 900 s (`providers/local_libvirt/settings.py`), so no
  cap can be shown to leave room for the TCG boot that follows, and a miss is terminal.
- **Carry `accel` on the external-boot request or port.** judgment: a schema change for a fact the
  session already holds in the domain XML.
- **Clean stop on every recovery.** judgment: after `BOOT_TIMEOUT` the target never ran user work,
  and a hung kernel would spend the whole bound before the same `destroy()`.
- **Destroy on every recovery.** judgment: a release after the user wrote to the target overlay
  would lose those writes, the #2757 loss class this change removes.
- **Pass the remaining deadline to the provider.** judgment: it needs a request field the remote
  provider does not use, and on KVM the fixed 60 s bound already leaves most of the deadline.
