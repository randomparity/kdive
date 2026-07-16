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
- **Enforcing a CPU pin against the bound rootfs image's ISA floor.** kdive's image catalog carries
  no declared per-image minimum ISA today, so admission validates only host-deliverability; a pin
  below the image floor (e.g. sub-`x86-64-v2` under EL9) non-boots and is disclosed in the field
  text as the agent's responsibility. Enforcing it is a tracked follow-up (requires an
  image-declared floor), not this issue.
- fault-inject CPU surfaces (a fake; unchanged).

## Design overview

Three surfaces, composed so the control allow-list **is** the visibility surface. Structured as
three phases (→ three commit groups in one PR).

### Phase A — local discovery: `host_cpu` (native) + `selectable_cpus` (per-arch)

`LocalLibvirtDiscovery.list_resources` advertises two additive `capabilities` jsonb keys (no
migration), agent-facing via `resource_capability_data` (`mcp/tools/_resource_envelopes.py`):

```
capabilities.host_cpu        = {model, vendor?, arch, baseline_level?}    # the host's NATIVE CPU
capabilities.selectable_cpus = {"<arch>": ["<model-name>", ...], ...}     # per-arch, sorted usable
```

**Arch scoping (a local host is multi-arch).** Local discovery advertises `guest_arches` with
possibly several entries and mixed default accel — the epic-#1139 box is an x86 host that advertises
`{x86_64: kvm, ppc64le: tcg}` (AC#12/#13 target the same host). "The host's CPU" and "the CPU a given
guest arch gets" are different concepts, so the two keys are scoped differently:

- **`host_cpu` is the host's single *native* CPU** — the arch that runs under KVM
  (`host-passthrough`/`host-model`). There is exactly one physical host CPU; a foreign/TCG-only arch
  (ppc64le emulated on x86) has **no host CPU** — its unpinned guest gets a QEMU machine-default,
  visible only post-provision via `resolved_cpu` (Phase C) or by pinning from `selectable_cpus`. The
  native arch is the discovered `guest_arches` entry whose **accel is `kvm`** (a per-host discovery
  value — the same arch is KVM on one host and TCG on another) intersected with the host's own arch
  (`parse_capabilities_arch`); `host_cpu.arch` records it. Shape unchanged from ADR-0368 (flat).
  Sourced by the native default mode:
  - **native x86 host-passthrough** → parse the host's `getCapabilities()` `<host><cpu><model>`/
    `<vendor>`. A new `parse_host_capabilities_cpu(caps_xml)` in `providers/shared/libvirt_xml.py`
    (defusedxml). ADR-0368's `getDomainCapabilities` host-model source would **under-report** a
    passthrough guest, so passthrough reads the host block. `baseline_level` runs the existing
    `cpu_baseline.baseline_level(model, disabled=())`.
  - **native ppc64le host-model** → reuse ADR-0368's `parse_host_cpu(getDomainCapabilities(...))`.
    Level omitted (non-x86).
- **`selectable_cpus` is keyed by guest arch** (mirroring `guest_arches`' per-arch shape), because
  the pinnable model set is arch- and virttype-sensitive and the knob (Phase B) must validate a
  pin against the profile's own arch. For **each advertised guest arch**, discovery reads that
  arch's `getDomainCapabilities` `<cpu><mode name='custom'>` `<model usable='yes'>` names via a new
  `parse_selectable_cpus(dom_caps_xml)` (defusedxml) → sorted, de-duplicated list; the arch key is
  omitted when the mode is unsupported or the parse faults. This is the exact set libvirt accepts in
  a `custom`-mode `<model>` **for that arch/virttype**, so it is the honest per-arch allow-list.

**`getDomainCapabilities` arguments are derived from the same sources the provisioner uses**, per
arch — not literals — because custom/host-model resolution is sensitive to `(arch, machine,
virttype)` (ADR-0368 lines 46–59 established this for remote):
- `arch=` the guest arch being enumerated.
- `machine=arch_traits(arch).machine` (the renderer's default — `q35`/`pseries`; a
  `domain_xml_params["machine"]` override is a per-System profile value not known at discovery, so
  discovery uses the arch default and the pin check tolerates that, see Phase B).
- `virttype=` the arch's default accel (`"kvm"` for the native/KVM arch, `"qemu"`/TCG for a foreign
  arch) — the same `<domain type>` the renderer emits.
- `emulator=` **omitted** for KVM (libvirt's default binary, matching the renderer); the discovered
  `qemu-system-<arch>` path for a TCG arch (matching `_append_emulator`).

- **All libvirt reads are guarded.** `arch`/`vcpus`/`memory_mb`/`transports` are computed first
  (unchanged); each CPU read + parse runs in a `try` catching any `libvirt.libvirtError`, logging at
  warning and omitting *that* field (a per-arch `selectable_cpus` failure omits only that arch key).
  A new advisory field never drops a host from discovery. (`_LibvirtConn` in local `discovery.py`
  gains `getDomainCapabilities`; the test fake implements it — mirrors ADR-0368's remote widening.)
- **Typed reads** in `domain/catalog/resource_capabilities.py`: reuse `host_cpu()` / `HOST_CPU_KEY`;
  add `selectable_cpus()` reader (returns `dict[str, list[str]]`, empty on a malformed/absent key) +
  `SELECTABLE_CPUS_KEY` + `_KNOWN_KEYS` entry (defensive, mirrors `guest_arches()`). Add
  `selectable_cpus` to `resource_capability_data` so it flows to `resources.describe`.

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
- **Admission validation (host-deliverability, per-arch).** At mint, when the profile pins
  `cpu.model`, validate membership in the bound Resource's `capability_view.selectable_cpus()`
  **entry for `profile.arch`** (`selectable_cpus().get(profile.arch)`); reject `CONFIGURATION_ERROR`
  with an actionable message (the pinned model + `profile.arch` + that arch's advertised set) if
  absent. This is a new check in the `_resolve_new_system_bindings` / profile-policy path
  (co-located with the accel mis-arch and fadump checks, which already fail-closed there). A profile
  with a pin but no bound Resource advertising `selectable_cpus[profile.arch]` (local host not
  re-discovered) → `CONFIGURATION_ERROR` (fail-closed: never render a pin the host cannot be shown to
  support for that arch).
- **ISA-floor is the agent's responsibility, made honest — NOT enforced here.** `selectable_cpus` is
  the host's full `usable` set, which on every x86 host includes sub-`x86-64-v2` models (`qemu64`,
  `kvm64`, `Nehalem`, …). Pinning one under an EL9/RHEL-family rootfs reintroduces the ADR-0294
  glibc PID-1 abort (a below-v2 CPU never reaches userspace) — through the new knob. This admission
  check validates only that the **host can deliver** the model, **not** that the **bound rootfs
  image can run on it**, because kdive's image catalog carries no declared per-image ISA floor to
  check against (verified: no ISA-floor field in `domain/catalog/images`). Rather than silently
  ship that footgun, the contract is made explicit at the surface the agent reads:
  - The pin `Field(description=...)` and the `resources.describe` `selectable_cpus` field text state
    that a model **below the rootfs image's ISA floor (x86-64-v2 for EL9/RHEL-family) produces a
    non-booting System**, that admission validates host-deliverability only, and that the
    `x86-64-vN` rungs (compared to the image's baseline) are the safe portable picks.
  - Enforcing a pin against an image-declared ISA floor is a **follow-up** (needs the image catalog
    to declare a floor); filed as a non-goal here so the gap is tracked, not hidden.
- **Agent-facing contract.** Update the wrapper docstring + `Field(description=...)` for the pin
  field and for `resources.describe`'s `selectable_cpus` (the FastMCP-serialized surface), per the
  ISA-floor contract above, pointing the agent at `selectable_cpus[arch]` and the `x86-64-vN`
  portable rungs for deterministic-reproducer pinning. Update the profile-schema / config docs the
  generated-doc guard covers.

### Phase C — live-verified `resolved_cpu` (local)

Post-provision, the **local** worker reads the running domain's resolved `<cpu>` and persists it to
the existing `systems.resolved_cpu` column (ADR-0368 / migration 0070 — **no new migration**).

- **The local mint snapshot is suppressed — Phase C is the *sole* writer of local `resolved_cpu`.**
  `_resolve_new_system_bindings` (`services/systems/admission.py`) is provider-agnostic: it returns
  `host_cpu_json(caps)` as the mint `resolved_cpu` for *both* providers, returning `None` for local
  only because local advertises no `host_cpu` **today**. The moment Phase A makes local advertise
  `host_cpu`, the unchanged mint path would stamp the **native** host CPU onto every local System —
  the wrong value for a pinned System, and an **arch-mismatched** value for a foreign-TCG guest
  (a ppc64le System stamped with the x86 host CPU). So Phase A/C **must** make the mint snapshot
  **remote-only**: gate `host_cpu_json(caps)` in `_resolve_new_system_bindings` on the bound
  Resource / profile provider being remote-libvirt, so a local System's `resolved_cpu` is `NULL` at
  mint and is filled **only** by this Phase-C live read. This keeps the "no unpinned behavior
  change" non-goal honest (an unpinned local System's `resolved_cpu` is NULL until the live read,
  never a wrong native snapshot) and makes Phase C the only local writer (finding-driven).
- **Read.** `virDomainGetXMLDesc(VIR_DOMAIN_XML_UPDATE_CPU)` on the running domain asks libvirt to
  expand host-passthrough / host-model / a `custom` pin to a concrete `<model>`. A new
  `parse_domain_resolved_cpu(domain_xml)` (defusedxml) extracts `{model, vendor?, arch,
  baseline_level?}` (level via the existing x86 table; omitted non-x86). The read happens once, at
  the local worker's post-provision boundary, after the domain is running.
- **Persist — one guarded, unconditional write.** Write the parsed `HostCpu` (concrete value) **or
  NULL** (on any read failure) to `systems.resolved_cpu` via a repository update **guarded by the
  System's lifecycle state**: the UPDATE affects the row only while the System is still in its
  post-provision provisioning/ready lifecycle (the existing `can_transition`/conditional-write
  pattern in `db/repositories.py`), so a System that **crashed, was NMI'd, or was reaped** in the
  read window (kdive is a crash tool — this window is real, cf. #984) takes a **no-op** write rather
  than resurrecting a value on a torn-down row. This resolves the ADR-0368 teardown-race objection
  for the local case by construction (the write cannot land on a terminal row), and it is a **new**
  local-worker→systems write scoped to the narrowest existing post-provision write point (pinned in
  the plan against `providers/local_libvirt/lifecycle/provisioning.py` + `db/repositories.py`).
  Because mint left local `resolved_cpu` NULL, the failure-path NULL write clobbers nothing.
- **Best-effort / never blocks.** Any `libvirt.libvirtError`, a parse fault, or an unexpanded
  `<cpu mode='host-model'/>` / TCG machine-default with no concrete `<model>` → write NULL, log the
  reason at info/warning, and **do not fail provisioning**. The provisioning result is independent
  of the CPU read (mirrors ADR-0368's "observability never fails the primary path").
- **Contract.** `resolved_cpu` becomes *live-verified for local, mint-snapshot for remote, NULL when
  unrecorded/unreadable* — stated in the `systems.get` wrapper docstring/field text and ADR-0369.
  Remote keeps ADR-0368's `_resolve_new_system_bindings` mint path unchanged (its snapshot is now
  explicitly remote-only). `systems.get` stays a pure DB read (`system_envelope` reads the row; no
  live call on the polled path).

## Acceptance criteria

1. `resources.describe` on a local-libvirt x86 host returns `data.host_cpu` with `model`/`vendor`/
   `arch`/`baseline_level` sourced from the host `<host><cpu>` block (a `SapphireRapids` host →
   `x86-64-v4`). Unit test with an injected fake connection.
2. `resources.describe` on a local-libvirt host returns `data.selectable_cpus` as a **per-arch map**
   (`{arch: [sorted usable model names], ...}`) with an entry for each advertised guest arch, sourced
   from that arch's domain-capabilities custom mode with the arch-derived `machine`/`virttype`/
   `emulator` args. A multi-arch fake (x86 KVM + ppc64le TCG) asserts both entries and their args.
   Unit test.
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
7. Admission **accepts** a pin whose `cpu.model ∈ selectable_cpus[profile.arch]` and **rejects**
   (`CONFIGURATION_ERROR`, message names the model + `profile.arch` + that arch's advertised set): a
   pin absent from the arch's set, a pin present only in a *different* arch's set (wrong-arch), and a
   pin when the bound Resource advertises no `selectable_cpus[profile.arch]`. Service/unit tests for
   all four.
8. A pinned System's provisioning end-to-end (renderer + admission) uses the pinned model; unit/
   service level (no `live_vm` gate needed to prove admission + render).
8a. The pin `Field(description=...)` and the `selectable_cpus` field text state the ISA-floor
    contract (a sub-`x86-64-v2` pin non-boots EL9; admission validates host-deliverability only).
    A doc/schema test asserts the caveat text is present in the serialized tool schema, so the
    footgun is disclosed at call time rather than silent.
9. Phase C: given a running domain whose `VIR_DOMAIN_XML_UPDATE_CPU` XML carries a concrete
   `<model>`, `parse_domain_resolved_cpu` returns the `HostCpu`, and the post-provision path writes
   it to `systems.resolved_cpu`; given an unexpanded/`<model>`-less XML or a raising read, it writes
   NULL and does not fail provisioning. Unit tests with fake domain XML + a raising fake.
10. `systems.get` on a System with a live-verified `resolved_cpu` returns `data.resolved_cpu`; on one
    without, the field is absent/`null`. `systems.get` performs **no** libvirt call (pure row read).
11. Remote `resolved_cpu` (ADR-0368 mint-snapshot) and remote `host_cpu` are unchanged — a
    regression test asserts the remote mint path still snapshots.
11a. **Local mint does not snapshot `resolved_cpu`.** A local System minted against a Resource that
    advertises `host_cpu` (Phase A) has `resolved_cpu` **NULL at mint** — asserted for an unpinned
    x86 System (no native snapshot), a **pinned** x86 System (not the native host CPU), and a
    ppc64le System on the x86 host (no arch-mismatched x86 snapshot). Proves the mint path is
    remote-only and Phase C is the sole local writer. Service/unit level.
11b. **Guarded Phase-C write / domain gone.** When the System row is no longer in its post-provision
    provisioning/ready lifecycle (crashed/reaped) at the write, the guarded UPDATE is a **no-op** —
    a test drives the System to a terminal state before the write and asserts `resolved_cpu` is
    unchanged (NULL, never a stale/wrong value). The write is otherwise unconditional (parsed value
    or NULL).
12. `live_vm` (x86, native KVM): provision with `cpu.model = x86-64-v2` (a `selectable_cpus` rung),
    assert the running domain resolves to it and `systems.get.resolved_cpu` reflects it. Skips
    cleanly without the KVM host.
13. `live_vm_tcg` (ppc64le, TCG on the epic-#1139 box): provision a ppc64le System; assert
    `resources.describe` advertised `selectable_cpus["ppc64le"]`. For the Phase-C TCG resolved read,
    the proof records a **definite outcome** with the box's QEMU/libvirt version: **either** a
    concrete `<model>` (then `resolved_cpu` equals the running domain's actual resolved CPU — a
    falsifiable match, closing the invisible-default gap on that platform) **or** a recorded
    NULL-with-reason (the expand does not resolve the machine-default at that version — the
    documented best-effort limitation). The test/proof-note must state which occurred; an untested
    "either passes" is not acceptable. Skips cleanly without the foreign emulator.
14. `just ci` green (lint, type, lint-shell, lint-workflows, check-mermaid, test), including
    regenerated generated docs.

## Failure modes & edges

- **Malformed/absent host-`<cpu>` or domain-capabilities XML** → parser returns `None`/empty; the
  field is omitted; discovery succeeds.
- **`getDomainCapabilities` / host-`<cpu>` read raises** (old libvirt, transient fault) → caught in
  `list_resources`, logged, field omitted, resource discovers intact.
- **Host advertises no custom mode / empty usable set for an arch** → that arch's `selectable_cpus`
  entry omitted; a pin for that arch fails admission `CONFIGURATION_ERROR` (fail-closed).
- **Pinned model not in `selectable_cpus[profile.arch]`** (absent, or present only for another arch)
  → admission `CONFIGURATION_ERROR` (never render a custom `<cpu>` the host can't deliver for that
  arch).
- **Pinned model is host-deliverable but below the rootfs image's ISA floor** (e.g. `qemu64` on an
  EL9 System) → admission **accepts** it (host-deliverability holds); the System boots to a glibc
  PID-1 abort (ADR-0294). This is disclosed in the pin/`selectable_cpus` field text as the agent's
  responsibility; kdive has no per-image ISA floor to enforce (follow-up non-goal). Not a silent
  admission bug — a documented contract.
- **`domain_xml_params["machine"]` override differs from the arch default** → discovery advertised
  `selectable_cpus` for the arch-default machine; the pin check tolerates this (the usable model set
  is machine-stable in practice, and a mismatch surfaces as a libvirt define error, not a silent
  wrong boot). Documented; a per-machine allow-list is out of scope.
- **Unpinned profile** → byte-identical legacy render; `resolved_cpu` NULL at mint (local mint does
  not snapshot), filled by the Phase C live read if it succeeds. No native-CPU snapshot.
- **Phase C read raises / returns no concrete `<model>` / TCG machine-default unexpanded** →
  Phase C writes `resolved_cpu` NULL, logged, provisioning succeeds (best-effort). Since mint left it
  NULL, nothing is clobbered.
- **System crashes / is NMI'd / reaped before the Phase C write** → the lifecycle-guarded UPDATE is
  a no-op on the terminal row; `resolved_cpu` stays NULL (never a stale/wrong native snapshot). kdive
  is a crash tool, so this window is a real path, not theoretical.
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
