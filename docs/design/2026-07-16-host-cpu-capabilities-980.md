# Advertise per-host guest CPU model/capabilities at System selection (#980)

- **Status:** Draft
- **Date:** 2026-07-16
- **Issue:** #980 (follow-up to #975 / ADR-0297)
- **ADR:** [ADR-0368](../adr/0368-host-cpu-capabilities-selection.md)

## Problem

ADR-0297 (#975) fixed the EL9/RHEL-family init panic on remote-libvirt by emitting
`<cpu mode='host-model'/>` on the domain. `host-model` was chosen over local-libvirt's
`host-passthrough` because a remote fleet may span **heterogeneous** hosts and needs a
portable, migratable v2+ baseline.

A documented consequence (ADR-0297 "Consequences") is that the **effective guest ISA now
depends on which remote host the domain lands on**. `host-model` reflects each host's real
CPU, so the guest's feature set varies across the fleet. An agent (or operator) selecting a
System today has **no way to see which CPU model/capabilities the guest will get** before
provisioning; the only signal is post-provision inspection of the live domain XML. This
matters when a workload needs specific CPU features (a reproducer that depends on an
instruction-set extension, or a build assuming a baseline).

This is a **visibility/observability** requirement. The provisioning path does not change.

## Goals

1. **At selection time**, advertise each remote host's expected guest CPU baseline on the
   discovery surface (`resources.list` / `resources.describe`), in both raw form (libvirt CPU
   model + vendor) and a normalized `x86-64-vN` level, so an agent can compare hosts before
   provisioning.
2. **Post-provision**, surface the *actual* resolved CPU model of a provisioned System on
   `systems.get`, alongside the existing `accel` field, so an agent can confirm what the guest
   actually received.
3. Change no provisioning behavior; add no new RBAC, error category, or agent-callable tool.

## Non-goals

- Changing the CPU mode (`host-model` stays; ADR-0297 is not reopened).
- Advertising CPU capabilities for **local-libvirt** (single co-located host,
  `host-passthrough`, deterministic — no selection ambiguity) or **fault-inject** (a fake).
  This feature is remote-libvirt-scoped, exactly as ADR-0338 scoped `guest_arches` to local.
- A non-x86 baseline taxonomy. `x86-64-vN` is x86-64 only; other arches carry the raw model
  and arch, with `baseline_level` omitted (see Open questions → resolved).
- Gating admission or placement on CPU capability. This is advisory only; no request is
  rejected for a CPU mismatch (a future issue may add opt-in gating).

## Design overview

Two additive surfaces, both mirroring existing precedents, neither needing a new tool.

### Surface 1 — discovery `host_cpu` capability (selection time)

Add an additive `host_cpu` key to a Resource's `capabilities` jsonb (no migration — jsonb is
schema-less, exactly as ADR-0338 added `guest_arches`). Value shape:

```
capabilities.host_cpu = {
  "model": "Skylake-Client-IBRS",   # libvirt CPU model name (host-model resolves to this)
  "vendor": "Intel",                # host CPU vendor, when libvirt reports it
  "arch": "x86_64",                 # host arch (already advertised separately; echoed for locality)
  "baseline_level": "x86-64-v3",    # normalized level, or omitted for a model with no mapping
}
```

- **Populated** by `RemoteLibvirtDiscovery.list_resources` from the connection's
  domain-capabilities host-model block (the exact model libvirt synthesizes for a
  `host-model` guest on this host). This is the honest predictor of the guest CPU, since the
  renderer emits `host-model`.
- **Parsed** by a new `parse_host_cpu(dom_caps_xml)` in `providers/shared/libvirt_xml.py`,
  defusedxml over the libvirtd trust boundary, returning `None` on any parse fault (never
  crashes discovery — mirrors `parse_capabilities_arch` / `parse_guest_arches`).
- **`baseline_level`** is derived by a shared, curated x86-64 model→level table in
  `domain/platform/` (see Open questions → resolved). A model with no table entry (an unknown
  or non-x86 model) omits `baseline_level` but still carries `model`/`vendor`/`arch`.
- **Typed read** via a defensive `host_cpu()` reader + `HOST_CPU_KEY` + `_KNOWN_KEYS` entry in
  `domain/catalog/resource_capabilities.py`, returning a `HostCpu` TypedDict or `None`
  (mirrors `guest_arches()` — a stale/hand-edited row never crashes a consumer).
- **Agent-facing**: flattened into the envelope `data` by `resource_capability_data`
  (`mcp/tools/_resource_envelopes.py`), so `resources.list` / `resources.describe` show it at
  selection time. This is the one deliberate divergence from `guest_arches`, which is
  admission-only and intentionally *not* surfaced — for #980 agent visibility **is** the point.

### Surface 2 — `systems.get` `resolved_cpu` readout (post-provision)

Persist the actual resolved CPU model on the System at provision time, and surface it from the
row — the exact shape of the `accel` field (ADR-0339, migration 0067).

- **Migration 0070** adds a nullable `resolved_cpu jsonb` column to `systems` (no default; NULL
  means "no resolved CPU recorded" — a pre-migration System, a local/fault System, or a
  provision where the read was unavailable). Mirrors `0067_system_accel.sql`.
