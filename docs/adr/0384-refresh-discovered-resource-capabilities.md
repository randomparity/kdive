# ADR 0384 — Refresh existing resource capabilities on discovery registration

- **Status:** Accepted
- **Date:** 2026-07-18
- **Deciders:** kdive maintainers

## Context

Discovery registration is **insert-only-when-absent**. The registrar
(`_discovery_registrar`, `providers/assembly/composition.py`) calls
`ensure_discovered_resource_registered` (`providers/core/resource_registration.py`), which
returns the moment the `(kind, host_uri)` row exists and never re-reads the discovery source.
Both the onboarding path (`admin/projects.py` → `register_discovered_resources`) and the
reconciler/process-start path (`resolver.register_all_discovery`) go through it.

When a new capability **key** is added to discovery, an existing row inserted by an older build
never gains it. Its `capabilities` jsonb stays stale, the capability reader returns absent, and
any admission gate keyed off that capability is silently off on the upgraded host. This
surfaced live in the #1151 fadump proof: a pre-fadump local-libvirt row lacked `pseries_fadump`
and admission denied every fadump provision (`CONFIGURATION_ERROR`/`pseries_fadump_unsupported`)
on a genuinely capable host until the row was recreated by hand through the existing upsert
`register_discovered_resource`. The failure is epic-wide (`guest_arches` #1140, `accel` #1141
share the set-at-creation behavior) and fail-closed (a missing key reads as unsupported → deny).

The insert-only choice was deliberate: the note at the registrar call site records that
`ensure_discovered_resource_registered` calls `discovery.list_resources()` synchronously inside
its async transaction and that a **remote** TLS connect has no pre-connect timeout, so avoiding
the discovery call on the hot (row-exists) path avoided an unbounded remote connect. Any refresh
design must preserve that safety.

## Decision

Refresh the stored `capabilities` in place on the registrar's **row-exists** branch, scoped and
guarded so the insert-only safety properties survive.

1. **Refresh location — the exists branch only.** The row-absent branch is unchanged (discover
   + insert, still fails the insert if discovery fails). The exists branch, which used to
   `return`, now re-reads the discovery record and `UPDATE`s the row. The function is renamed
   `register_or_refresh_discovered_resource` to reflect the new behavior (a mechanical rename of
   two call sites).

2. **Capabilities only.** The refresh writes **only** `capabilities`. It does not touch
   `status`, `pool`, `cost_class`, `cordoned`, `managed_by`, `host_uri`, `id`, or `created_at`.

3. **Full replace of the jsonb from the fresh discovery record.** For a `creates=True`
   (local-libvirt) host, discovery is the sole authoritative writer of `capabilities`; there is
   no production operator capability-edit path to merge-preserve, so replacing the jsonb with
   the current discovery record is correct and also corrects a *changed* key value, not only a
   missing one.

4. **`creates=True` scope preserves the remote-connect safety structurally.** The registrar
   already returns early for `creates=False`. Only local-libvirt is `creates=True`;
   remote-libvirt and fault-inject are `creates=False` bind-only no-ops. So the refresh only
   ever re-reads the **local** discovery source (a local libvirt connection) — it adds **no**
   remote connect on any path. The "remote TLS connect has no pre-connect timeout" concern is
   honored by construction, not by a runtime check.

5. **Best-effort on the exists branch.** If discovery raises or does not return the target
   record, the refresh logs at `WARNING` and leaves the existing `capabilities` untouched. A
   transiently-unreachable local libvirt on a redeploy/restart must not fail onboarding or
   starve the reconciler, and must never make a working row worse — the refresh can only improve
   an existing row or leave it as-is. The absent branch keeps today's fail-on-discovery-failure
   behavior (a never-registered host must discover to insert).

No schema change, no migration, no new tool, no new error category.

## Consequences

- A capability key added to discovery rolls out to existing local-libvirt rows automatically on
  the next onboard or process start. The #1151-class manual row recreation is no longer needed.
- The exists branch now re-reads the local discovery source on every registration pass (onboard
  + each process start), inside the per-resource advisory-locked transaction — the same place
  and lock the absent branch already pays for `list_resources()` on cold start. Bounded local
  connection; no remote connect.
- Latent-status-clobber is avoided: a future health path that sets a resource `DEGRADED`/
  `OFFLINE` will not be reset to `AVAILABLE` by a capability refresh.
- `capabilities` is a full replace from discovery, so it is not a place to stash operator
  overrides for a `creates=True` host — that was already true (discovery owned inserts).

## Considered & rejected

- **Reuse `register_discovered_resource` wholesale.** It also rewrites `status`/`pool`/
  `cost_class` (latent status clobber, §Decision 2) and opens its own transaction + advisory
  lock, which is awkward to call from inside the registrar's existing locked transaction.
  Rejected in favor of a capabilities-only `UPDATE` on the existing connection.
- **A separate operator-invoked refresh tool / CLI command.** More surface for a problem the
  automatic deploy/reconciler paths already cover; the issue asks for automatic rollout.
  Rejected.
- **Extend the refresh to remote-libvirt (`creates=True` for remote).** Would require the
  untimed remote TLS connect the insert-only choice exists to avoid, on every pass. Rejected;
  remote rows are config-overlay-owned (ADR-0112) and refreshed through that path.
- **Additive-only capability merge (add missing keys, never change existing).** Fails to correct
  a *changed* capability value and adds merge complexity for no benefit, since discovery is the
  sole authoritative writer of a local host's capabilities. Rejected in favor of full replace.
- **Raise on an exists-branch discovery failure.** Regresses onboard/restart robustness for a
  working row (a transient libvirt blip would fail a redeploy that previously succeeded).
  Rejected in favor of best-effort.
