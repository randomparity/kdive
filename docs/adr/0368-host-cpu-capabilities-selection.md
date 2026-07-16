# ADR 0368 — Advertise per-host guest CPU model/capabilities at System selection

- **Status:** Accepted
- **Date:** 2026-07-16
- **Issue:** #980 (follow-up to #975)
- **Builds on:** ADR-0297 (remote-libvirt `host-model`), ADR-0338 (`guest_arches` discovery
  capability), ADR-0339 (persisted `accel` on the System)

## Context

ADR-0297 emitted `<cpu mode='host-model'/>` on remote-libvirt domains so EL9/RHEL-family guests
clear the glibc x86-64-v2 barrier. `host-model` was chosen over `host-passthrough` because a
remote fleet may span heterogeneous hosts and needs a portable, migratable baseline. Its
documented consequence (ADR-0297 "Consequences"): the **effective guest ISA now depends on the
landing host**, and an agent selecting a System cannot see which CPU it will get. ADR-0297
split that selection-surface gap to this follow-up.

kdive already has the two patterns this needs:

- **ADR-0338** added a `guest_arches` key to a Resource's `capabilities` jsonb, populated at
  discovery from the libvirt capabilities document via a defusedxml parser that returns empty on
  fault, read back through a defensive typed reader. No migration (jsonb is schema-less). But
  `guest_arches` is **admission-only** — deliberately not surfaced to the agent.
- **ADR-0339** resolved a host-derived value (`accel`) during the System lifecycle and persisted
  it on a nullable column (migration `0067_system_accel.sql`), surfaced cheaply by `systems.get`
  from the row.

This ADR composes both: a discovery capability for the *predicted* selection-time CPU (like
`guest_arches`, but agent-facing), and a persisted System column for the *actual* post-provision
CPU (like `accel`).

## Decision

Two additive, remote-libvirt-scoped surfaces. No new tool, RBAC, or error category; no change to
the provisioning path's behavior.

### 1. Discovery `host_cpu` capability (selection time)

Add a `host_cpu` key to `capabilities` (no migration), populated by
`RemoteLibvirtDiscovery.list_resources`:

```
host_cpu = {"model": str, "vendor": str?, "arch": str, "baseline_level": "x86-64-v{1..4}"?}
```

- **Source = domain-capabilities host-model.** Read the connection's
  `getDomainCapabilities()` `<cpu><mode name='host-model'>` block — the exact model libvirt
  synthesizes for a `host-model` guest on this host, which is what the renderer emits. This
  widens the duck-typed `_LibvirtConn` protocol (`connection/transport.py`) with
  `getDomainCapabilities`, satisfied by the real binding and the test fake. Discovery is a cold
  path, so the extra libvirt call is immaterial.
- **Parser** `parse_host_cpu(dom_caps_xml)` in `providers/shared/libvirt_xml.py`, defusedxml
  (the XML crosses the libvirtd trust boundary), returning `None` on any parse fault or a
  host-model block with no concrete `<model>` — discovery never crashes and never advertises an
  empty model (mirrors `parse_capabilities_arch`/`parse_guest_arches`). `supported` arch set is
  **not** needed here (this is a single host CPU, not a guest-arch enumeration).
- **`baseline_level`** from a curated x86-64 model→level table in `domain/platform/` (a small
  module-level mapping keyed on the libvirt/QEMU model name). An unmapped or non-x86 model omits
  `baseline_level` but keeps `model`/`vendor`/`arch`.
- **Typed reader** `host_cpu()` + `HOST_CPU_KEY` + `_KNOWN_KEYS` in
  `domain/catalog/resource_capabilities.py`, returning a `HostCpu` TypedDict or `None`,
  dropping malformed values (mirrors `guest_arches()`).
- **Agent-facing** via `resource_capability_data` (`mcp/tools/_resource_envelopes.py`) →
  `resources.list`/`resources.describe`. This is the one deliberate divergence from
  `guest_arches`: #980 exists precisely to make this visible at selection time.

### 2. Persisted `resolved_cpu` on the System (post-provision)

Mirror `accel`: **migration 0070** adds a nullable `resolved_cpu jsonb` column to `systems`
(no default; NULL = not recorded). The remote-libvirt provisioner, after the domain reaches
readiness (it already reads the running domain XML), parses the resolved `<cpu>` (host-model
expanded to a concrete `<model>`) via a `parse_resolved_cpu` helper and persists it —
**best-effort**: any read/parse fault, or an unexpanded `host-model` element, leaves the column
NULL and provisioning continues unchanged. `system_envelope` surfaces `data["resolved_cpu"]`
(sibling of `accel`) as a pure DB read, so `systems.get` stays libvirt-free with no new failure
mode.

## Consequences

- An agent can compare candidate remote hosts' CPU baselines (raw model + `x86-64-vN`) at
  `resources.describe` time, and confirm the actual model a provisioned System received at
  `systems.get` — closing the ADR-0297 selection-surface gap.
- Both surfaces are additive JSON fields on existing reads; existing consumers are unaffected,
  and absence is the graceful default everywhere (unmapped model, malformed row, pre-migration
  System, local/fault host).
- `resources.describe` gains one libvirt call at discovery (cold path). `systems.get` gains no
  libvirt call (the resolved CPU is persisted, read from the row).
- The advertised `host_cpu` is a *prediction* (what host-model would synthesize); the persisted
  `resolved_cpu` is the *confirmed* value. On the bound host these agree; the two-surface split
  lets an agent both plan and verify.
- The curated model→level table needs maintenance as new CPU models appear; an unmapped model
  degrades to "raw model, no level" rather than a wrong level. Documented, not silent.
- Residual risk: the `live_vm` proof is operator-run (CI cannot boot a guest); the persisted
  `resolved_cpu` populates only on real provisions against a host whose libvirt expands
  host-model in the running-domain XML.

## Considered & rejected

- **Live-read domain XML on every `systems.get`.** Rejected: bolts a TLS round-trip and new
  failure modes onto kdive's most-polled read; the resolved CPU is immutable once the domain is
  defined, so persist-at-provision (mirroring `accel`) is both cheaper and staleness-free.
- **Static host `<cpu>` from `getCapabilities()` as the discovery source.** Rejected as primary:
  that is the *host* CPU, not the guest-under-host-model CPU (host-model omits non-migratable
  features). `getDomainCapabilities` host-model is the exact predictor. (`getCapabilities` arch
  parsing stays as-is for the `arch` key.)
- **Derive `baseline_level` by expanding the feature set.** Rejected: needs a fully expanded
  feature list and libvirt-version-specific feature naming; a curated model→level table is
  offline-testable and honest (unmapped → omit, never guess).
- **Advertise `host_cpu` for local-libvirt / fault-inject.** Rejected: local is a single
  co-located `host-passthrough` host (no selection ambiguity); fault-inject is a fake. Scoped to
  remote exactly as ADR-0338 scoped `guest_arches` to local.
- **Surface `host_cpu` the same way as `guest_arches` (admission-only, hidden).** Rejected: the
  whole requirement is agent visibility at selection time.
- **A new `resources.cpu` / `systems.cpu` tool.** Rejected: additive fields on existing reads
  match the envelope convention; a new tool is unwarranted agent surface.

## Notes

The issue and ADR-0297 cite a `render_build_domain_xml`/`build_vm.py` second remote renderer;
it does not exist in the tree (remote-libvirt has one renderer, `render_domain_xml`). Discovery
advertises the host once, so a single `host_cpu` per Resource covers every domain on that host.
