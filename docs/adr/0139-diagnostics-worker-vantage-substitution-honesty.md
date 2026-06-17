# ADR 0139 — Diagnostics worker-vantage substitution attributes its cause

- **Status:** Accepted
- **Date:** 2026-06-16
- **Deciders:** kdive maintainers

## Context

`ops.diagnostics` (ADR-0091) assembles worker-vantage checks `provider_tls` and
`gdbstub_acl` (ADR-0125) but wires no worker-job dispatch for them in this slice. The default
service factory therefore builds the service with `worker_available=False`
(`src/kdive/diagnostics/service.py`), and those checks are *substituted* — never run — with a
single fixed detail:

> `worker could not pick up the diagnostic job; check /livez and /readyz`

That message has exactly one production caller (`default_service_factory`), and for that
caller the message is **wrong**. The worker is not down — there is simply no diagnostic-job
dispatch wired in this deployment. Provision/build/teardown jobs are demonstrably picked up by
the same running worker. Pointing triage at `/livez`/`/readyz` (the health endpoints, ADR-0090)
reads as a worker outage and misdirects the operator. Found during black-box MCP evaluation
(call #22, D4), issue #484.

The single `WORKER_UNAVAILABLE_DETAIL` string overloads two distinct conditions that the
substitution path cannot today tell apart:

1. **Feature-not-enabled** — worker-vantage diagnostic dispatch is unwired in this deployment
   (the only production path today). A worker may be running and healthy.
2. **Worker-unavailable** — dispatch *is* wired but the worker cannot pick the job up. No
   caller produces this today; it becomes reachable only when the ADR-0125 worker-job
   follow-up lands.

The substantive capability gap (actually probing TLS/ACL from a worker job) is the ADR-0125
follow-up and is **out of scope here**. Flipping `worker_available` to `True` alone is unsafe
(`service.py` load-bearing comment): the checks are constructed with empty fields and
never-called probes precisely because they are substituted, not run.

## Decision

The substitution detail attributes its cause. We introduce a closed
`WorkerVantageSubstitution` reason enum with two members and pair it with the existing
`worker_available` flag so the substituted `error` detail is correct for each cause:

- `FEATURE_NOT_ENABLED` →
  `worker-vantage diagnostic checks (provider_tls, gdbstub_acl) are not enabled in this
  deployment` — the default-factory stance while no worker-job dispatch is wired.
- `WORKER_UNAVAILABLE` → the existing `/livez`/`/readyz` health-endpoint detail — reserved for
  a genuine worker outage once dispatch exists.

`DiagnosticsService.__init__` gains an optional `substitution_reason: WorkerVantageSubstitution`
(defaulting to `WORKER_UNAVAILABLE`, preserving the existing meaning of a bare
`worker_available=False`). The default factory passes `FEATURE_NOT_ENABLED` explicitly. The
substituted result stays a three-state `error` (never a contract `fail`, no `fix` string),
satisfying ADR-0091 §1: the tool that explains breakage must not itself wedge or fabricate a
verdict. The reason is also carried as the result's `failure_category` (`not_implemented` for
`FEATURE_NOT_ENABLED`, `transport_failure` for `WORKER_UNAVAILABLE`) so a programmatic caller
can branch without string-matching the detail. `not_implemented` (mirroring
`ErrorCategory.NOT_IMPLEMENTED`) is chosen over `configuration_error` deliberately: an unwired
feature is not an operator-fixable misconfiguration, and `configuration_error` would point the
operator at config they cannot change — a softer form of the very misdirection this ADR removes.

`worker_available` keeps its safety contract intact: it still governs *whether* a worker-vantage
check runs, and the load-bearing "do not flip alone" invariant is unchanged. The new field only
selects *which honest detail* is emitted when a check is substituted.

## Consequences

- An operator reading a `provider_tls`/`gdbstub_acl` `error` is told the feature is not enabled
  in this deployment, not sent to chase a non-existent worker outage. The acceptance criterion
  (detail correctly attributes the cause) is met for the message-honesty slice.
- The `/livez`/`/readyz` detail is preserved for the genuine-outage path, so the ADR-0125
  worker-job follow-up can pass `WORKER_UNAVAILABLE` once a real worker-down condition can occur.
- `failure_category` on the substituted result lets the MCP verdict and any gate distinguish
  "not enabled here" (`not_implemented`) from "worker down" (`transport_failure`) without
  parsing prose.
- One new public symbol (`WorkerVantageSubstitution`) and one new optional constructor argument;
  no schema, migration, dependency, or entrypoint change.

## Considered & rejected

- **Flip `worker_available=True` and run the checks.** Out of scope and unsafe: the checks have
  no worker-job probe wired (empty fields, `_never_*` probes); running them would raise or
  fabricate. This is the ADR-0125 capability follow-up, not the honesty fix.
- **Just change the one `WORKER_UNAVAILABLE_DETAIL` string to the feature-not-enabled wording.**
  Rejected: it would mislabel a *genuine* worker outage (the future dispatch path) as a
  config/feature gap. The two causes are distinct and must stay distinguishable; collapsing them
  the other way is the same defect inverted.
- **Drop the worker-vantage checks from the report entirely when unwired.** Rejected: their
  presence (as a named `error`) tells the operator the capability is recognized but not enabled,
  which is more honest than silently omitting them, and it keeps the check set stable for the
  follow-up that wires real probes.
- **A free-form per-call reason string instead of an enum.** Rejected: a closed enum keeps the
  substitution causes auditable and maps cleanly onto `failure_category`; prose details are
  derived from the enum, not the source of truth.
