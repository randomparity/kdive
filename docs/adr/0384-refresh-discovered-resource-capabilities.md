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

3. **Discovery-authoritative replace, preserving operator-owned keys.** The new `capabilities`
   value is the fresh discovery record with the stored value of each operator-owned key overlaid
   back on top: `fresh | {k: stored[k] for k in _OPERATOR_OWNED_CAP_KEYS if k in stored}`, where
   `_OPERATOR_OWNED_CAP_KEYS = {concurrent_allocation_cap}`. Replacing (not additively-filling)
   the discovery-owned keys corrects a *changed* value, not only a net-new key. The operator
   overlay exists because `ops.set_host_capacity` (`mcp/tools/ops/tuning.py`, `platform_operator`)
   writes `concurrent_allocation_cap` directly onto the discovery row's `capabilities` and
   deliberately records **no** override-ledger entry — its contract is "a discovery row is
   outside the ledger; reconcile never overwrites a runtime row's cap." That audited value sticks
   today only because nothing re-writes the discovery row; a naive full replace would revert it to
   the `KDIVE_LIBVIRT_ALLOCATION_CAP` default (fail-open: re-opening placement an operator
   blocked). The overlay keeps that key stable while still rolling out every other discovery
   change. The `UPDATE` is change-guarded — it writes only when the merged value differs from the
   stored one, so an unchanged discovery source is a pure read. The exists-branch existence probe
   becomes a `SELECT capabilities … FOR UPDATE` (replacing the bare `SELECT 1`): the refresh and
   `ops.set_host_capacity` derive their *advisory* keys from different columns (`host_uri` vs the
   NULL `name`) so they do not serialize there, but both touch the same physical row and
   `set_host_capacity` already reads it `FOR UPDATE`; making the refresh read `FOR UPDATE` too
   serializes them on the row lock, so a concurrent operator cap change cannot be lost to a stale
   overlay read.

4. **`creates=True` scope preserves the remote-connect safety structurally.** The registrar
   already returns early for `creates=False`. Only local-libvirt is `creates=True`;
   remote-libvirt and fault-inject are `creates=False` bind-only no-ops. So the refresh only
   ever re-reads the **local** discovery source (a local libvirt connection) — it adds **no**
   remote connect on any path. The "remote TLS connect has no pre-connect timeout" concern is
   honored by construction, not by a runtime check.

5. **Best-effort on the exists branch, scoped to the pre-write read.** The `try/except` wraps
   only `list_resources()` + record selection; on failure it logs at `WARNING` and leaves the
   existing `capabilities` untouched. The change-guarded `UPDATE` runs outside the catch, so a
   genuine DB write error propagates like any other registration DB error (swallowing a failed
   statement inside the outer advisory-locked transaction would poison it and fail the commit
   regardless). A transiently-unreachable local libvirt on a redeploy/restart must not fail
   onboarding or starve the reconciler, and must never make a working row worse — the refresh can
   only improve an existing row or leave it as-is. The absent branch keeps today's
   fail-on-discovery-failure behavior (a never-registered host must discover to insert). No new
   local-connect timeout is added: the exists-branch read is the same untimed local libvirt call
   the cold-start insert already makes, and the reconciler path keeps its existing
   `PROVIDER_DISCOVERY_TIMEOUT_SECONDS` bound around `register_all_discovery`.

No schema change, no migration, no new tool, no new error category.

## Consequences

- A capability key added to discovery rolls out to existing local-libvirt rows automatically on
  the next onboard or process start. The #1151-class manual row recreation is no longer needed.
- The exists branch now re-reads the local discovery source on every registration pass (onboard
  + each process start), inside the per-resource advisory-locked transaction — the same place
  and lock the absent branch already pays for `list_resources()` on cold start. The local
  connect is **untimed**, exactly as the existing cold-start insert read is (this change adds no
  new timeout); the property that holds is **no remote connect**, and the reconciler path keeps
  its `PROVIDER_DISCOVERY_TIMEOUT_SECONDS` bound around `register_all_discovery`. A wedged local
  `libvirtd` hang is a pre-existing residual, unchanged in kind.
- `concurrent_allocation_cap` becomes row-authoritative once a row is inserted: because
  discovery always emits it, the overlay cannot distinguish an operator-set value from the
  insert-time env default, so a later `KDIVE_LIBVIRT_ALLOCATION_CAP` change does not roll out to
  existing rows (an operator uses `ops.set_host_capacity`). This matches today's insert-only
  behavior; rolling an env-default change out to existing rows is out of scope.
- Latent-status-clobber is avoided: a future health path that sets a resource `DEGRADED`/
  `OFFLINE` will not be reset to `AVAILABLE` by a capability refresh.
- An operator's `ops.set_host_capacity` change to a discovery host survives a refresh (the
  operator-owned-key overlay), so an audited cap is not silently reverted on restart.
- `_OPERATOR_OWNED_CAP_KEYS` is the single point that must grow if a future operator tool writes
  another capability key directly onto a discovery row. A new such key not added there would be
  reverted by the refresh — a maintenance obligation, called out so it is not a silent trap.

## Considered & rejected

- **Reuse `register_discovered_resource` wholesale.** It also rewrites `status`/`pool`/
  `cost_class` (latent status clobber, §Decision 2) and opens its own transaction + advisory
  lock, which is awkward to call from inside the registrar's existing locked transaction.
  Rejected in favor of a capabilities-only `UPDATE` on the existing connection.
- **Full replace of the whole `capabilities` jsonb from discovery.** Simpler, but it reverts an
  operator's `ops.set_host_capacity` change to `concurrent_allocation_cap` — which discovery
  re-emits from an env default — on every onboard/process-start, silently and in the fail-open
  direction (§Decision 3). Rejected in favor of the operator-owned-key-preserving overlay.
- **A separate operator-invoked refresh tool / CLI command.** More surface for a problem the
  automatic deploy/reconciler paths already cover; the issue asks for automatic rollout.
  Rejected.
- **Extend the refresh to remote-libvirt (`creates=True` for remote).** Would require the
  untimed remote TLS connect the insert-only choice exists to avoid, on every pass. Rejected;
  remote rows are config-overlay-owned (ADR-0112) and refreshed through that path.
- **Additive-only capability merge (add missing keys, never change any existing value).** Would
  preserve the operator cap, but only as a side effect of never updating *any* existing key — so
  it also fails to roll out a *changed* value of a genuinely discovery-owned key (e.g. an
  expanded `guest_arches`). Rejected in favor of the targeted overlay, which updates
  discovery-owned keys and preserves only the enumerated operator-owned ones.
- **Raise on an exists-branch discovery failure.** Regresses onboard/restart robustness for a
  working row (a transient libvirt blip would fail a redeploy that previously succeeded).
  Rejected in favor of best-effort.
