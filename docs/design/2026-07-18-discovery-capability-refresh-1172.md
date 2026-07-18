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
The change adds a refresh to the *exists* branch: discover the current record and `UPDATE` the
stored `capabilities` in place. The absent branch is left **byte-for-byte identical** to today,
so cold-start insert behavior — including its "discovery must succeed or the insert fails" —
does not change.

Because the function no longer only *ensures registered*, it is renamed
`register_or_refresh_discovered_resource` (call sites: `providers/assembly/composition.py` and
the discovery tests). The rename is mechanical.

### What is refreshed: capabilities only

The refresh updates **only** the `capabilities` jsonb. It does **not** touch `status`, `pool`,
`cost_class`, `cordoned`, `managed_by`, `host_uri`, `id`, or `created_at`.

Rationale (full detail in ADR-0384):

- `capabilities` is the field the bug is about, and for a `creates=True` (local-libvirt) host
  discovery is the **sole** authoritative writer of it — there is no operator capability-edit
  path to preserve, so a full replace of the jsonb from the fresh discovery record is correct.
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

On the **exists** branch, the refresh is **best-effort**: if `list_resources()` raises or the
target record is not returned, the failure is logged at `WARNING` and the existing row's
`capabilities` are left untouched. The row already exists and the system is functional; a
transiently-unreachable local libvirt on a redeploy/restart must not fail an onboarding or
starve the reconciler, and must never make a working row worse. This is what "preserve that
safety" means concretely: the refresh can only *improve* an existing row or leave it as-is.

The reconciler path (`register_provider_resources`) already wraps `register_all_discovery` in a
timeout + catch-all; the onboarding path (`register_discovered_resources`) does not, which is
why the exists-branch best-effort guard lives at the refresh site rather than relying on an
outer catch.

## Behavior change summary

| Path | Row absent | Row exists, key present | Row exists, key missing/stale |
|------|------------|-------------------------|-------------------------------|
| Today | discover + insert | no-op (short-circuit) | no-op (stale kept) |
| After | discover + insert (unchanged) | discover + `UPDATE capabilities` (idempotent) | discover + `UPDATE capabilities` (**gains key**) |

On the exists branch the refresh now re-reads the local discovery source on every registration
pass (onboard + each process start). For local libvirt this is a bounded local connection; it
was already paid once on cold-start insert.

## Acceptance criteria

1. An existing local-libvirt resource row whose stored `capabilities` lack a key that discovery
   now reports **gains** that key after `register_or_refresh_discovered_resource` runs, with
   the row's `id` and `managed_by` unchanged.
2. `status`, `pool`, `cost_class`, and `cordoned` on an existing row are **unchanged** by a
   refresh even when the discovery record would report different values.
3. A `creates=False` registration (remote-libvirt, fault-inject) remains a bind-only no-op:
   `list_resources()` is **not** called and no row is written.
4. On the **absent** path, a discovery failure still raises (insert behavior unchanged).
5. On the **exists** path, a discovery failure (raise, or target record absent from the result)
   leaves the stored `capabilities` **unchanged** and does not propagate — the pass logs and
   continues.
6. The refresh is idempotent: running it twice against an unchanged discovery source leaves the
   row identical.
7. Guardrails green: `just lint`, `just type`, `just test`.

## Test plan

- Unit (`tests/services/test_resource_discovery.py`): rework
  `test_ensure_discovered_resource_registered_does_not_overwrite_existing_row` into a
  **refresh** assertion — an existing row's capabilities are updated from a changed discovery
  record — and add: (a) a missing-key-gained case, (b) `status`/`pool`/`cost_class`/`cordoned`
  preserved on refresh, (c) `id`/`managed_by` preserved, (d) exists-branch discovery failure is
  swallowed and leaves capabilities intact, (e) `creates=False` still never calls
  `list_resources`. Update `test_ensure_discovered_resource_registered_bootstraps_one_row`'s
  `discovery.calls` expectation to reflect the refresh re-read.
- Rename ripples: `test_register_discovered_resource_is_idempotent` (the standalone upsert
  helper) is unaffected.
