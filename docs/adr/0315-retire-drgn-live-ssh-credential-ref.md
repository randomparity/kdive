# ADR 0315 — Retire the vestigial drgn-live `ssh_credential_ref`; gate on the per-System bootstrap key

- **Status:** Accepted
- **Date:** 2026-07-08
- **Deciders:** kdive maintainers
- **Issue:** [#1037](https://github.com/randomparity/kdive/issues/1037) (BLACK_BOX_REVIEW.md P6; epic #998)
- **Amends:** [ADR-0039](0039-ssh-transport-live-introspection.md) §2 — the drgn-live SSH credential
  is no longer resolved from a profile `ssh_credential_ref` through a `FileRefBackend`; the redaction
  seed comes from the per-System bootstrap key.
- **Builds on:** [ADR-0289](0289-per-system-ssh-bootstrap-key.md) (the per-System bootstrap key that
  already authenticates `introspect.*`), [ADR-0281](0281-always-render-ssh-forward.md) (the SSH
  forward renders on every local domain), [ADR-0085](0085-drgn-live-transport-generalization.md) (the
  agent-facing `drgn-live` transport kind).
- **Spec:** [`docs/superpowers/specs/2026-07-08-retire-ssh-credential-ref-1037-design.md`](../superpowers/specs/2026-07-08-retire-ssh-credential-ref-1037-design.md).

## Context

drgn-live introspection — a headline capability — is unreachable in the `local-dev` deployment
(BLACK_BOX_REVIEW.md P6). `debug.start_session(transport="drgn-live")` fails closed unless the
provisioning profile carries `ssh_credential_ref`, an opaque reference into the file-ref secret
backend pointing at an operator-placed key file under `KDIVE_SECRETS_ROOT`. **No MCP tool creates
that file**, so an agent following the "provision for live introspection" guidance cannot reach the
capability.

The gate is vestigial. `introspect.run` / `introspect.script` do **not** use `ssh_credential_ref`:
they authenticate with the per-System **bootstrap key** (ADR-0289) —
`load_system_bootstrap_private_key` → `materialized_private_key` → `ssh -i key_path`. And the
transport open at `start_session` (`LocalLibvirtConnect._open_ssh`) is a read-only SSH **banner
probe** that never authenticates. So the resolved credential value is never used to connect to
anything. The `ssh_credential_ref` gate's only live effects are (a) blocking `start_session` unless
a ref is set, and (b) seeding the redaction registry with the file's value — a secret that never
authenticates. Meanwhile the SSH forward renders on every local domain (ADR-0281) and every ready
System has a bootstrap key (ADR-0289), so drgn-live has no residual provisioning prerequisite.

The result: a required, agent-uncreatable knob gates a read-only capability while providing zero
authentication value and redacting the wrong secret.

## Decision

Retire the `ssh_credential_ref` credential path for drgn-live and gate a drgn-live `start_session`
on the per-System bootstrap key instead.

1. **Remove** `LibvirtProfile.ssh_credential_ref` from the local-libvirt profile section
   (`profiles/provisioning.py`). The models are `extra="forbid"`, so a profile carrying the field is
   rejected at parse — the field is gone, not ignored.
2. **Replace** the two vestigial `ProfilePolicy` methods (`ssh_credential_ref`,
   `drgn_live_requires_credential`) with one boolean seam `drgn_live_seeds_bootstrap_key(profile)` —
   true iff the drgn-live transport-open *at `start_session`* authenticates over the loopback SSH
   forward and so must gate+seed on the per-System bootstrap key: `True` for local-libvirt, `False`
   for remote-libvirt and fault-inject. (Remote-libvirt still has and uses a bootstrap key — the
   shared `introspect.run` path loads it for every provider — but opens its drgn-live transport over
   the guest-agent seam, so it needs no seed *at start_session*; the seam name is scoped to that
   start-time step, not "remote never touches the key".)
3. **At `start_session`** for a drgn-live transport where `drgn_live_seeds_bootstrap_key` is true,
   call `load_system_bootstrap_private_key(conn, system.id, secret_registry=self._secret_registry)`
   before opening the transport. That shared loader already **fails closed** with
   `configuration_error` / `reason="no_bootstrap_key"` when the key row is absent, and **registers**
   the key value into the redaction registry — so the new path is a reuse: the fail-closed gate and
   the ADR-0039 §2 seed-before-output ordering are both preserved, now with the secret that is
   actually used to authenticate. The dead `FileRefBackend` / `secret_backend_factory` wiring on the
   debug-session path is removed.
4. **No opt-in knob.** drgn-live is available on every ready local System. It is read-only,
   contributor-RBAC-gated, and non-destructive; access control is RBAC's job. The real prerequisite
   — drgn installed in-guest — stays enforced downstream (`introspect.run` returns
   `missing_dependency` off a prepared host).

Provider scope: local-libvirt. remote-libvirt and fault-inject drgn-live behavior is unchanged.
The unrelated build-host `ssh_credential_ref` (a `build_hosts` column + build-host SSH transport)
is a different store and is untouched.

## Consequences

- The drgn-live dead-end is gone: an agent can `debug.start_session(transport="drgn-live")` on any
  ready local System with no credential provisioning, then `introspect.run`.
- The redaction registry is seeded with the bootstrap key — the secret genuinely materialized to a
  temp file and used by `introspect.run` — instead of an unused file. Seed lifetime is process-global
  (`scope=None`, retained for the process, matching how `introspect.run` already seeds the same key);
  strictly more conservative than the former session-scoped release, and the key is long-lived and
  reused across sessions.
- A **stored** profile whose local-libvirt section still carries `ssh_credential_ref` now fails to
  parse (`configuration_error`) on any op that reads it. This is an accepted pre-release greenfield
  break: profiles are immutable request inputs (ADR-0024) with no cross-version compatibility
  promise, and repo policy is replace-don't-deprecate with no migration shims. No DB migration
  (profile is jsonb; no column).
- The `ssh_credential_ref_missing` failure reason is retired; the fail-closed reason for a missing
  key is `no_bootstrap_key`.
- Because the bootstrap key seeds redaction process-globally, no secret is registered under the
  per-session scope on the debug path anymore, so the session-scoped seed/release machinery
  (`_secret_scope`, `_release_failed_attach_secret`, the `start_session`/`end_session` `release`
  calls) becomes registrant-less and is removed as dead code rather than left to imply a
  session-scoped lifetime that no longer exists.
- One provider-policy method shape is unchanged (boolean, same per-provider values); only its name
  and meaning change, so the three adapters and their tests update in lockstep.

## Considered and rejected

- **Keep the credential; document the operator step** (place the key file, set `KDIVE_SECRETS_ROOT`)
  — the doc-only fallback in the issue. Leaves the dead-end for agents (they still cannot create the
  file) and keeps a vestigial field that redacts the wrong secret. Rejected.
- **Keep `ssh_credential_ref` as an accepted-but-ignored field** — a deprecation shim; violates
  replace-don't-deprecate and leaves a misleading agent-visible knob. Rejected.
- **Boolean opt-in field** (`drgn_live: bool`) or **infer from the image `drgn` capability** — both
  keep a per-System gate. drgn-live is read-only and RBAC-gated with no provisioning prerequisite, so
  a per-profile gate is a speculative flag with no security value, and a *required* field would
  recreate the P6 discoverability dead-end. The genuine in-guest-drgn prerequisite is already
  enforced at `introspect.run` (`missing_dependency`). Rejected (operator-confirmed).
- **Add a `secrets.create` MCP tool** so an agent can place the key file — expands the agent's
  secret-writing attack surface to supply a credential that is never used to authenticate. Rejected.
- **Seed redaction from the `ssh_credential_ref` file** (status quo) — masks a secret that never
  reaches any output path while leaving the actually-used bootstrap key unseeded on this path.
  Rejected.
