# 0868 — Keep authority boundary values with their protocol contracts

## Status

Accepted (2026-11-24)

Builds on [ADR-0584](0584-provider-host-authority-fences-external-boot-mutations.md)
and [ADR-0623](0623-authority-owned-system-provisioning.md).

## Context

At `9ba206df6`, transport imports its authenticated-peer value from the SQL
repository and its System error value from the concrete service. The System
package also eagerly exports the service: a fresh protocol import initializes
the service, journal and repository even without requesting a service API.

## Decision

Move the unchanged peer and System error definitions into their respective
existing protocol modules. Migrate direct consumers; keep authentication and
database policy with the repository. Keep one canonical definition per value.

Retain every existing System package export. Resolve only its service class on
demand with Python's module attribute mechanism; retain static typing and the
existing `__all__`. Do not add an export framework or old-module alias layer.

## Consequences

Protocol-value consumers no longer require these implementation owners.
Explicit service access still initializes the service. Other transport imports,
including the teardown snapshot's database-owned binding record, remain outside
this repair. Wire bytes, error categories, credential checks and fences do not change.

## Considered & rejected

- **Keep values in implementation modules.** verified: the imports at
  `src/kdive/providers/external_boot_authority/transport.py` lines22 and72
  import these values from SQL and service modules. The existing external
  `AuthorityServiceError` already has a protocol owner.
- **Move only the exception.** verified: a fresh import of
  `kdive.providers.system_authority.protocol` at `9ba206df6` leaves the service,
  journal and repository in `sys.modules` because the package eagerly exports it.
  `uv run python -B -c "import sys; import kdive.providers.system_authority.protocol;
  print('kdive.providers.system_authority.service' in sys.modules)"` returns `True`.
- **Remove the service package export.** judgment: one deferred attribute keeps
  the existing API without coupling protocol consumers to service initialization.
- **Move every database record or add a shared type package.** judgment: the
  two existing protocol owners suffice; unrelated record ownership is separate work.
