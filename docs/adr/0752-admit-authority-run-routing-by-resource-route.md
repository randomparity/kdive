# 0752 — Admit authority Run routing by the Resource's declared authority

## Status

Accepted (2026-11-03)

## Context

Issue #3138: `runs.install` and `runs.boot` send a Run to the external-boot authority
whenever the server's provider binding carries an authority route
(`server_authority_instance`). That route is one server-wide setting, not a property of
the System. A System the fixed worker provisioned, on a Resource with no
`authority_instance`, is routed to an authority that cannot find its domain. The
activation stays `preparing`, and `allocations.release` is fenced with
`external_boot_restricted`, so the allocation is stranded.

The live-vm fixture lane (`system_mode: "fixture"`, live-testing runbook) is the one
legitimate instance of that shape: its System is provisioned by the fixed worker on a plain
Resource, then relocated onto the authority's private daemon by
`provision-authority-fixture.py`, and the lane drives `runs.install`/`runs.boot` through the
real authority. The server cannot see which daemon holds a domain, and the relocation writes no
ownership row (ADR-0623 rows exist only for authority-provisioned Systems). Resource route
and ownership row alone therefore cannot tell the defect from the fixture lane; an explicit
operator declaration can.

## Decision

Add an optional `relocated_authority_instance` to the `[[local_libvirt]]` inventory entry:
the operator's declaration that Systems on this Resource are moved onto that authority's
daemon after provisioning. It is mutually exclusive with `authority_instance` (a Resource is
either authority-provisioning or relocated-fixture; setting both is an inventory error at
load), and non-blank like it. It changes no provisioning behavior: `systems.provision` still
takes the ordinary path on that Resource.

After `server_authority_instance(binding)` returns authority `A`, `runs.install` and
`runs.boot` admit the Run only when any of these holds, else refuse with
`configuration_error`:

- the Resource's `authority_instance` equals `A`;
- the Resource's `relocated_authority_instance` equals `A`;
- the System has an authority System binding (ADR-0623) whose `authority_instance` equals
  `A`.

A recorded binding naming a different authority refuses with
`reason: authority_system_binding_mismatch` whatever the Resource declares. Any other
failure refuses with `reason: authority_resource_route_mismatch`. No authority route keeps the
ordinary path.

The check runs before keyed replay (a stored key cannot bypass a now-invalid route) and
again under the System lock before any job, run-step deletion, activation or reservation is
written. A refusal names the cause and the fix: the Resource does not route to this authority; declare
`relocated_authority_instance` only if every System on it is relocated to that authority's daemon
(the fixture lane), otherwise end this System and provision a new one on a Resource that routes
to the authority. A System the fixed worker already provisioned is not made safe by editing
the inventory. The check lives in a server-side module outside the worker handlers' import
closure. No schema, migration, ownership record, deployment setting or provider operation
changes; the declaration is inventory configuration only.

## Consequences

The relocation declaration is Resource-wide operator trust: it carries no per-System
evidence, so an operator who declares it on a Resource whose Systems are not relocated
re-creates the defect for those Systems. The decision narrows the defect to that explicit
operator act; it does not prove physical location. The Resource-route arm has the same limit: a System the fixed worker provisioned before the operator added `authority_instance` to its Resource is admitted afterwards and strands exactly as in #3138. Enabling a route on a Resource that holds fixed-worker Systems is an operator transition that needs those Systems ended first; a per-System provisioning record would close it and is outside this change.

Invalid calls create no job, activation, reservation or keyed result. Restoring a valid
configuration restores idempotent replay. Fixture-mode Systems relocated onto a routed
Resource, authority-owned Systems and Systems with no route behave as before.

Prevention does not clear an allocation already stranded this way. The break-glass tools
do not recover it: `ops.force_teardown` takes the same authority-marked teardown route as
`systems.teardown`, which the issue's live evidence shows failing
(`journal-conflict` / `provider-not-configured`) because the authority cannot observe a
fixed-worker domain, and `ops.force_release` uses the guarded release service that
refuses while the activation is open. That recovery is tracked in #3520; no
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
