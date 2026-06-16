# Spec — Diagnostics worker-vantage substitution honesty (#484)

- **Issue:** [#484](https://github.com/randomparity/kdive/issues/484) (D4, black-box MCP eval)
- **ADR:** [`0139`](../adr/0139-diagnostics-worker-vantage-substitution-honesty.md)
- **Refines:** [`0125`](../adr/0125-diagnostics-host-reachability.md),
  [`0091`](../adr/0091-doctor-diagnostics-model.md)
- **Date:** 2026-06-16

## Problem

`ops.diagnostics` reports the worker-vantage checks `provider_tls` and `gdbstub_acl` as

> `error: "worker could not pick up the diagnostic job; check /livez and /readyz"`

even when provision/build/teardown jobs are demonstrably picked up by a running worker. The
message misdirects triage to the health endpoints (ADR-0090) — it reads as a worker outage when
the real cause is that worker-vantage diagnostic-job dispatch is **not wired in this slice**.

The default factory (`src/kdive/diagnostics/service.py`) builds the service with
`worker_available=False` because no dispatch exists, and the substitution path emits one fixed
`WORKER_UNAVAILABLE_DETAIL` for *every* substituted worker-vantage check. The same string is
the only thing emitted whether the cause is "feature not enabled here" (today's only path) or
"worker genuinely down" (a future path once dispatch lands). The two causes are conflated.

This is the **message-honesty** half of #484. The substantive capability — wiring real
worker-job probes so `provider_tls`/`gdbstub_acl` actually run — is the ADR-0125 worker-job
follow-up and is out of scope (flipping `worker_available=True` alone is unsafe per the
load-bearing comment; the checks have `_never_*` probes and empty fields).

## Approach

Attribute the substitution cause (ADR-0139). Introduce a closed reason enum
`WorkerVantageSubstitution { FEATURE_NOT_ENABLED, WORKER_UNAVAILABLE }` in
`diagnostics/service.py`. `DiagnosticsService.__init__` gains an optional
`substitution_reason` (default `WORKER_UNAVAILABLE`, preserving the existing meaning of a bare
`worker_available=False`). `worker_unavailable_results` becomes reason-driven, emitting:

- `FEATURE_NOT_ENABLED` → `worker-vantage diagnostic checks (provider_tls, gdbstub_acl) are not
  enabled in this deployment`, `failure_category="not_implemented"`.
- `WORKER_UNAVAILABLE` → the existing `/livez`/`/readyz` detail,
  `failure_category="transport_failure"`.

`not_implemented` (mirroring `ErrorCategory.NOT_IMPLEMENTED`) is deliberate over
`configuration_error`: the feature being unwired is **not** an operator-fixable
misconfiguration (there is no config that enables it in this slice), so labeling it
`configuration_error` would re-create a softer form of the misdirection #484 removes. The two
reasons carry **distinct** `failure_category` values precisely so a programmatic caller can
branch on the cause without string-matching the detail.

Each substituted result stays `CheckStatus.ERROR` with `fix=None` (ADR-0091 §1 — never a
contract `fail`, never a fix string). The default factory passes
`substitution_reason=WorkerVantageSubstitution.FEATURE_NOT_ENABLED` alongside the unchanged
`worker_available=False`. The `worker_available` flag keeps its safety contract: it still
governs whether a worker-vantage check runs, and "do not flip alone" stays true.

## Behavior table

| caller | `worker_available` | `substitution_reason` | substituted detail | `failure_category` |
|---|---|---|---|---|
| `default_service_factory` (today) | `False` | `FEATURE_NOT_ENABLED` | "...not enabled in this deployment" | `not_implemented` |
| future genuine worker-down | `False` | `WORKER_UNAVAILABLE` | "...check /livez and /readyz" | `transport_failure` |
| bare `DiagnosticsService(worker_available=False)` (no reason) | `False` | default `WORKER_UNAVAILABLE` | "...check /livez and /readyz" | `transport_failure` |

## Out of scope

- Wiring real worker-job dispatch for `provider_tls`/`gdbstub_acl` (ADR-0125 follow-up).
- Any change to the server-vantage `secret_ref`/`remote_libvirt_reachability` checks, which run
  unaffected by the flag.
- Schema, migration, dependency, or entrypoint changes.

## Acceptance

- A `default_service_factory` run with a remote-libvirt instance configured surfaces
  `provider_tls` and `gdbstub_acl` as `error` whose detail names "not enabled in this
  deployment" (not `/livez`/`/readyz`) and whose `failure_category` is `not_implemented`.
- A `DiagnosticsService` built with `substitution_reason=WORKER_UNAVAILABLE` (or none) still
  emits the `/livez`/`/readyz` detail with `failure_category=transport_failure`.
- The two substitution reasons carry **distinct** `failure_category` values (`not_implemented`
  vs `transport_failure`), so the cause is machine-distinguishable without parsing the detail.
- The substituted result is `error` with `fix=None` in both cases; `has_failure` stays `False`.
- Server-vantage `secret_ref`/`remote_libvirt_reachability` checks still run.
