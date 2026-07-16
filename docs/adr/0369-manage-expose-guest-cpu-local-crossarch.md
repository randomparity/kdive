# ADR 0369 — Manage and expose the guest CPU across local-libvirt and cross-architecture

- **Status:** Accepted
- **Date:** 2026-07-16
- **Issue:** #1227 (follow-up to #980)
- **Builds on / extends:** ADR-0368 (remote `host_cpu` + `resolved_cpu`), ADR-0294 (local x86
  `host-passthrough`), ADR-0297 (remote `host-model`), ADR-0340 (accel-derived domain XML),
  ADR-0338/0339 (`guest_arches` discovery + persisted `accel`)

## Context

ADR-0368 (#980) closed the **remote-libvirt host-model** slice of guest-CPU visibility: it
advertises `host_cpu` on `resources.describe` and persists a mint-time `resolved_cpu` snapshot on
`systems.get`, both scoped to remote-libvirt where `host-model` makes the guest ISA vary by
landing host. ADR-0368 deliberately left three cases open (its non-goals), each needing a
different source:

- **local x86 + KVM** emits `<cpu mode='host-passthrough'>` (ADR-0294): the guest gets the host's
  *exact* CPU. The honest source is the host's actual `<host><cpu>` (`getCapabilities`), **not**
  the `getDomainCapabilities` host-model block ADR-0368 reads (host-model subtracts non-migratable
  features and so **under-reports** a passthrough guest).
- **local ppc64le + KVM** emits `<cpu mode='host-model'>`: ADR-0368's discovery path works, but its
  `baseline_level` is `x86-64-vN`-only, so a POWER model has no normalized level.
- **any arch + TCG** emits **no `<cpu>`** (ADR-0340), so the guest presents QEMU's **machine-default**
  CPU. That default varies by QEMU version / machine type and is **completely invisible** today. It
  cannot be predicted at discovery by `getDomainCapabilities host-model` (host-model is meaningless
  under TCG); it is only knowable by reading the resolved `<cpu>` of the *running* domain.

The CPU type is load-bearing for kernel work — ISA extensions, errata/mitigation state, and
feature-dependent reproducers all hinge on the exact guest CPU. Two needs follow: an agent must
**see** the guest CPU, and — for a deterministic, portable reproducer — an agent should be able to
**pin** it to a lower, migratable baseline instead of tracking the host.

## Decision

Extend the two ADR-0368 surfaces to local-libvirt and add one control knob, composed so that the
control allow-list *is* the visibility surface. No new tool, RBAC role, or error category. The
default (unpinned) provisioning path is byte-identical to today.

### 1. Discovery `host_cpu` + `selectable_cpus` for local-libvirt (selection time)

`LocalLibvirtDiscovery.list_resources` advertises two additive `capabilities` jsonb keys (no
migration — jsonb is schema-less, as ADR-0338), both agent-facing via `resources.list/describe`:

- **`host_cpu`** — the baseline the *default (unpinned)* guest gets, sourced by the default mode:
  - **x86 host-passthrough** → the host's `getCapabilities()` `<host><cpu>` (model/vendor). This is
    the passthrough-honest source (ADR-0368 rejected the host block for the *host-model* case; the
    reverse holds here — host-model would under-report a passthrough guest).
  - **ppc64le host-model** → `getDomainCapabilities` host-model block (ADR-0368's path).
  - `baseline_level` stays x86-only (below); a POWER model carries `model`/`vendor`/`arch`, no level.
- **`selectable_cpus`** — the sorted list of CPU model names this host can deliver, read from the
  `getDomainCapabilities` `<cpu><mode name='custom'>` `<model usable='yes'>` enumeration. This is the
  honest, host-derived allow-list for the control knob (§3).

Both reads are **guarded**: `arch`/`vcpus`/`memory_mb`/`transports` are computed first (unchanged);
the CPU reads + parse run in a `try` that catches any `libvirt.libvirtError`, logs at warning, and
omits the field. A new advisory field never drops a host from discovery. Parsers live in
`providers/shared/libvirt_xml.py` (defusedxml, domain-free), mirroring `parse_host_cpu` /
`parse_guest_arches`. `host_cpu` reuses ADR-0368's `HostCpu` TypedDict, `host_cpu()` reader,
`HOST_CPU_KEY`, and `host_cpu_json` serializer; `selectable_cpus` adds a sibling
`SELECTABLE_CPUS_KEY` + defensive reader.

### 2. `baseline_level` remains x86-only; non-x86 carries the raw model

`x86-64-vN` has no established upstream analog for POWER, so no per-arch level ladder is invented.
A non-x86 `host_cpu` carries `model` (e.g. `POWER10`) + `vendor` + `arch` with `baseline_level`
omitted — the named POWER model *is* the portable identity. `cpu_baseline.baseline_level` stays the
curated x86-64 table + disable-guard from ADR-0368, unchanged.

### 3. Agent-selectable guest CPU, validated against the advertised subset

Add an optional `cpu` block to the local-libvirt provisioning-profile section (`LibvirtProfile`, a
frozen request input, ADR-0003): `cpu: {model: <name>}`.

- **Omitted (default)** → today's per-arch `kvm_cpu_mode` (host-passthrough x86 / host-model
  ppc64le / no-`<cpu>` TCG). Operator default = host CPU. **Zero behavior change; byte-identical XML.**
- **Pinned** → admission validates `cpu.model ∈` the bound Resource's advertised `selectable_cpus`
  (fail-closed `CONFIGURATION_ERROR` if the host cannot deliver it — never render an unbootable
  domain). The renderer emits `<cpu mode='custom' check='partial'><model>…</model></cpu>` (the
  `custom`-mode block libvirt validates against the same `usable` set discovery read).

The knob's allow-list is exactly the discovery `selectable_cpus` surface, so visibility and control
are one contract: an agent reads `resources.describe` → `selectable_cpus`, picks a portable rung
(e.g. drop a `SapphireRapids` host to `x86-64-v2`), and admission enforces the pick. The
agent-facing wrapper docstring + `Field` text (the FastMCP-serialized contract) direct the agent to
`selectable_cpus` for deterministic-reproducer CPU pinning.

### 4. `resolved_cpu` is live-verified for local, mint-snapshot for remote

Post-provision, the local worker reads the *running* domain's resolved `<cpu>` (via
`virDomainGetXMLDesc(VIR_DOMAIN_XML_UPDATE_CPU)`, which asks libvirt to expand host-passthrough /
host-model / a `custom` pin to a concrete `<model>`) and persists it to the existing
`systems.resolved_cpu` column (ADR-0368 / migration 0070 — **no new migration**). This is the
honest observation for the local cases: it closes the invisible-TCG-machine-default gap, and it
cannot be stale (it is read from the domain that actually booted).

