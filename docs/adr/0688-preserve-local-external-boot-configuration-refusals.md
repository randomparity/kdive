# 0688 — Preserve local external-boot configuration refusals on the authority wire

## Status

Accepted (2026-09-26)

Amends [ADR-0684](0684-scale-external-boot-deadlines.md) only where it maps the two
pre-mutation local configuration mismatches to `provider_conflict`.

## Context

ADR-0684 requires the authority host to reject an accelerator or console-window
mismatch before mutation. Its adapter currently catches both refusals with other
provider exceptions and sends `provider-conflict`. The worker consequently records
an infrastructure failure for an operator-fixable configuration error. Sending the
host exception text would disclose provider-host details.

## Decision

The two checks in local external-boot session opening raise one dedicated internal
configuration-refusal type. Only that type is mapped by the local adapter to the
authority service's `configuration_error`, then to the closed `configuration-error`
wire category. The worker sender accepts that category and raises a terminal
`CategorizedError` with `ErrorCategory.CONFIGURATION_ERROR` and a fixed, bounded
reason. The worker's existing bound-failure path records category and phase; the
host's diagnostic text remains in the authority-host log.

Other local exceptions, including other `CategorizedError` instances, retain
`provider_conflict`. Authentication, authority fencing, journal replay, and
provider observation semantics do not change. The category carries no host text.
Current deployment guidance stages worker-fence upgrades with old workloads stopped;
this decision does not promise mixed-version rolling transport. Operators using a
separate host rollout can install the worker receiver before the authority sender.

## Consequences

New refusals for those two mismatches reach the job failure record as
`configuration_error`, which is already an allowed committable category. Existing
wire peers retain the same response envelope and all existing category meanings.
An older worker treats the new category as an invalid response if paired with a
newer authority host outside the documented staged upgrade.

## Considered & rejected

- **Map every local `CONFIGURATION_ERROR`.** verified: the local kernel bundle
  and guest writer also raise that category, including work after session open
  (`src/kdive/providers/local_libvirt/lifecycle/boot/` at base
  `77fb30b7f`); the category alone does not prove pre-mutation refusal.
- **Carry the host exception message on the wire.** judgment: an extra
  unbounded host-data surface is unnecessary for an actionable worker category.
- **Keep `provider-conflict`.** verified: the worker sender maps it to
  `INFRASTRUCTURE_FAILURE` (`src/kdive/jobs/authority_sender.py` at base
  `77fb30b7f`), losing the configuration distinction requested in #2832.
