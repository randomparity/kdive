# ADR 0327 — Explicit secret registry ownership

- **Status:** Accepted
- **Date:** 2026-07-11
- **Deciders:** kdive maintainers
- **Supersedes:** [ADR-0027](0027-safety-modules-secret-backend-impl.md) registry singleton shape

## Context

ADR-0027 introduced `SecretRegistry`, `Redactor`, and `FileRefBackend` with a
`PROCESS_SECRET_REGISTRY` singleton. The singleton matched the original PoC, but the service now
assembles one `SecretRegistry` at process bootstrap and passes it through server, worker, provider,
middleware, and job-handler composition.

Keeping the singleton beside the explicit registry leaves two mutable redaction sources. A secret
resolved through one path can be invisible to a redactor built from the other, and tests can pass by
accident through leaked process state.

## Decision

Remove the runtime `PROCESS_SECRET_REGISTRY` singleton and `process_global_redactor` helper. All
runtime redaction and file-ref secret resolution take an explicit `SecretRegistry`:

- `FileRefBackend(root, registry, scope=...)` registers into the caller-owned registry before
  returning a value.
- `secret_backend_from_env(registry=..., scope=...)` requires that same explicit registry.
- `Redactor(registry=...)` and `SecretRedactionFilter(registry)` snapshot or cache only the supplied
  registry.
- `scope=None` remains a process-lifetime scope inside the supplied registry; it no longer implies a
  module-level singleton.

Tests that need redaction state create a local `SecretRegistry` and pass it through the same public
constructors production uses.

## Consequences

- There is one redaction state owner per assembled KDIVE process.
- Secret backend callers cannot silently fall back to a process-global registry.
- Redactor behavior is easier to reason about in tests because state cannot leak across tests unless a
  test explicitly shares a registry.
- CLI or one-shot helpers that lack broader composition must create their own `SecretRegistry` and
  pass it to the backend/redactor they construct.
