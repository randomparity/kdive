# AppArmor session confinement decision (#3067)

## Problem and scope

The documented Ubuntu session deployment runs guests without per-domain AppArmor
confinement. The owning installer deliberately runs libvirtd as the operator;
Ansible persists that unprivileged daemon identity independently of whether its
manager uses a user or system unit. The installed authority endpoint
also uses an unprivileged session daemon. These are protected security boundaries,
not incidental startup details. Source base: `33ddea4559c6498895458b6ecb2ebb0d6d508c9f`.

[ADR-0741](../../adr/0741-apparmor-session-confinement-posture.md) records the
operator's explicit choice: “Accept documented unconfined posture”. This authorizes
resolution through accurate documentation while retaining the failed release
confinement obligation. It does not change the required release-cell assertion.

## Ownership and behavior

Keep the existing lifecycle installer, Ansible role and provider-authority owners.
No callers migrate, no runtime compatibility branch is added, and no daemon is
replaced. Correct the confinement paragraph in the live-testing runbook and the
AppArmor claims in the live host role's main and verification comments. Add an
operator-facing limitation in installation guidance, linking the ADR. Preserve
accepted ADR bodies; no SELinux decision is superseded.

The required host-install cell remains failed when either guest lacks its
per-domain enforcing label. Its `label_confined` and assertion aggregation are
unchanged. Closed #2807 retains carrier evidence only. The operator accepted resolution of
#3067 through the documented-unconfined posture; failed release obligations remain.
No new issue is created.

## Success

1. The accepted deployment posture and its limits are explicitly recorded.
2. Current operator guidance distinguishes unconfined AppArmor session
   guests, enforcing daemon profiles, and the system daemon
   advertising AppArmor capability (not live-tested system guest confinement).
3. Documentation never treats this decision or a closed issue as successful
   confinement evidence; POWER #2818 and the authority-lane exclusions remain.

## Failure model

- Prevent: inaccurate guest-confinement claims and implicit security-policy changes.
- Detect: the existing host-install assertion continues rejecting `unconfined`.
- Accept only with human decision: existing session guests lack AppArmor per-domain
  isolation; this is unsuitable evidence for a confined multi-user deployment.
- Out of scope: privileged daemon migration, new profile-loading authority,
  support removal, native POWER qualification and release qualification itself.

## Validation and checkpoints

The reproduced Ubuntu guest failure and upstream driver path establish the cause.
Run doc and record guards plus staged hooks; run the existing focused host-install
proof unit module to demonstrate the confinement predicate is retained. Following the
operator decision, rerun the complete affected Debian host-install required cell
with the actual deployed candidate and retain its outcome, including the expected
confinement failure. The transient probe is root-cause evidence, not a substitute
for that required rerun. Do not report the cell passed or qualification complete.

If the operator requires positive confinement instead, stop this design cycle and
reassess a scoped authority migration before implementation. A system-daemon
capabilities result alone is not proof that KDIVE can safely share that endpoint.
