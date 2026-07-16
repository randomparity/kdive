# Manage and expose the guest CPU across local-libvirt and cross-architecture (#1227)

- **Status:** Draft
- **Date:** 2026-07-16
- **Issue:** #1227 (follow-up to #980 / ADR-0368)
- **ADR:** [ADR-0369](../adr/0369-manage-expose-guest-cpu-local-crossarch.md)

## Problem

ADR-0368 (#980) advertises `host_cpu` at `resources.describe` and persists a mint-time
`resolved_cpu` on `systems.get`, **scoped to remote-libvirt**. It deliberately left three
local/cross-arch cases open, each with a different honest source and none visible today:

| config | `<cpu>` emitted | guest gets | visibility today |
|---|---|---|---|
| local x86_64 + KVM | `host-passthrough` (ADR-0294) | host's **exact** CPU | none |
| local ppc64le + KVM | `host-model` | host-model baseline | none |
| local/remote **any arch + TCG** | **none** (ADR-0340) | QEMU **machine-default** | none |
| remote x86 (host-model) | `host-model` (ADR-0297) | migratable baseline | **#980 (done)** |

The CPU type is load-bearing for kernel work (ISA extensions, errata/mitigation state,
feature-dependent reproducers). Two needs: an agent must **see** the guest CPU across every case,
and — for a deterministic, portable reproducer — should be able to **pin** it to a lower migratable
baseline instead of tracking the host. The CPU mode is currently hardcoded per-arch in
`domain/platform/arch_traits.py::_TRAITS` (`kvm_cpu_mode`) with no operator/agent knob.

## Goals

1. **See it (local).** Advertise `host_cpu` on `resources.list/describe` for local-libvirt, sourced
   honestly per default accel mode (x86 passthrough from the host `<cpu>`; ppc64le host-model from
   domain-capabilities), extending ADR-0368's remote surface to local.
2. **Pin it.** Let an agent select the guest CPU model per-System from a host-advertised allow-list
   (`selectable_cpus`), validated fail-closed at admission; the operator default stays "host CPU"
   (today's mode) with a byte-identical unpinned path.
3. **Verify it (local).** Make `systems.get`'s `resolved_cpu` a **live reading** of the running
   local domain's resolved `<cpu>` (closing the invisible-TCG-machine-default gap), best-effort NULL
   when unreadable; keep remote `resolved_cpu` as ADR-0368's mint-snapshot.
4. Non-x86 arches carry the raw model with no invented `baseline_level`.
5. Add no new tool, RBAC role, or error category. Change no unpinned provisioning behavior.

## Non-goals

- Reopening remote `host_cpu` / `resolved_cpu` (ADR-0368 unchanged) or the remote `host-model` /
  local `host-passthrough` **defaults** (ADR-0294/0297 unchanged) — the knob adds an *opt-in*
  override, it does not change defaults.
- A non-x86 `baseline_level` taxonomy (Q2 resolved: raw model only).
- Operator-side CPU policy config (systems.toml). The knob is agent-owned per-System; the operator
  default is the existing host-CPU mode. (Rejected in ADR-0369.)
- Gating placement/scheduling on CPU. The `selectable_cpus` check validates a *pinned* model against
  the bound host only; it does not steer allocation to a host that can satisfy a pin.
- A hard guarantee that the TCG machine-default always live-reads (best-effort by operator decision;
  NULL + logged where the libvirt expand does not resolve).
- fault-inject CPU surfaces (a fake; unchanged).

## Design overview

Three surfaces, composed so the control allow-list **is** the visibility surface. Structured as
three phases (→ three commit groups in one PR).

### Phase A — local discovery: `host_cpu` + `selectable_cpus`

`LocalLibvirtDiscovery.list_resources` advertises two additive `capabilities` jsonb keys (no
migration), agent-facing via `resource_capability_data` (`mcp/tools/_resource_envelopes.py`):

```
capabilities.host_cpu       = {model, vendor?, arch, baseline_level?}   # reuses ADR-0368 shape
capabilities.selectable_cpus = ["<model-name>", ...]                    # sorted, usable models
```

- **`host_cpu` source is the default accel mode:**
  - **x86 host-passthrough** → parse the host's `getCapabilities()` `<host><cpu><model>`/`<vendor>`.
    A new `parse_host_capabilities_cpu(caps_xml)` in `providers/shared/libvirt_xml.py` (defusedxml).
    ADR-0368's `getDomainCapabilities` host-model source would **under-report** a passthrough guest,
    so passthrough reads the host block. `baseline_level` runs the existing
    `cpu_baseline.baseline_level(model, disabled=())` (no disabled features in the host block).
  - **ppc64le host-model** → reuse ADR-0368's `parse_host_cpu(getDomainCapabilities(...))`. Level
    omitted (non-x86).
- **`selectable_cpus` source** → `getDomainCapabilities` `<cpu><mode name='custom'>`
  `<model usable='yes'>` names, parsed by a new `parse_selectable_cpus(dom_caps_xml)` (defusedxml),
  returning a sorted, de-duplicated list; `None`/empty when the mode is unsupported or the parse
  faults. This is the exact set libvirt will accept in a `custom`-mode `<model>`, so it is the
  honest allow-list for Phase B.
- **The libvirt reads (`getDomainCapabilities`, and `getCapabilities` for the x86 host `<cpu>`) are
  guarded.** `arch`/`vcpus`/`memory_mb`/`transports` are computed first (unchanged); the CPU reads +
  parse run in a `try` catching any `libvirt.libvirtError`, logging at warning and omitting the
  field. A new advisory field never drops a host from discovery. (`_LibvirtConn` in local
  `discovery.py` gains `getDomainCapabilities`; the test fake implements it — mirrors ADR-0368's
  widening of the remote `_LibvirtConn`.)
- **Typed reads** in `domain/catalog/resource_capabilities.py`: reuse `host_cpu()` / `HOST_CPU_KEY`;
  add `selectable_cpus()` reader + `SELECTABLE_CPUS_KEY` + `_KNOWN_KEYS` entry (defensive: drops a
  malformed row to `None`/empty, mirrors `guest_arches()`). Add `selectable_cpus` to
  `resource_capability_data` so it flows to `resources.describe`.

### Phase B — agent-selectable CPU pin, validated against `selectable_cpus`

- **Profile field.** Add optional `cpu: LibvirtCpuPin | None` to `LibvirtProfile`
  (`profiles/provisioning.py`), where `LibvirtCpuPin` is a frozen `_ProfileBase` with
  `model: NonEmptyStr`. `extra="forbid"` already rejects unknown keys. Omitted → default mode.
- **Renderer.** `_append_guest_cpu` (both `render_domain_xml` and `render_customization_domain_xml`
  in `providers/local_libvirt/lifecycle/xml.py`) takes an optional `cpu_model: str | None`. When
  set, emit `<cpu mode='custom' check='partial'><model>NAME</model></cpu>` (regardless of KVM/TCG —
  a pinned model is valid under both). When `None`, today's behavior exactly (host-passthrough /
  host-model under KVM, nothing under TCG) — **byte-identical unpinned output** (regression-tested).
  The provisioner threads `profile...cpu.model` into the render call.
- **Admission validation.** At mint, when the profile pins `cpu.model`, validate it against the
  bound Resource's `capability_view.selectable_cpus()`; reject `CONFIGURATION_ERROR` with an
  actionable message (the pinned model + the advertised set) if absent. This is a new check in the
  `_resolve_new_system_bindings` / profile-policy path (co-located with the accel mis-arch and
  fadump checks, which already fail-closed there). A profile with no bound Resource advertising
  `selectable_cpus` (local host not re-discovered) and a pin → `CONFIGURATION_ERROR` (fail-closed:
  never render a pin the host cannot be shown to support).
- **Agent-facing contract.** Update the wrapper docstring + `Field(description=...)` for the pin
  field and for `resources.describe`'s `selectable_cpus` (the FastMCP-serialized surface), pointing
  the agent at `selectable_cpus` and the `x86-64-vN` portable rungs for deterministic-reproducer
  pinning. Update the profile-schema / config docs the generated-doc guard covers.

### Phase C — live-verified `resolved_cpu` (local)

Post-provision, the **local** worker reads the running domain's resolved `<cpu>` and persists it to
the existing `systems.resolved_cpu` column (ADR-0368 / migration 0070 — **no new migration**).

- **Read.** `virDomainGetXMLDesc(VIR_DOMAIN_XML_UPDATE_CPU)` on the running domain asks libvirt to
  expand host-passthrough / host-model / a `custom` pin to a concrete `<model>`. A new
  `parse_domain_resolved_cpu(domain_xml)` (defusedxml) extracts `{model, vendor?, arch,
  baseline_level?}` (level via the existing x86 table; omitted non-x86). The read happens once, at
  the local worker's post-provision boundary, after the domain is running.
- **Persist.** Write the parsed `HostCpu` (or NULL) to `systems.resolved_cpu` via the repository.
  This is a new local-worker→systems write; scope it to the narrowest existing post-provision write
  point (identified in the plan against `providers/local_libvirt/lifecycle/provisioning.py` +
  `db/repositories.py`). It is on the provision path (not racing teardown/reap), and best-effort.
- **Best-effort / never blocks.** Any `libvirt.libvirtError`, a parse fault, or an unexpanded
  `<cpu mode='host-model'/>` / TCG machine-default with no concrete `<model>` → record NULL, log the
  reason at info/warning, and **do not fail provisioning**. The provisioning result is independent
  of the CPU read (mirrors ADR-0368's "observability never fails the primary path").
- **Contract.** `resolved_cpu` becomes *live-verified for local, mint-snapshot for remote, NULL when
  unrecorded/unreadable* — stated in the `systems.get` wrapper docstring/field text and ADR-0369.
  Remote keeps ADR-0368's `_resolve_new_system_bindings` mint path unchanged. `systems.get` stays a
  pure DB read (`system_envelope` reads the row; no live call on the polled path).

## Acceptance criteria

1. `resources.describe` on a local-libvirt x86 host returns `data.host_cpu` with `model`/`vendor`/
   `arch`/`baseline_level` sourced from the host `<host><cpu>` block (a `SapphireRapids` host →
   `x86-64-v4`). Unit test with an injected fake connection.
2. `resources.describe` on a local-libvirt host returns `data.selectable_cpus` (sorted usable model
   names from domain-capabilities custom mode). Unit test.
3. `parse_host_capabilities_cpu` and `parse_selectable_cpus` return `None`/empty on malformed/empty
   XML and a populated result on a real capabilities document; edge tests cover missing `<model>`,
   no custom mode, and non-x86 (level omitted). Property/edge unit tests.
4. When `getDomainCapabilities` **or** the host-`<cpu>` read raises `libvirtError`, the resource
   still discovers with `arch`/`vcpus`/`memory_mb`/`transports` intact and the CPU field(s) omitted.
   Unit test with a fake whose reads raise.
5. `resources.describe` on **fault-inject** is unchanged (no `host_cpu`/`selectable_cpus`) — a
   regression test asserts the keys are absent.
6. A `LibvirtProfile` with `cpu.model` set renders `<cpu mode='custom' check='partial'><model>…`;
   a profile without `cpu` renders **byte-identical** XML to the pre-#1227 renderer for x86-KVM,
   ppc64le-KVM, and TCG (three golden/regression assertions).
7. Admission **accepts** a pin whose `cpu.model ∈ selectable_cpus` and **rejects** (`CONFIGURATION_
   ERROR`, message names the model + advertised set) a pin not in the set, and a pin when the bound
   Resource advertises no `selectable_cpus`. Service/unit tests for all three.
8. A pinned System's provisioning end-to-end (renderer + admission) uses the pinned model; unit/
   service level (no `live_vm` gate needed to prove admission + render).
9. Phase C: given a running domain whose `VIR_DOMAIN_XML_UPDATE_CPU` XML carries a concrete
   `<model>`, `parse_domain_resolved_cpu` returns the `HostCpu`, and the post-provision path writes
   it to `systems.resolved_cpu`; given an unexpanded/`<model>`-less XML or a raising read, it writes
   NULL and does not fail provisioning. Unit tests with fake domain XML + a raising fake.
10. `systems.get` on a System with a live-verified `resolved_cpu` returns `data.resolved_cpu`; on one
    without, the field is absent/`null`. `systems.get` performs **no** libvirt call (pure row read).
11. Remote `resolved_cpu` (ADR-0368 mint-snapshot) and remote `host_cpu` are unchanged — a
    regression test asserts the remote mint path still snapshots.
12. `live_vm` (x86, native KVM): provision with `cpu.model = x86-64-v2` (a `selectable_cpus` rung),
    assert the running domain resolves to it and `systems.get.resolved_cpu` reflects it. Skips
    cleanly without the KVM host.
13. `live_vm_tcg` (ppc64le, TCG on the epic-#1139 box): provision a ppc64le System; assert
    `resources.describe` advertised `host_cpu`/`selectable_cpus`, and `resolved_cpu` is either the
    live-read machine-default `<model>` **or** NULL-with-logged-reason (best-effort). Skips cleanly
    without the foreign emulator.
14. `just ci` green (lint, type, lint-shell, lint-workflows, check-mermaid, test), including
    regenerated generated docs.

## Failure modes & edges

- **Malformed/absent host-`<cpu>` or domain-capabilities XML** → parser returns `None`/empty; the
  field is omitted; discovery succeeds.
- **`getDomainCapabilities` / host-`<cpu>` read raises** (old libvirt, transient fault) → caught in
  `list_resources`, logged, field omitted, resource discovers intact.
- **Host advertises no custom mode / empty usable set** → `selectable_cpus` omitted; a pin against
  that host fails admission `CONFIGURATION_ERROR` (fail-closed).
- **Pinned model not in `selectable_cpus`** → admission `CONFIGURATION_ERROR` (never render an
  unbootable custom `<cpu>`).
- **Unpinned profile** → byte-identical legacy render; no new failure mode.
- **Phase C read raises / returns no concrete `<model>` / TCG machine-default unexpanded** →
  `resolved_cpu` NULL, logged, provisioning succeeds (best-effort).
- **Non-x86 model** → `baseline_level` omitted; raw `model` advertised/persisted.
- **Stale/hand-edited jsonb capabilities row** → defensive readers drop malformed values.
- **Local host not re-discovered since #1227** → no `host_cpu`/`selectable_cpus`; unpinned
  provisioning works; a pin fails fail-closed; `resolved_cpu` still live-reads at provision (it does
  not depend on discovery).
- **`resolved_cpu` NULL is coarse** — {pre-feature, remote-advertising-none, local-read-unreadable}.
  The live-read local path distinguishes "read produced nothing" (NULL + log) from "not attempted";
  the split contract is documented so an agent treats NULL as "unknown", not "no CPU".

## Considered & rejected (full rationale in ADR-0369)

- Reuse `getDomainCapabilities` host-model as the local x86 source (under-reports a passthrough
  guest — read the host `<cpu>`).
- Mint-time `resolved_cpu` for local (cannot express the TCG machine-default — the highest-value
  case; local live-read is cheap + staleness-free).
- ppc64le `baseline_level` ladder (no upstream POWER analog; raw model is the identity).
- Operator-only / config-driven CPU pin (motive is per-System agent reproducibility; the host's
  `usable` set is the self-maintaining allow-list).
- Blocking on a fully-live TCG `resolved_cpu` (best-effort NULL keeps the low-risk surfaces landing).
- A new `resources.cpu` / `systems.cpu` tool (additive fields match the envelope convention).

## Open questions (resolved)

- **Q1 mechanism** → **Both**: discovery `host_cpu` (predict, selection-time) **and** live-verified
  `resolved_cpu` (observe, per-System). Composed, not conflicting.
- **Q2 non-x86 level** → **raw model only** (no invented ladder).
- **Q3 control** → **agent selects from the host-advertised subset**; operator default = host CPU;
  the `selectable_cpus` surface is the allow-list.
- **Q4 honesty** → **live-verified for local, mint-snapshot for remote**; documented split contract.
- **PR shape** → one PR, phased commits (A → B → C). **TCG-read risk** → ship A+B; C best-effort.

## Rollout & freshness

- Existing local hosts must be **re-discovered** (`resources.reconcile`/register over the config
  overlay) to gain `host_cpu`/`selectable_cpus`; until then the fields are absent and a pin
  fails fail-closed. Additive and degrades to absent, not a wrong value.
- `host_cpu`/`selectable_cpus` are registration-time snapshots (as `vcpus`/`memory_mb`); a host CPU/
  libvirt change is stale until re-discovery. `resolved_cpu` (local) is the live-verified counter to
  that staleness for a specific System.

## Notes

- The TCG live-read reliability is the one API-uncertain piece (Phase C); it degrades to a logged
  NULL and is proven on the epic-#1139 dev box. #980's AC#11 already prototyped
  `VIR_DOMAIN_XML_UPDATE_CPU` as a test-only reconcile read — Phase C promotes that read to a
  best-effort product path for local only.
- The exact post-provision write point and the precise `VIR_DOMAIN_XML_UPDATE_CPU` behavior for the
  local TCG machine-default are pinned in the implementation plan against the real code / a dev-box
  spike, not assumed here.
