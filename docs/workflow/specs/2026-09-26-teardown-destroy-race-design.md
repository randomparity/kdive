# Local external-boot teardown destroy race

## Problem

Issue #2829: an owned domain can shut down after teardown observes it active but before
`destroy()`. Libvirt then rejects destroy although teardown's desired inactive state has been
reached. The rejection stops undefine and owned-storage cleanup.

## Scope and authority

The issue and campaign dispatch authorize the local external-boot teardown destroy call and its
direct tests. Other lifecycle destroy sites belong to separate work; clean power-off is owned by
#2802. Ownership validation, hard teardown, and cleanup order remain intact. No public API,
persisted state, dependency, or toolchain change is needed. The implementation targets x86_64 and
ppc64le, as declared by the project.

## Design

The teardown session remains the owner of this call. After its exact ownership lookup, it checks
whether the domain is active. If active, it calls `destroy()`. An operation-invalid libvirt error
is accepted only when a fresh `isActive()` read proves inactivity. Other libvirt errors propagate.
The existing final active check still rejects a domain that remains active after a successful or
accepted destroy. Teardown's caller can then undefine the owned domain and remove its overlay and
baseline in the existing order.

The clean power-off helper introduced by #2802 checks for the exact `SHUTOFF` state and governs a
different path; changing it would expand this issue's surface. The local teardown check already
uses `isActive()`, which expresses its existing postcondition. No new helper or caller migration is
needed.

## Failure model

- Actors and deployments: local-libvirt worker teardown on supported x86_64 and ppc64le hosts.
- Invariants and assets: destroy only an exactly owned domain; do not remove storage while it is
  active; continue cleanup when the domain is already inactive.
- Accepted failure classes: libvirt failures other than operation-invalid remain errors; an
  unreadable post-state remains an error; these preserve the existing fail-closed cleanup path.
- Covered elsewhere: clean power-off race is covered by #2802; other lifecycle destroy sites
  belong to separate follow-up work.

## Success

A deterministic direct test makes `destroy()` raise operation-invalid after the active check and
sets the owned domain inactive; teardown then completes undefine and owned-storage cleanup.
Direct tests show the same exception with an active post-state, and an unrelated exception with
an inactive post-state, still fail without resource removal.

## Validation

- focused-test: direct teardown tests in `test_session.py` for the race and both error branches.
- focused-test: existing teardown tests in that file preserve ownership and cleanup behavior.
- task-test-not-applicable: no transport or live VM contract changes; live VM tests require an
  operator-provided image and are outside this one-call-site proof.
- guardrails: `just lint`, `just type`, and the mandatory pre-push `just ci`.