- **Best-effort.** If the expand does not yield a concrete `<model>` (a QEMU/libvirt that leaves the
  TCG machine-default unexpanded), the worker records NULL and logs the reason — never fabricates a
  value and never fails provisioning on the CPU read. The provisioning result does not depend on it.
- **Remote unchanged.** Remote `resolved_cpu` keeps ADR-0368's mint-time snapshot (the remote
  live-read objections — TLS round-trip, worker→DB race — still hold across the network). The
  contract is stated on the `systems.get` field text: *live-verified for local, mint-snapshot for
  remote, NULL when unrecorded/unreadable.*
- **`systems.get` stays a pure DB read** (`system_envelope` reads the row); the single live read
  happens once at the local worker's post-provision boundary, not on the polled read path.

## Consequences

- An agent can see the guest CPU for every provider/accel case (local passthrough, local/native
  host-model, TCG machine-default) and pin a portable baseline for a deterministic reproducer,
  bounded to what the host can actually deliver.
- All surfaces are additive: absence is the graceful default everywhere (unmapped model, malformed
  row, RPC fault, pre-feature host, unreadable resolved CPU). The unpinned provisioning path is
  byte-identical to pre-#1227 output.
- `resolved_cpu` gains a split contract (live-verified local / mint-snapshot remote). This is a
  deliberate honesty trade — the local read is cheap and staleness-free where the remote read is
  not — documented in the field text and this ADR rather than papered over.
- The local worker gains one guarded post-provision libvirt read + a `resolved_cpu` write. This is a
  new local-worker→systems write, but it is on the provision path (not racing teardown as the remote
  case would) and is best-effort (NULL on any fault), so it adds no new provisioning failure mode.
- `selectable_cpus` can be a long list on a modern host; it is advisory agent surface (like
  `guest_arches` is admission surface) and the field text points at the `x86-64-vN` rungs as the
  portable picks rather than enumerating semantics for every named model.
- Discovery gains two guarded libvirt reads on the cold path; the TCG live-read residual risk is
  contained by the best-effort NULL fallback.

## Considered & rejected

- **Reuse ADR-0368's `getDomainCapabilities` host-model block as the local x86 source.** Rejected:
  host-model under-reports a `host-passthrough` guest (it subtracts non-migratable features the
  passthrough guest actually gets). Local x86 must read the host `<host><cpu>` block. (ADR-0368
  rejected the host block for the *remote host-model* case — the mirror-image reason.)
- **Keep `resolved_cpu` as a mint-time snapshot for local too (ADR-0368's mechanism).** Rejected for
  local: it cannot express the TCG machine-default (there is no advertised `host_cpu` to snapshot
  under TCG — no `<cpu>` is emitted), which is the issue's highest-value case. A local post-provision
  read is cheap (co-located, no TLS) and staleness-free. The remote snapshot is kept precisely
  because the remote objections (TLS, teardown race) do not apply locally.
- **Invent a ppc64le `baseline_level` ladder.** Rejected: no established upstream POWER micro-arch
  level; a fabricated rung risks advertising a misleading level. The named POWER model is the
  portable identity.
- **Operator-only CPU pin (systems.toml policy, like `guest_egress`).** Rejected: the motive is
  per-System agent reproducibility (a repro pins its own portable CPU); a host-wide operator policy
  forces every System on a host to the same pin. The operator default (host CPU) is preserved; the
  agent selects per-System within the host-advertised allow-list.
- **Free-form operator allow-list config for the knob.** Rejected as redundant: the host's own
  `getDomainCapabilities` `usable` set is the honest, self-maintaining allow-list. A separate config
  layer adds precedence rules and drift with no gain.
- **Block the PR on a fully live-verified TCG `resolved_cpu`.** Rejected (operator decision): the
  visibility + control surfaces are low-risk and land regardless; the TCG machine-default read is
  best-effort (NULL + logged when the API is unreliable) so the hardest read never blocks the
  feature. A hard TCG-expand guarantee, if needed, is a follow-up.
- **A new `resources.cpu` / `systems.cpu` tool.** Rejected: additive fields on existing reads match
  the envelope convention (ADR-0368); a new tool is unwarranted agent surface.

## Notes

The TCG live-read reliability is the one API-uncertain piece; it is proven on the epic-#1139 dev box
(foreign qemu emulator) by the `live_vm` / `live_vm_tcg` proofs, and degrades to a logged NULL where
the expand does not resolve. ADR-0368's remote surfaces and mint mechanism are unchanged.
