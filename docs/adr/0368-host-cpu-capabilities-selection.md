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
  `getDomainCapabilities(emulator, arch, machine, virttype)` `<cpu><mode name='host-model'>`
  block — the exact model libvirt synthesizes for a `host-model` guest on this host, which is
  what the renderer emits. The arguments are **derived from the same sources the provisioner
  uses** — `virttype="kvm"` (the renderer's `<domain type="kvm">`; host-model is meaningless
  under TCG), `machine=config.machine` (the same `REMOTE_LIBVIRT_MACHINE`-or-default the renderer
  reads, **not** a literal `"pc"` — an operator may set `q35`), `arch=` the host arch parsed at
  discovery, and `emulator` **omitted** (the renderer emits no `<emulator>`, so libvirt picks the
  same default for the query and the built domain). host-model resolution is sensitive to
  `(arch, machine, virttype)`, so the no-arg form would let libvirt pick its own default and
  predict a CPU for a configuration the provisioner does not build. This widens the duck-typed
  `_LibvirtConn` protocol (`connection/transport.py`) with `getDomainCapabilities`, satisfied by
  the real binding and the test fake. Discovery is a cold path, so the extra libvirt call is
  immaterial.
- **The call is guarded.** `arch`/`vcpus`/`memory_mb`/`transports` are computed first (unchanged
  from today); the `getDomainCapabilities` call + parse run in a `try` that catches any
  `libvirt.libvirtError` (an older libvirt without the API, a transient RPC fault), logs at
  warning, and omits `host_cpu` — the ResourceRecord still discovers with every existing
  capability intact. A new advisory field must never drop a host from discovery
  (`parse_host_cpu` returning `None` guards a parse fault; the `try` guards an RPC raise before
  any XML exists).
- **Parser** `parse_host_cpu(dom_caps_xml)` in `providers/shared/libvirt_xml.py`, defusedxml
  (the XML crosses the libvirtd trust boundary), returning `None` on any parse fault or a
  host-model block with no concrete `<model>` — discovery never crashes and never advertises an
  empty model (mirrors `parse_capabilities_arch`/`parse_guest_arches`). `supported` arch set is
  **not** needed here (this is a single host CPU, not a guest-arch enumeration).
- **`baseline_level`** from a curated x86-64 model→level table in `domain/platform/` (a small
  module-level mapping keyed on the libvirt/QEMU model name), **disable-guarded**: `parse_host_cpu`
  also reads the host-model block's `<feature policy='disable'>` names, and the mapper omits the
  level when a feature that *defines* the mapped level is disabled (host-model subtracts
  non-migratable/host-absent features, so a `Skylake` with AVX2 stripped must not read `v3`). The
  guard checks only the fixed level-boundary features (v2 `sse4.2`/`popcnt`; v3 `avx2`/`bmi2`/`avx`;
  v4 `avx512f`) — a targeted disable check, **not** the full feature expansion rejected below. An
  unmapped or non-x86 model, or a disabled defining feature, omits `baseline_level` but keeps
  `model`/`vendor`/`arch`. The level is a **nominal, name-derived upper bound**: a base-model-implied
  feature the host silently lacks is not enumerated and cannot be caught, so a present level is not
  a guaranteed floor — the agent-facing field text says so and directs a hard extension requirement
  to a running-guest check.
- **Typed reader** `host_cpu()` + `HOST_CPU_KEY` + `_KNOWN_KEYS` in
  `domain/catalog/resource_capabilities.py`, returning a `HostCpu` TypedDict or `None`,
  dropping malformed values (mirrors `guest_arches()`).
- **Agent-facing** via `resource_capability_data` (`mcp/tools/_resource_envelopes.py`) →
  `resources.list`/`resources.describe`. This is the one deliberate divergence from
  `guest_arches`: #980 exists precisely to make this visible at selection time.

### 2. Persisted `resolved_cpu` on the System (resolved at mint)

Mirror `accel`'s **actual** mechanism, which is mint-time resolution — not a provision-time
worker write-back. `accel` is resolved inside the mint transaction by `_resolve_new_system_accel`
(`services/systems/admission.py`) from the bound Resource's advertised `guest_arches()` and
written in the same INSERT that creates the System; the remote worker **discards** the accel it
is handed (`install.py` `del accel`) and never writes the systems row.

- **Migration 0070** adds a nullable `resolved_cpu jsonb` column to `systems` (no default;
  NULL = not recorded), mirroring `0067_system_accel.sql`.
