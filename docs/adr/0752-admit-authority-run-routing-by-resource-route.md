# 0752 — Admit authority Run routing only for a Resource that names the authority

## Status

Proposed

## Context

Issue #3138: `runs.install` and `runs.boot` send a Run to the external-boot authority
whenever the server's provider binding carries an authority route
(`server_authority_instance`). That route is one server-wide setting, not a property of
the System. A System the fixed worker provisioned, on a Resource with no
`authority_instance`, is routed to an authority that cannot find its domain. The
activation stays `preparing`, and `allocations.release` is fenced with
`external_boot_restricted`, so the allocation is stranded.

An earlier draft required an authority System binding (ADR-0623 ownership row) for every
authority-routed Run. That also refused the fixture-mode and ADR-0713 reuse Systems, which
the authority legitimately serves from a Resource that routes to it but which were never
provisioned through the authority-owned path. The Resource's own route is the correct
discriminator: the registrar already selects it per Resource
(`authority_instance_for_resource`, ADR-0623).

## Decision

After `server_authority_instance(binding)` returns authority `A`, `runs.install` and
`runs.boot` resolve the System's own Resource route with
`authority_instance_for_resource(kind, resource_name)` and refuse with
`configuration_error` unless it equals `A`
(`reason: authority_resource_route_mismatch`). When the System has an authority System
binding whose `authority_instance` differs from `A`, refuse with
`reason: authority_system_binding_mismatch`. Otherwise admit. No binding row is required.

The check runs before keyed replay (a stored key cannot bypass a now-invalid route) and
again under the System lock before any job, run-step deletion, activation or reservation
is written. A refusal names the cause and the fix: align the Resource's
`authority_instance` with the server route, or provision a new System on the intended
Resource. No authority route keeps the ordinary path unchanged. This decision changes no
schema, ownership record, deployment setting or provider operation.

## Consequences

Invalid calls create no job, activation, reservation or keyed result. Restoring a valid
configuration restores idempotent replay. Fixture-mode Systems relocated onto a routed
Resource, authority-owned Systems and Systems with no route behave as before.

Prevention does not clear an allocation already stranded this way. The break-glass tools
do not recover it: `ops.force_teardown` takes the same authority-marked teardown route as
`systems.teardown`, which the issue's live evidence shows failing
(`journal-conflict` / `provider-not-configured`) because the authority cannot observe a
fixed-worker domain, and `ops.force_release` uses the guarded release service that
refuses while the activation is open. That recovery is tracked in its own issue; no
manual SQL or ownership rewrite is authorized.

## Considered & rejected

- **Require an authority System binding (earlier draft).** verified: refuses fixture-mode
  and reuse Systems the authority serves correctly.
- **Check only root provenance.** verified: provenance describes the root image, not the
  daemon owning the System.
- **Check only inside the enqueue.** verified: keyed replay runs before that callback and
  would bypass the condition.
- **Silently select the ordinary worker.** judgment: hides inconsistent operator
  configuration and changes the requested route.
- **Repair historical routing automatically.** judgment: safe cleanup needs evidence from
  both owners that admission lacks.