- **Populated** by the remote-libvirt provisioner: after the domain reaches readiness (the
  worker already reads the running domain XML), it parses the resolved `<cpu>` (host-model
  expanded to a concrete `<model>`) and persists `resolved_cpu`. Best-effort: on any read/parse
  fault, or an unexpanded `host-model` element, the column stays NULL and **provisioning
  continues unchanged** (observability never fails a provision).
- **Parsed** by a `parse_resolved_cpu(domain_xml)` helper in the remote xml module, reusing the
  same `HostCpu` shape (model/vendor/arch/baseline_level).
- **Surfaced** by `system_envelope` as `data["resolved_cpu"]` (sibling of `accel`), a pure DB
  read — `systems.get` stays a cheap, libvirt-free read with no new failure mode.

## Acceptance criteria

1. `resources.describe` on a remote-libvirt host whose domain-capabilities advertise a
   host-model CPU returns `data.host_cpu` with `model`, `vendor`, `arch`, and (for a mapped
   x86-64 model) `baseline_level`. Verified by a unit test with an injected fake connection.
2. A remote-libvirt host whose domain-capabilities XML is malformed, or omits a host-model CPU,
   discovers successfully with **no** `host_cpu` key (never raises). Verified by a unit test.
3. `parse_host_cpu` returns `None` on malformed/empty XML and a populated `HostCpu` on a real
   host-model capabilities document. Property/edge unit tests cover empty, malformed, missing
   `<model>`, and non-x86 arch (level omitted).
4. The x86-64 level mapper returns `x86-64-v{1..4}` for representative named models and `None`
   for an unknown model. Unit-tested against a table of known models.
5. `resources.describe` on **local-libvirt** and **fault-inject** hosts is unchanged (no
   `host_cpu`) — a regression test asserts the key is absent.
6. Migration 0070 adds a nullable `resolved_cpu` column; the migration test asserts the column
   exists, is nullable, and a System with no resolved CPU reads back `None`.
7. `systems.get` on a System with a persisted `resolved_cpu` returns `data.resolved_cpu`; on a
   System without one, the field is absent/`null`. `systems.get` performs no libvirt call.
8. A `live_vm`-gated test provisions (or inspects an operator-provided) remote EL9 domain and
   asserts the persisted `resolved_cpu.model` is non-empty and its `baseline_level` is ≥
   `x86-64-v2` (the EL9 floor). Skips cleanly without the remote host/image env.
9. `just ci` is green (lint, type, lint-shell, lint-workflows, check-mermaid, test), including
   regenerated generated docs.

## Failure modes & edges

- **Malformed/absent domain-capabilities XML** → `host_cpu` omitted, discovery succeeds.
- **Remote host unreachable at discovery** → existing `TRANSPORT_FAILURE` path is unchanged
  (this feature reads from the same already-open connection; it adds no new connect).
- **Unknown/non-x86 CPU model** → `baseline_level` omitted, raw `model` still advertised.
- **`resolved_cpu` read fault at provision** → column NULL, provisioning unaffected.
- **Stale/hand-edited jsonb row** → defensive typed readers drop malformed values, never crash.
- **Pre-migration Systems** → `resolved_cpu` NULL, field absent on `systems.get`.
- **Domain-capabilities reports `host-model` with no concrete `<model>`** (a host libvirt
  cannot model) → `host_cpu` omitted rather than advertising an empty model.

## Considered & rejected (summarized; full rationale in ADR-0368)

- Live-read domain XML on every `systems.get` (rejected: TLS round-trip + new failure modes on
  a hot, cheap read; persist-at-provision mirrors `accel` and keeps `systems.get` libvirt-free).
- Static host `<cpu>` from `getCapabilities()` as the discovery source (rejected as primary: it
  is the *host* CPU, not the guest-under-host-model CPU; domain-capabilities host-model is the
  exact predictor. `getCapabilities` arch stays the arch source.).
- Feature-set expansion to derive `baseline_level` (rejected: needs a full expanded feature list
  and libvirt-version-specific feature naming; a curated model→level table is simpler, testable
  offline, and honest — unknown models omit the level rather than guess).
- Advertising `host_cpu` for local-libvirt (rejected: single host, `host-passthrough`,
  deterministic — no selection ambiguity to resolve).
- A new dedicated `resources.cpu` / `systems.cpu` tool (rejected: additive fields on existing
  reads match the envelope convention; a new tool is unwarranted surface).

## Open questions (resolved)

- **Raw vs normalized vs both?** → **Both** (operator decision): raw `model`/`vendor` for
  precise identity, `baseline_level` for agent reasoning ("does this meet v2/v3?").
- **Discovery only, or also `systems.get`?** → **Both** (operator decision): discovery for
  selection-time prediction, `systems.get` for post-provision confirmation.
- **How to derive `baseline_level`?** → curated x86-64 model→level table in `domain/platform/`,
  `None` for unmapped models (see ADR-0368).

## Notes

- **Stale premise correction:** the issue and ADR-0297 reference a
  `render_build_domain_xml`/`build_vm.py` second remote renderer. That file/function does not
  exist in the tree; remote-libvirt has exactly one domain renderer (`render_domain_xml`,
  `providers/remote_libvirt/lifecycle/xml.py`). Discovery advertises the host once, so a single
  `host_cpu` per resource covers both System and (nonexistent-separate) build paths.
