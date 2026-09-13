# Customization-boot terminal manager freeze

## Problem

On the recorded Fedora 44 ppc64le-under-TCG run, systemd reports both `Failed to start up
manager` and `Freezing execution`. The transient domain remains active, so the customization
boot poller treats it as pending until its scaled window expires and reports `boot_timeout`.
The guest repair is outside KDIVE's scope, but this terminal console evidence is sufficient to
stop the KDIVE wait and preserve the existing `provisioning_failure` error category.

## Scope

Extend only the customization-console backstop classifier. A console is terminal for this defect
only when it contains both full observed terminal lines: the `[!!!!!!]` manager-start failure and
the timestamped `systemd[1]` freeze line, in either position in the accumulated console. The
success marker remains authoritative. A matched terminal pair follows the existing failed-verdict
path, which emits the bounded console tail, tears down the transient domain, and does not sleep
or consume the remaining poll budget.

The change does not modify Fedora or systemd, guest image content, SELinux policy, domain XML,
the timeout setting, multiplier calculation, error taxonomy, or live-host provisioning.

### Failure model

- Actors and deployments: a local-libvirt worker runs a transient customization domain on KVM or
  TCG; unit tests use injected console and domain seams.
- Invariants and assets: a genuine terminal manager failure must retain its console evidence and
  be `provisioning_failure`; successful marker output must not become a failure.
- Accepted failure classes: unknown or incomplete systemd output remains pending, because only
  the recorded pair establishes a frozen manager; a live reproduction is accepted as unavailable
  without the operator emulated-host fixture.
- Covered elsewhere: Fedora/systemd guest repair is upstream-owned; TCG deadline calibration is
  completed by #2414/#2420.

## Success

For the named pair `Failed to start up manager` plus `Freezing execution`, the classifier returns
`FAILED`, and `run_customization_boot` raises `CategorizedError(PROVISIONING_FAILURE)` before its
next sleep. The existing named cases remain unchanged: `kdive-customize-ok`,
`kdive-customize-failed`, a genuine kernel fault, quiet/pending output, and window exhaustion.

## Validation

- Focused classifier tests prove the pair is required, the pair fails, and the success marker
  wins when present.
- An orchestration test proves the pair yields `PROVISIONING_FAILURE`, retains the console tail,
  and does not invoke the sleep seam.
- `just lint`, `just type`, and the focused customization-boot test module validate source and
  test integration; `just ci` is the pre-push gate.

## Decision record

ADR-0345 already governs marker-authoritative customization-console classification and its
kernel-fault backstop. This change adds one observed terminal signature within that decision and
does not introduce a competing architecture or ownership boundary; no new ADR is warranted.
