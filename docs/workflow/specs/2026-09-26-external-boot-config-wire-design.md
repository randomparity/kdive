# Preserve local external-boot configuration refusals (#2832)

## Problem

The two pre-mutation checks added by #2800 reject mismatched System acceleration or
authority-host console window as `CONFIGURATION_ERROR`. The local authority adapter
turns those into `provider_conflict`; the worker treats that category as infrastructure
failure. The host log has the corrective detail, but the worker has no typed signal.

## Scope and contracts

Implement [ADR-0688](../../adr/0688-preserve-local-external-boot-configuration-refusals.md).
The two checks in `LocalExternalBootSessionFactory.open` alone raise a dedicated
`CategorizedError` subtype. The local adapter catches that subtype before its broad
provider catch and emits service category `configuration_error`; its logger retains
the local diagnostic. The authority transport renders `configuration-error` in the
existing closed error envelope. The worker sender accepts exactly that new reason,
maps it to `ErrorCategory.CONFIGURATION_ERROR`, and exposes only fixed category text.
The external-boot runner's existing `_bound_failure` writes `error_category` beside
`failure_context.phase`, without the original diagnostic. Other provider errors
still map to `provider_conflict`/`provider-conflict`.

No request or journal model changes. No raw exception text crosses the wire. No
change to authorization, replay, timing arithmetic, remote provider operations,
system authority, or database schema. Current supported Helm upgrades stop old
workloads before starting target-image workers; the receiver may be installed first
where authority hosts are updated separately, but mixed-version rolling operation
is not a promised compatibility mode.

## Failure model

- Actors and deployments: local-libvirt authority host, worker sender, and operator
  in the documented staged deployment; a caller can trigger an external-boot job.
- Invariants and assets: pre-mutation refusal remains distinct from an unknown
  provider state; closed wire vocabulary and host-detail privacy remain intact.
- Accepted failure classes: an older worker paired with a newer authority host
  outside staged deployment reports invalid-response; that mixed mode is unsupported.
- Covered elsewhere: timing calculations and mismatch checks are #2800; authority
  fencing and journal replay remain under ADR-0584; other providers retain owners.

## Threat model

- Boundaries: widen only the authority-host service-to-worker category vocabulary;
  no request field or caller-controlled free text is added.
- Actors: an authenticated tenant can initiate a job but cannot choose the
  authority response; authority-host diagnostics are trusted only on that host.
- Controls: the adapter recognizes only the dedicated pre-mutation type; transport
  emits a fixed category in the existing bounded envelope; sender allowlists that
  category and returns fixed text. Existing peer authentication and response
  canonicalization remain in force.
- Out of scope: host-log disclosure controls belong to deployment logging policy;
  remote and system-authority transports retain their current categories.

## Success and validation

- Each of the two real session-open checks reaches a worker `configuration_error`
  through the local adapter, authority service category, transport dispatcher,
  and sender in one composed test. It asserts exact wire bytes and absence of
  host text in both response and worker exception; a focused runner test checks
  that durable failure context stores only category and phase.
- An unrelated `CategorizedError(CONFIGURATION_ERROR)` and an unexpected provider
  exception still become `provider_conflict`; tests fail if the adapter broadens
  classification.
- Existing unauthenticated, superseded, journal conflict, and provider conflict
  reasons retain their mapping; focused sender and transport tests protect them.
- Run `just lint`, `just type`, focused pytest, `just test-changed`, and the
  pre-push `just ci` gate when implementation resumes. Live VM proof is reported
  only if the runbook preflight finds its fixtures available.