- A `_resolve_new_system_cpu` helper alongside `_resolve_new_system_accel` reads the bound
  Resource's `capability_view.host_cpu()` and writes the resulting `HostCpu` (or NULL) into the
  mint INSERT. No live libvirt call, no dependency on libvirt expanding `host-model` in a
  running domain's XML, no new worker→DB path racing teardown/reap.
- The value is a **mint-time snapshot**: a later host re-registration or hardware change does not
  retroactively alter a provisioned System's `resolved_cpu` — it records the baseline the System
  was minted against.
- `system_envelope` surfaces `data["resolved_cpu"]` (sibling of `accel`) as a pure DB read, so
  `systems.get` stays libvirt-free with no new failure mode, and the persist path is fully
  unit-testable at admission (no `live_vm` gate).

## Consequences

- An agent can compare candidate remote hosts' CPU baselines (raw model + `x86-64-vN`) at
  `resources.describe` time, and read a specific System's pinned CPU baseline at `systems.get` —
  closing the ADR-0297 selection-surface gap.
- Both surfaces are additive JSON fields on existing reads; existing consumers are unaffected,
  and absence is the graceful default everywhere (unmapped model, malformed row, RPC fault,
  pre-migration System, local/fault host, un-refreshed remote host).
- `resources.describe` gains one guarded libvirt call at discovery (cold path). `systems.get`
  gains no libvirt call (the value is persisted at mint, read from the row).
- The advertised `host_cpu` is the fleet-level baseline for planning; the persisted `resolved_cpu`
  is that same advertised value frozen onto one System at mint. Both come from the host's last
  registration (`getDomainCapabilities` host-model), so the split lets an agent plan across the
  fleet and read one System cheaply without a live-domain read. Both are a **registration-time
  baseline, not a live-verified reading**: on a host whose CPU/microcode/libvirt changed after
  registration they can lag the host's current host-model resolution, so an agent needing
  certainty about a specific extension should confirm against the running guest.
- The curated model→level table needs maintenance as new CPU models appear; an unmapped model
  degrades to "raw model, no level" (absent = unknown, agent falls back to the raw model) rather
  than a wrong level. Documented, not silent.
- The remote capabilities row refreshes only on re-registration (existing behavior for
  `arch`/`vcpus`/`memory_mb`): existing hosts gain `host_cpu` only after re-registration, and a
  host CPU/libvirt change is stale until then. Documented in the rollout note; a per-field
  freshness timestamp is a possible follow-up, out of scope here.
- Residual risk: the `live_vm` proof (that a real remote host advertises a non-empty `host_cpu`)
  is operator-run (CI has no remote host); the mint-time persist path itself is unit-tested
  without a live gate.

## Considered & rejected

- **Live-read domain XML on every `systems.get`, or a provision-time worker→systems write-back.**
  Rejected: the live read bolts a TLS round-trip and new failure modes onto kdive's most-polled
  read and depends on libvirt expanding `host-model` in the running XML (not guaranteed — on a
  host that leaves it unexpanded the field is silently empty on the very fleet this serves). The
  worker write-back is an entirely new DB path the remote worker lacks today (it `del`s the accel
  it is handed and never touches the systems row), racing teardown/reap and provable only by an
  operator-run `live_vm` test. Mint-time resolution from the advertised `host_cpu` is the *actual*
  `accel` mechanism — resolved in the mint INSERT from bound-Resource capabilities — so it is
  cheap, staleness-free, and unit-testable at admission.
- **Static host `<cpu>` from `getCapabilities()` as the discovery source.** Rejected as primary:
  that is the *host* CPU, not the guest-under-host-model CPU (host-model omits non-migratable
  features). `getDomainCapabilities` host-model is the exact predictor. (`getCapabilities` arch
  parsing stays as-is for the `arch` key.)
- **Derive `baseline_level` by fully expanding the feature set.** Rejected: computing the level
  bottom-up from a fully expanded feature list needs the base-model feature closure and
  libvirt-version-specific feature naming. The curated model→level table plus the targeted
  disable-guard (adopted above) is offline-testable and honest: it maps the name, then omits the
  level only when an explicitly-disabled *level-defining* feature contradicts the name — reacting
  to data already in the parsed block without reconstructing the whole feature set. It does not
  catch a silently-absent base-model feature, which is why the field is documented as a nominal
  upper bound rather than a floor.
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
