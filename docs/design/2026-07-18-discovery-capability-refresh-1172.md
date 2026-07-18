# Spec — refresh existing resource capabilities on upgrade (#1172)

- **Status:** Draft (for adversarial review)
- **Issue:** #1172 — "discovery: refresh existing resource capabilities on upgrade
  (capability keys added after row creation)"
- **ADR:** [ADR-0384](../adr/0384-refresh-discovered-resource-capabilities.md)
- **Epic:** #1139

## Problem

Deploy/onboard discovery registers a local-libvirt resource **insert-only-when-absent**.
The registrar (`_discovery_registrar`, `providers/assembly/composition.py`) calls
`ensure_discovered_resource_registered` (`providers/core/resource_registration.py`), which
short-circuits the moment the `(kind, host_uri)` row already exists — it never re-reads the
discovery source and never updates the stored `capabilities` jsonb. The reconciler-startup
`register_all_discovery` (`processes/reconciler.py` → `resolver.register_all_discovery`) and
the onboarding path (`admin/projects.py` → `register_discovered_resources`) both go through
this same insert-only registrar.

So when a new capability **key** is added to discovery, an **existing** resource row (inserted
by an older build) never gains it — it keeps its stale `capabilities`. The capability reader
returns `False`/absent for the missing key, so any admission gate keyed off that capability is
effectively off on the upgraded host until the row is recreated by hand.

### Evidence

Surfaced live during the #1151 fadump proof (epic #1139): the local-libvirt row was inserted
by a pre-fadump build, so it carried `guest_arches` but not `pseries_fadump`. Admission then
denied every fadump provision on a genuinely QEMU-10.2.2-capable host with
`CONFIGURATION_ERROR`/`pseries_fadump_unsupported`, until the row was re-registered through the
existing upsert path `register_discovered_resource` (which refreshes `capabilities` in place,
UUID and FKs preserved). See `docs/design/2026-07-14-ppc64le-fadump-proof-record-1151.md` and
ADR-0349 §5.

