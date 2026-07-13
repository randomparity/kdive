# ADR 0339 — Validate profile arch at admission and persist the accelerator on the System

- **Status:** Accepted
- **Date:** 2026-07-13
- **Issue:** #1141
- **Epic:** #1139 (full ppc64le support)
- **Builds on:** ADR-0338 (`guest_arches` discovery), ADR-0025 (System admission),
  `domain/platform/arch_traits.py`

## Context

`ProvisioningProfile.arch` is a bare `NonEmptyStr` and Systems admission never
checks it. A profile requesting an arch the host cannot boot — a `ppc64le`
profile on a host with no `qemu-system-ppc64` — is accepted, debits the
allocation's capacity, mints a System, enqueues a provision job, and fails only
at libvirt define/boot time in the worker. The failure is late (after the
allocation flipped `granted → active`) and its message is a raw libvirt error,
not an actionable admission rejection.

ADR-0338 (issue 1) landed the missing input: local-libvirt discovery now
advertises a `guest_arches` capability key — `{arch: {"accel", "emulator"}}` —
filtered to the kdive-provisionable set, where `accel` is `"kvm"` or `"tcg"`.
The typed reader `ResourceCapabilities.guest_arches()` returns `{}` when the key
is absent or malformed.

Downstream epic work needs the resolved accelerator as a **recorded fact** on the
System, not a value re-derived from live host state on every read: issue 3 renders
`<domain type>` from it, issue 4 scales TCG provision/boot deadlines off it, and
cost accounting and arch-parameterized tests key on it.

## Decision

At admission, validate `profile.arch` against the bound Resource's advertised
`guest_arches` and persist the resolved accelerator on the System row.

**Schema.** Migration `0067` adds a nullable `systems.accel text` column;
`System.accel: str | None = None`. Nullable is load-bearing (see below) — a NULL
means "no host-derived accelerator was recorded," not a fabricated default.

**Resolution helper.** A single `resolve_accel(conn, resource_id, arch)` in the
admission layer both validates and resolves:

1. `resource_id is None` or the Resource row is absent → `None` (no bound host to
   validate against; skip).
2. `guest_arches` is empty → `None` (**fail-open**; the resource advertises no
   guest-arch capability, so behave exactly as today — no arch check, no accel).
3. `guest_arches` is non-empty and `arch ∉ guest_arches` → raise
   `CONFIGURATION_ERROR` naming the supported set (**fail-fast**, the same rule as
   `arch_traits()` — never a silent x86 fallback).
4. otherwise → the advertised `accel` string for `arch`.

**Enforcement point: System mint only.** The helper is called at the System-mint
point of both admission lanes — `_insert_provisioning_system` (`systems.provision`)
and `_insert_defined_system` (`systems.define`) — **before** `_new_system_allowed`
and the `_insert_system_and_activate` insert that flips the allocation
`granted → active`. A rejection therefore writes no System, enqueues no job, and
leaves the allocation `granted` (the all-or-nothing rule), exactly like the
existing pre-insert quota/rootfs rejections. On success the resolved accel is
threaded into `_insert_system_and_activate`, which persists it on the inserted row.

The `provision_defined` lane (`defined → provisioning`) does **not** re-validate
the arch: the System's arch was validated and its accel committed at `define`, and
re-validating there would consume no capacity but could newly reject a System whose
allocation is already `active`, stranding it in `defined` with no recovery action
(the `CONFIGURATION_ERROR` flows through `_failure_from_error`, which sets no
`AdmissionRecovery`). The rare case of a host that loses the arch between `define`
and `provision_defined` is left to the existing worker-failure path: the provision
job fails, the System reaches `failed`, and `_failed_system_retry_failure` already
surfaces actionable `RECYCLE_ALLOCATION` guidance. Earlier rejection is not worth a
new recovery-less stuck state for a mid-lane host change.

**Accel value domain.** The persisted accel is the advertised string from
`guest_arches` as-is (`guest_arches()` guarantees it is a `str`, not that it is in
`{kvm, tcg}`); `systems.get` surfaces it verbatim. Domain enforcement is issue 3's
concern — its accel→libvirt-domain-type mapper must fail closed on an unexpected
value (ADR-0338), and, per the nullability decision below, must also handle a NULL
accel (default or fail closed, never crash).

**Surfacing.** `systems.get` returns `accel` via the shared `system_envelope`
(so `systems.list` carries it too), alongside the `arch` the summary already
surfaces; the `systems.get` and `systems.provision` wrapper docstrings — the
agent-facing contract — document the field and the admission-time arch rejection
in the same PR.

## Consequences

- A mis-arch provision is rejected at admission with an actionable message naming
  the supported arches, before any capacity is debited or job enqueued. Transition
  guards are untouched — this is a pre-insert validation, not a new state edge.
- The accel is a recorded fact downstream consumers (issues 3, 4, cost, tests)
  read from the row instead of re-deriving host state.
- Resources that advertise no `guest_arches` — remote-libvirt, fault-inject, and
  any local host that has not re-run discovery since ADR-0338 — provision exactly
  as today and record `accel = NULL`. Downstream consumers must treat NULL as "not
  host-derived" and fall back to their prior behavior.
- A host whose `guest_arches` changes between `define` and `provision_defined`
  keeps its `define`-time accel and is not re-validated at `provision_defined`; a
  now-unsupported arch fails in the worker and recovers via the existing
  `failed → RECYCLE_ALLOCATION` path. This window is short and re-discovery
  mid-lane is rare.
- Downstream accel consumers (issues 3, 4, cost, tests) must handle a NULL accel
  (resource advertised none) as well as an unexpected non-`{kvm,tcg}` string; this
  ADR records the fact, issue 3's mapper enforces the domain.

## Rejected alternatives

- **Validate/resolve in the worker provision handler.** Rejected: the whole point
  is to reject before admission debits the allocation. Worker-time failure is the
  status quo this ADR removes.
- **Fail-closed when `guest_arches` is empty** (reject every provision). Rejected:
  only local-libvirt discovery populates the key (ADR-0338); remote-libvirt,
  fault-inject, and every local host not yet re-discovered advertise none.
  Fail-closing would regress all of them to a hard reject. Gating enforcement on
  the capability actually being advertised mirrors `disk_ceiling()` returning
  `None` to mean "unbounded."
- **Re-validate arch (and/or re-resolve accel) at `provision_defined`.** Rejected:
  the System's allocation is already `active` by then, so a fresh
  `CONFIGURATION_ERROR` strands it in `defined` with no `AdmissionRecovery`
  next-action — a recovery-less stuck state strictly worse than the status quo it
  would replace. The rare mid-lane host change is handled by the worker-failure →
  `RECYCLE_ALLOCATION` path, which already gives actionable recovery. Re-persisting
  the accel would additionally need a generic single-column repo write the codebase
  does not have (only `update_state`), for a negligible re-discovery window.
- **A non-nullable `accel` with a default** (e.g. `"kvm"`). Rejected: fabricates a
  host fact for resources that never advertised one. NULL honestly encodes
  "unknown / not host-derived."
- **Store the libvirt domain type (`kvm`/`qemu`) instead of the accel name.**
  Rejected: ADR-0338 already chose the accel name as the scheduling fact; the
  domain-type mapping is issue 3's rendering concern.

## Rollout

Additive and backward compatible. The migration adds a nullable column;
pre-existing System rows read back `accel = NULL`. No data backfill — the accel is
resolved on the next provision, and NULL is a valid "not recorded" state for the
lifetime of an already-provisioned System.
