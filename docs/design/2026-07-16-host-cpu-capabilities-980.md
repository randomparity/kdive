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
2. **On a provisioned System**, surface the CPU baseline the System was minted against on
   `systems.get`, alongside the existing `accel` field, resolved at mint from the bound
   Resource's advertised `host_cpu` (the same mechanism `accel` uses), so an agent can read a
   specific System's pinned CPU baseline without re-deriving it from the fleet.
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
  "baseline_level": "x86-64-v3",    # nominal name-derived level; omitted if unmapped OR a
                                    # level-defining feature is disabled in the host-model block
}
```

The four fields are the persisted, agent-facing shape. `parse_host_cpu` additionally reads the
host-model block's `<feature policy='disable'>` names to compute the disable-guard below, but
that raw feature list is **not** stored — only the guarded `baseline_level` result is.

- **Populated** by `RemoteLibvirtDiscovery.list_resources` from the connection's
  domain-capabilities host-model block (the exact model libvirt synthesizes for a
  `host-model` guest on this host). This is the honest predictor of the guest CPU, since the
  renderer emits `host-model`. **The `getDomainCapabilities` call is parameterized from the same
  sources the provisioner uses**, not literals, because host-model resolution is sensitive to
  `(arch, machine, virttype)`:
  - `virttype="kvm"` — the renderer emits `<domain type="kvm">`; host-model is meaningless under
    TCG, so this is load-bearing.
  - `machine=config.machine` — the **same** `REMOTE_LIBVIRT_MACHINE`-or-default value the renderer
    reads (`config.py`, `provisioning.py` passes `machine=config.machine`). Pinning a literal
    `"pc"` would mispredict for an operator who set `q35`.
  - `arch=` the host arch already parsed at discovery (`parse_capabilities_arch`,
    `discovery.py`) — there is no profile at discovery time.
  - `emulator=` **omitted** — the renderer emits no `<emulator>` element, so libvirt picks the
    same default emulator for both the discovery query and the built domain; there is nothing to
    "resolve the same way".
- **The `getDomainCapabilities` RPC call is guarded.** The other capabilities (`arch`, `vcpus`,
  `memory_mb`, `transports`, connect refs) are computed **first**, from `getInfo()` /
  `getCapabilities()` exactly as today. The `getDomainCapabilities` call and its parse run in a
  `try` that catches **any** `libvirt.libvirtError` (an older libvirt lacking the API, a
  transient RPC fault): on failure it logs at warning and **omits** `host_cpu`, and the
  ResourceRecord still discovers with every existing capability intact. A new advisory field
  must never drop a host from discovery — the same "observability never fails the primary path"
  rule Surface 1 and 2 both follow.
- **Parsed** by a new `parse_host_cpu(dom_caps_xml)` in `providers/shared/libvirt_xml.py`,
  defusedxml over the libvirtd trust boundary, returning `None` on any parse fault, empty XML,
  or a host-model block with no concrete `<model>` (never crashes discovery — mirrors
  `parse_capabilities_arch` / `parse_guest_arches`).
- **`baseline_level`** starts from a shared, curated x86-64 model→level table in
  `domain/platform/` (see Open questions → resolved) keyed on the model name, then is
  **disable-guarded** against the host-model block: `parse_host_cpu` also reads the block's
  `<feature policy='disable'>` names, and the level mapper **omits** `baseline_level` when any
  feature that *defines* the candidate level is explicitly disabled (host-model subtracts
  non-migratable / host-absent features, so a `Skylake` guest with AVX2 stripped must not
  advertise `v3`). The guard is a targeted check over the small, fixed set of level-boundary
  features (v2: `sse4.2`/`popcnt`; v3: `avx2`/`bmi2`/`avx`; v4: `avx512f`), **not** a full feature
  expansion (that alternative was rejected in ADR-0368 for needing the whole expanded feature list
  and version-specific naming); it only reacts to an explicit disable already present in the XML.
- **`baseline_level` is a nominal, name-derived upper bound, not a guaranteed floor.** Even
  disable-guarded, the level reflects the named model's spec minus explicitly-disabled features;
  a feature the base model implies but that the host silently lacks is not enumerated and cannot
  be caught here. **Agent contract:**
  - *Absent level* means *unknown* (model present, not in table, or a defining feature was
    disabled), **not** "below v1"/"unsupported" — fall back to comparing the raw `model` or treat
    the host as unverified. "Present model, absent level" is an expected steady state on new
    hardware, not an error.
  - *A present level* is the **nominal** level of the named model; for a hard instruction-set
    requirement an agent must confirm against the running guest, never treat the level as a
    guaranteed floor. This is stated in the wrapper docstring / field text so the contract is
    visible at call time.
- **Typed read** via a defensive `host_cpu()` reader + `HOST_CPU_KEY` + `_KNOWN_KEYS` entry in
  `domain/catalog/resource_capabilities.py`, returning a `HostCpu` TypedDict or `None`
  (mirrors `guest_arches()` — a stale/hand-edited row never crashes a consumer).
- **Agent-facing**: flattened into the envelope `data` by `resource_capability_data`
  (`mcp/tools/_resource_envelopes.py`), so `resources.list` / `resources.describe` show it at
  selection time. This is the one deliberate divergence from `guest_arches`, which is
  admission-only and intentionally *not* surfaced — for #980 agent visibility **is** the point.

### Surface 2 — `systems.get` `resolved_cpu` readout (resolved at mint)

Resolve the System's CPU baseline **at mint**, from the bound Resource's advertised `host_cpu`,
and persist it on the System row — the *actual* mechanism `accel` uses (ADR-0339), not a
provision-time worker write-back.

`accel` is resolved inside the mint transaction by `_resolve_new_system_accel`
(`services/systems/admission.py`) from the bound Resource's advertised `guest_arches()`, and
written in the same INSERT that creates the System; the remote worker **discards** the accel it
is handed (`install.py` `del accel`) and never writes the systems row. `resolved_cpu` follows
that exact path — there is no new worker→DB write-back, and no dependency on libvirt expanding
`host-model` in a running domain's live XML (which is not guaranteed).

- **Migration 0070** adds a nullable `resolved_cpu jsonb` column to `systems` (no default; NULL
  means "no CPU baseline recorded" — a pre-migration System, a local/fault System, or a remote
  Resource that advertises no `host_cpu` because it has not been re-registered since this
  feature shipped). Mirrors `0067_system_accel.sql`.
- **Resolved at mint** from the bound Resource's `capability_view.host_cpu()`, written as `HostCpu`
  (or NULL) into the mint INSERT. `_resolve_new_system_accel` already loads that Resource
  (`RESOURCES.get`) inside the same mint transaction; to avoid a second round-trip, resolve accel
  **and** `resolved_cpu` from a **single** Resource load — fetch the Resource once and pass its
  `capability_view` to both resolvers (or fold both into one helper). No live libvirt call; the
  value is the `host_cpu` the host advertised at its last registration, frozen onto this System.
- **Frozen per System.** Because it is a mint-time snapshot, a later host re-registration or
  hardware change does **not** retroactively alter a provisioned System's `resolved_cpu` — it
  records the baseline the System was minted against, which is the honest post-selection answer.
- **Surfaced** by `system_envelope` as `data["resolved_cpu"]` (sibling of `accel`), a pure DB
  read — `systems.get` stays a cheap, libvirt-free read with no new failure mode, and the mint
  path is fully unit-testable (no `live_vm` gate required to prove the persist).

## Acceptance criteria

1. `resources.describe` on a remote-libvirt host whose domain-capabilities advertise a
   host-model CPU returns `data.host_cpu` with `model`, `vendor`, `arch`, and (for a mapped
   x86-64 model) `baseline_level`. Verified by a unit test with an injected fake connection.
2. A remote-libvirt host whose domain-capabilities XML is malformed, or omits a host-model CPU,
   discovers successfully with **no** `host_cpu` key (never raises). Verified by a unit test.
3. `parse_host_cpu` returns `None` on malformed/empty XML and a populated `HostCpu` on a real
   host-model capabilities document. Property/edge unit tests cover empty, malformed, missing
   `<model>`, and non-x86 arch (level omitted).
4. The x86-64 level mapper returns `x86-64-v{1..4}` for representative named models, `None` for an
   unknown model, and `None` when a level-defining feature for the mapped level is present with
   `policy='disable'` in the host-model block (the disable-guard). Unit-tested against a table of
   known models and a disable case (e.g. a `v3` model with `avx2` disabled → `None`).
5. `resources.describe` on **local-libvirt** and **fault-inject** hosts is unchanged (no
   `host_cpu`) — a regression test asserts the key is absent.
6. When `getDomainCapabilities` **raises** `libvirtError`, the resource still discovers with its
   `arch`/`vcpus`/`memory_mb`/`transports` intact and `host_cpu` omitted — verified by a unit
   test with a fake connection whose `getDomainCapabilities` raises (the pre-feature record is
   never dropped).
7. `_resolve_new_system_cpu` writes the bound Resource's advertised `host_cpu` onto a newly
   minted System, and NULL when the Resource advertises none. A unit/service test mints a System
   against a Resource with `host_cpu` and asserts the row carries it, and against one without and
   asserts NULL — the persist path is proven **without** a `live_vm` gate.
8. Migration 0070 adds a nullable `resolved_cpu` column; the migration test asserts the column
   exists, is nullable, and a System with no resolved CPU reads back `None`.
9. `systems.get` on a System with a persisted `resolved_cpu` returns `data.resolved_cpu`; on a
   System without one, the field is absent/`null`. `systems.get` performs no libvirt call.
10. A `live_vm`-gated test discovers an operator-provided remote host and asserts its advertised
    `host_cpu.model` is non-empty; for a model the test pins as known-in-table it also asserts
    `baseline_level` is ≥ `x86-64-v2` (the EL9 floor). For an unmapped model the assertion is only
    that `baseline_level` is absent (never a wrong level). Skips cleanly without the remote host env.
11. A `live_vm`-gated **reconcile** test closes the prediction-vs-reality loop (test-only — not a
    product read path): it provisions (or inspects an operator-provided) domain on the same host
    and reads the running domain's CPU with libvirt's CPU-expand flag
    (`VIR_DOMAIN_XML_UPDATE_CPU`), which asks libvirt to resolve `host-model` to a concrete
    `<model>`. **Deterministic outcome:** (a) if the expanded XML carries a concrete `<model>`,
    assert it **equals** the discovery-advertised `host_cpu.model` — the falsifiable proof that
    the `getDomainCapabilities` `machine`/`virttype`/`arch` pinning predicts what the renderer
    builds (a mispin fails it); (b) if the XML still carries `<cpu mode='host-model'/>` with **no**
    concrete `<model>` (a libvirt/host that does not expand it — the case the spec flags as not
    guaranteed), the test **skips with an explicit recorded reason**, never silently passes or
    fails. Skips cleanly without the remote host/image env.
12. `just ci` is green (lint, type, lint-shell, lint-workflows, check-mermaid, test), including
    regenerated generated docs.

## Failure modes & edges

- **Malformed/absent domain-capabilities XML** → `parse_host_cpu` returns `None`, `host_cpu`
  omitted, discovery succeeds.
- **`getDomainCapabilities` raises `libvirtError`** (old libvirt without the API, transient RPC
  fault) → caught inside `list_resources`, logged at warning, `host_cpu` omitted, and the
  resource still discovers with `arch`/`vcpus`/`memory_mb`/`transports` intact. A new advisory
  field never drops a host from discovery.
- **Remote host unreachable at discovery** → existing `TRANSPORT_FAILURE` path is unchanged
  (the `getDomainCapabilities` call uses the same already-open connection; it adds no new connect).
- **Domain-capabilities reports `host-model` with no concrete `<model>`** (a host libvirt cannot
  model) → `host_cpu` omitted rather than advertising an empty model.
- **Unknown/non-x86 CPU model** → `baseline_level` omitted, raw `model` still advertised.
- **host-model disables a level-defining feature** (e.g. AVX2 stripped by microcode/mitigations on
  a model the table maps to `v3`) → the disable-guard omits `baseline_level` rather than
  advertising an overstated level; the raw `model` is still advertised.
- **host-model omits a base-model-implied feature the host silently lacks** → not detectable from
  the block (the feature is simply not enumerated), so `baseline_level` remains a nominal upper
  bound; the agent contract documents that a present level is not a guaranteed floor.
- **Bound Resource advertises no `host_cpu`** (local/fault host, or a remote host not
  re-registered since this feature shipped) → `_resolve_new_system_cpu` records NULL; `systems.get`
  omits `resolved_cpu`.
- **Stale/hand-edited jsonb row** → defensive typed readers drop malformed values, never crash.
- **Pre-migration Systems** → `resolved_cpu` NULL, field absent on `systems.get`.
- **`resolved_cpu` NULL is intentionally coarse** — it means "no CPU baseline recorded" across
  all of {pre-migration, local/fault, un-refreshed remote}. This matches `accel`'s NULL semantics
  (ADR-0339). Because `resolved_cpu` is resolved at mint from advertised data (not read from a
  live domain), there is no "feature ran but produced nothing" case to distinguish: if the bound
  Resource advertises `host_cpu`, the System carries it; if not, NULL. The freshness question is a
  Resource-registration concern (see Rollout), not a per-System silent failure.

## Considered & rejected (summarized; full rationale in ADR-0368)

- Live-read domain XML on every `systems.get`, or a provision-time worker→systems write-back
  (rejected: the live read bolts a TLS round-trip + new failure modes onto a hot read and depends
  on libvirt expanding `host-model` in the running XML — not guaranteed; the worker write-back is
  a new DB path the remote worker does not have today, racing teardown/reap and provable only by
  an operator-run `live_vm` test. Mint-time resolution from advertised `host_cpu` is the actual
  `accel` mechanism: cheap, staleness-free, and unit-testable at admission).
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
  selection-time prediction across the fleet, `systems.get` for a specific System's pinned CPU
  baseline (resolved at mint from that System's bound host — see the Surface 2 redesign, which
  replaced the originally-sketched live-XML readout after the spec review found it needed a
  non-existent worker write-back path).
- **How to derive `baseline_level`?** → curated x86-64 model→level table in `domain/platform/`,
  `None` for unmapped models (see ADR-0368).

## Rollout & freshness

The remote-libvirt capabilities row is `insert-if-absent, refreshed only by re-registration`
(`RemoteLibvirtDiscovery` module docstring) — the same lifecycle as the existing
`arch`/`vcpus`/`memory_mb` it already carries. Consequences for this feature:

- **Existing remote hosts must be re-registered** (`resources.register_*` / `reconcile_resources`
  over the config overlay) to gain `host_cpu`; until then `resources.describe` omits it and a
  newly minted System's `resolved_cpu` is NULL. This is expected, not a defect: the feature is
  additive and, when unpopulated, degrades to **absent** rather than emitting a value. The rollout
  note is called out in the operator docs delta (host-registration section).
- **`host_cpu` is a registration-time snapshot, and can lag the host.** If a host's
  CPU/microcode/libvirt changes after registration, the advertised `host_cpu` is stale until the
  host is re-registered — identical to how `vcpus`/`memory_mb` behave today. `resolved_cpu`, being
  a mint-time snapshot of that advertised value, can therefore record a baseline that lags the
  host's *current* host-model resolution (e.g. a microcode update that disables a feature). It is
  **the baseline advertised at the bound host's last registration, not a live-verified reading** —
  honest for planning and for "what this host claimed", but an agent needing certainty about a
  specific instruction-set extension should confirm against the running guest, and operators
  should re-register a host after a CPU/microcode/libvirt change. This registration-driven
  freshness model (no per-field `discovered_at` timestamp) is the existing capabilities contract;
  adding a freshness marker is a possible follow-up, out of scope here.

## Notes

- **Stale premise correction:** the issue and ADR-0297 reference a
  `render_build_domain_xml`/`build_vm.py` second remote renderer. That file/function does not
  exist in the tree; remote-libvirt has exactly one domain renderer (`render_domain_xml`,
  `providers/remote_libvirt/lifecycle/xml.py`). Discovery advertises the host once, so a single
  `host_cpu` per resource covers both System and (nonexistent-separate) build paths.
