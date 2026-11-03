# 0752 — Require System ownership for authority Run routing

## Status

Proposed

## Context

Issue #3138 observes a fixed-worker System sent to a different libvirt owner.
The server route describes provider configuration; it does not establish System
ownership. An incorrectly admitted boot creates a preparing activation and fences
allocation release. ADR-0623 already records authority System ownership, and
ADR-0654 exposes the configured route without resolving worker credentials.

## Decision

For `runs.install` and `runs.boot`, a configured authority route requires the
System's durable authority binding to exist and name that same authority instance.
Perform this read after project authorization and before keyed mutation, and
recheck inside the existing System-locked enqueue path before fresh mutations.
Refuse a missing or different owner with `configuration_error`, describing the
configuration mismatch and directing the operator to restore the System's owner
route or provision a new System through the intended authority. Do not fall back
to ordinary worker routing when an authority route is configured.

No configured authority route keeps the existing ordinary path. Matching ownership
keeps existing activation admission, root provenance, replay and lifecycle rules.
Ownership state remains governed by the existing admission policy. This decision
changes no schema, ownership record, deployment setting, or provider operation.

## Consequences

Invalid calls create no new job, activation, reservation or keyed result. A key
previously used under a now-invalid route does not bypass current ownership checks.
Restoring valid configuration permits existing idempotent replay again.

This prevention does not clear activations already created on the wrong owner.
Release requires an active activation; preparing teardown follows its recorded
authority route. No verified operator cleanup for the cross-owner case is supplied
by this decision. Issue #3138 retains that unresolved recovery criterion; no manual
SQL, forced ownership rewrite or purported native POWER recovery is authorized.

## Considered & rejected

- **Check only root provenance.** verified: provenance describes the root image,
  not the daemon or authority owning the System.
- **Check only inside enqueue.** verified: keyed replay executes before that
  callback and would bypass the new admission condition.
- **Silently select the ordinary worker.** judgment: conceals inconsistent
  operator configuration and changes the requested ownership route.
- **Repair historical ownership automatically.** judgment: safe cleanup needs
  evidence across both owners and their retained journals; admission lacks it.