This is **epic-wide** — `guest_arches` (#1140) and `accel` (#1141) share the same
set-at-row-creation behavior. It degrades **fail-closed** (a missing key reads as unsupported →
deny, never a false-positive), and a **fresh deploy** inserts the row with the key, so it only
bites in-place upgrades. kdive is pre-first-release, so there are no deployments to upgrade
today; this closes the gap before there are.

## Goal

A capability key added to discovery rolls out to an **existing** resource row automatically on
the next deploy / process start, with no manual row recreation — while preserving the safety
properties the insert-only choice was protecting.

## Non-goals

- **No new tool, CLI command, or opt-in flag.** The refresh rides the paths that already run
  discovery registration (onboard + reconciler/process start). Adding an operator-invoked
  "refresh" surface is rejected in ADR-0384.
- **Not the #1151 change.** #1151 correctly follows the set-at-creation pattern; it is out of
  scope.
- **No schema/migration change.** The refresh is an `UPDATE` over the existing
  `resources.capabilities` column.

## Design

### Where the refresh lands

`ensure_discovered_resource_registered` currently has two branches inside its per-resource
advisory-locked transaction: **row exists → return** and **row absent → discover + insert**.
The existence probe is today a bare `SELECT 1` (`_resource_exists`). The change replaces that
probe with a `SELECT capabilities FROM resources WHERE kind = %s AND host_uri = %s FOR UPDATE`
that both decides the branch and reads the stored `capabilities` under a row lock:

- **absent** (no row) → discover + insert, **byte-for-byte identical** to today (including
  "discovery must succeed or the insert fails").
- **exists** (row locked) → discover the current record, compute the merge (below), and
  change-guarded `UPDATE` the stored `capabilities`.

The `FOR UPDATE` is load-bearing for concurrency, not decoration. The refresh's advisory lock
keys on `(kind, host_uri)`, but the operator tool `ops.set_host_capacity` serializes on a
*different* advisory key — `resource_identity_lock(kind, name)`, and `name` is NULL/`""` for a
discovery-inserted row — so the two writers do **not** mutually exclude at the advisory layer.
They *do* both touch the same physical row: `ops.set_host_capacity` reads it `FOR UPDATE`
(`_lock_host_for_cap`) before merging the cap. Making the refresh's read also `FOR UPDATE`
serializes the two on the Postgres row lock, so a refresh's read-modify-write of `capabilities`
cannot lose a concurrent operator cap change (whichever transaction commits second sees the
other's write). Without it, a refresh carrying a genuine discovery change could overlay a stale
cap read and clobber a just-committed operator cap — the exact fail-open reversion the overlay
exists to prevent.

Because the function no longer only *ensures registered*, it is renamed
`register_or_refresh_discovered_resource` (call sites: `providers/assembly/composition.py` and
the discovery tests). The rename is mechanical.

### What is refreshed: capabilities, preserving operator-owned keys

The refresh updates **only** the `capabilities` jsonb. It does **not** touch `status`, `pool`,
`cost_class`, `cordoned`, `managed_by`, `host_uri`, `id`, or `created_at`.

Within `capabilities`, the refresh is **discovery-authoritative except for operator-owned
keys**. Concretely, the new value is the fresh discovery record's capabilities, with the stored
value of each operator-owned key overlaid back on top:

```
new_capabilities = fresh_discovery_caps | {
    k: stored_caps[k] for k in _OPERATOR_OWNED_CAP_KEYS if k in stored_caps
}
```

`_OPERATOR_OWNED_CAP_KEYS = frozenset({CONCURRENT_ALLOCATION_CAP_KEY})` — a new constant beside
the capability keys. It captures the one capability key a `platform_operator` can write directly
onto a resource row.

Rationale (full detail in ADR-0384):

- **`concurrent_allocation_cap` is operator-owned and must survive the refresh.** Local-libvirt
  discovery emits `concurrent_allocation_cap` itself (from `KDIVE_LIBVIRT_ALLOCATION_CAP`,
  default 1), *and* the `platform_operator` tool `ops.set_host_capacity`
  (`mcp/tools/ops/tuning.py`) writes that same key straight into the discovery row's
  `capabilities` via a targeted `jsonb ||` merge — deliberately writing **no** override-ledger
  entry, on the documented contract that "a discovery row is outside the ledger" and "reconcile
  never overwrites a runtime row's cap." That audited cap sticks today *only* because nothing
  re-writes the discovery row. A naive full replace would revert it to the env default on every
  onboard/process-start — in the **fail-open** direction (a deliberately-lowered cap raised back
  up, re-opening placement the operator blocked). Overlaying the stored value back preserves it.
- **Everything else in `capabilities` is discovery-authoritative.** Replacing (not merely
  additively-filling) lets the refresh correct a *changed* value of a discovery-owned key — e.g.
  a host that gains an arch reports a new `guest_arches` list under the same key — not only a
  net-new key. Additive-only-by-key (the ADR-0384-rejected merge) would miss that.

**Known limitation — `concurrent_allocation_cap` is row-authoritative once inserted.** Because
local-libvirt discovery *always* emits `concurrent_allocation_cap` (so the key is present from
insert onward) and the overlay cannot tell an operator-set value from the insert-time env
default, the stored value always wins for this key. Consequence: changing
`KDIVE_LIBVIRT_ALLOCATION_CAP` and redeploying rolls out new capability *keys* to an existing
row but **not** a new default cap — an operator must use `ops.set_host_capacity` to change a
registered host's cap. This matches today's behavior (insert-only never refreshed anything), so
it is not a regression; it is called out so a deployer bumping the env default does not expect
it to propagate to existing rows. Rolling an env-default change out to existing rows is out of
scope for #1172.
- `status` from a local discovery record is a hardcoded `AVAILABLE`. Nothing in production
  transitions a resource to `DEGRADED`/`OFFLINE` today, but the state machine permits it; a
  refresh that also rewrote `status` would silently reset any future operationally-set status
  back to `AVAILABLE`. Refreshing capabilities only removes that latent clobber for zero
  benefit.
- `pool`/`cost_class` are static registration metadata (identical on every pass), so refreshing
  them is a no-op with no upside.
- `cordoned` is already preserved (neither the insert nor the upsert path writes it).

### Scope of hosts: `creates=True` only (safety preserved structurally)

The registrar already returns early for a `creates=False` registration
(`_discovery_registrar`: `if not registration.creates: return`). Only **local-libvirt** is
`creates=True`; **remote-libvirt** and **fault-inject** are `creates=False` bind-only no-ops.
So the refresh only ever re-reads the **local** discovery source, which is a local libvirt
connection — never the remote one. The "remote TLS connect has no pre-connect timeout" note
that motivated the insert-only choice is honored: the refresh introduces **no** new remote
connect on any path. This is a structural property, not a runtime check.

### Failure posture: best-effort on the exists branch

On the **absent** branch, discovery failure still fails the insert exactly as today (a host
that has never registered must discover to insert; unchanged).

On the **exists** branch, the refresh is **best-effort with a precisely-scoped catch**. The
`try/except` wraps **only the pre-write work** — the `list_resources()` call and the record
selection. If either raises (discovery unreachable, or the target record absent from the
result), the failure is logged at `WARNING` and the branch returns with the existing row's
`capabilities` untouched. The subsequent change-guarded `UPDATE` is **outside** that catch: a
genuine DB error on the write propagates like any other registration DB error rather than being
swallowed (swallowing a failed statement inside the outer advisory-locked transaction would
poison it and fail the commit anyway, so a broad catch around the write would be worse than
useless). The row already exists and the system is functional; a transiently-unreachable local
libvirt on a redeploy/restart must not fail an onboarding or starve the reconciler, and must
never make a working row worse. This is what "preserve that safety" means concretely: the
refresh can only *improve* an existing row or leave it as-is.

The reconciler path (`register_provider_resources`) already wraps `register_all_discovery` in a
timeout + catch-all; the onboarding path (`register_discovered_resources`) does not, which is
why the exists-branch best-effort guard lives at the refresh site rather than relying on an
outer catch.

### Discovery-read boundedness (no new timeout added)

The exists-branch re-read calls the **local** libvirt discovery source, the same source the
absent branch already calls on cold-start insert. There is no separate pre-connect timeout on
the local connect, and this change does **not** add one — doing so would be inconsistent with
the untimed cold-start insert read that already exists and would expand scope. So the hang
exposure of a wedged local `libvirtd` is unchanged in kind from today; the only difference is
that the read now also happens on a *re-registration* pass, not solely on first insert. The
reconciler path retains its existing `PROVIDER_DISCOVERY_TIMEOUT_SECONDS` `wait_for` bound
around the whole `register_all_discovery`; the onboarding path is an interactive operator CLI
action where a wedged local libvirtd is observable to the operator. The best-effort catch
covers a discovery call that *raises*, not one that *hangs* — that is an accepted, pre-existing
residual, not a new one.

## Behavior change summary

| Path | Row absent | Row exists, capabilities already current | Row exists, key missing / discovery-owned value stale |
|------|------------|------------------------------------------|-------------------------------------------------------|
| Today | discover + insert | no-op (short-circuit) | no-op (stale kept) |
| After | discover + insert (unchanged) | discover, compute merge, **skip UPDATE** (no write) | discover + `UPDATE capabilities` (**gains key / corrects value**, operator-owned keys preserved) |

The `UPDATE` is **change-guarded**: the branch computes the merged capabilities and writes only
when they differ from the stored value, so an unchanged discovery source produces no row write
(no WAL, no version bump) — only a discovery read. On the exists branch the refresh re-reads the
local discovery source on every registration pass (onboard + each process start); see
"Discovery-read boundedness" for why that read is no worse than today's cold-start insert.

## Acceptance criteria

1. An existing local-libvirt resource row whose stored `capabilities` lack a key that discovery
   now reports **gains** that key after `register_or_refresh_discovered_resource` runs, with
   the row's `id` and `managed_by` unchanged.
2. A discovery-owned key whose **value** changed (e.g. `guest_arches`) is **updated** to the
   discovery value on refresh.
3. An operator-set `concurrent_allocation_cap` on an existing row **survives** a refresh even
   though the discovery record reports a different (env-default) value — the stored operator
   value wins, and the row still gains any net-new discovery keys in the same pass.
4. The exists-branch capabilities read is `FOR UPDATE`, so a `ops.set_host_capacity` cap change
   committed concurrently with a refresh that carries a net-new discovery key is **not** lost —
   the operator cap is present on the final row regardless of commit order.
5. `status`, `pool`, `cost_class`, and `cordoned` on an existing row are **unchanged** by a
   refresh even when the discovery record would report different values.
6. A `creates=False` registration (remote-libvirt, fault-inject) remains a bind-only no-op:
   `list_resources()` is **not** called and no row is written.
7. On the **absent** path, a discovery failure still raises (insert behavior unchanged).
8. On the **exists** path, a discovery **read** failure (raise, or target record absent from the
   result) leaves the stored `capabilities` **unchanged** and does not propagate — the pass logs
   and continues.
9. Change-guard: a refresh against an unchanged discovery source performs the discovery read but
   issues **no** `UPDATE` (end-state identical, no write).
10. Guardrails green: `just lint`, `just type`, `just test`.

## Test plan

- Unit (`tests/services/test_resource_discovery.py`): rework
  `test_ensure_discovered_resource_registered_does_not_overwrite_existing_row` into a
  **refresh** assertion — an existing row's capabilities are updated from a changed discovery
  record — and add: (a) a missing-key-gained case, (b) a discovery-owned changed value updated,
  (c) an operator-set `concurrent_allocation_cap` preserved while a net-new key is still gained,
  (d) `status`/`pool`/`cost_class`/`cordoned` preserved on refresh, (e) `id`/`managed_by`
  preserved, (f) exists-branch discovery-**read** failure is swallowed and leaves capabilities
  intact, (g) change-guard: an unchanged discovery source issues no `UPDATE` (assert via a write
  probe such as `xmin` unchanged or an unchanged `updated_at`/row identity), (h) `creates=False`
  still never calls `list_resources`. Update
  `test_ensure_discovered_resource_registered_bootstraps_one_row`'s `discovery.calls` expectation
  to reflect the refresh re-read.
- Concurrency (`tests/services/test_resource_discovery.py`, disposable-Postgres tier): hold a
  `SELECT … FOR UPDATE` on the resource row in one transaction (simulating an in-flight
  `ops.set_host_capacity`) and assert a concurrent refresh **blocks** on the row lock until that
  transaction commits its cap change, then observe the operator cap on the final row. If a true
  two-transaction interleave is impractical in the harness, fall back to asserting the narrower
  invariant that the exists-branch read statement is `FOR UPDATE` (so it participates in the row
  lock) together with the sequential AC3 preservation — and state that coverage boundary
  explicitly rather than implying the full race is exercised.
- Rename ripples: `test_register_discovered_resource_is_idempotent` (the standalone upsert
  helper) is unaffected.
