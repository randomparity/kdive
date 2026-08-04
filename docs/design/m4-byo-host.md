# M4 — BYO host: adopt-only bare metal and PowerVM LPARs (Integration Contract)

## Purpose

M4 adds the **first provider that does not create a machine**. An operator declares an
already-running host — an x86 server with a BMC, or a ppc64le PowerVM LPAR reached through an
HMC — and `byo_host` adopts it, drives the full spine (allocate → adopt → build → install →
boot → attach → force-crash → capture vmcore → debug → release), and returns it to the
operator's baseline kernel.

Two things make it a different shape from every provider before it, and everything else in
this document follows from them:

1. **There is no hypervisor above the target.** Power, console, and crash recovery reach the
   host through its service processor, out-of-band, because the in-band SSH path is the one a
   kernel debugger destroys.
2. **Release restores rather than destroys.** A failed teardown leaves a *working-looking*
   machine in an unknown kernel state, which placement will happily hand to the next tenant.
   Every provider before this one could only leave an orphan.

**Roadmap position.** `docs/design/top-level-design.md:581` scopes M4 as bare metal (PXE / SoL /
IPMI / Redfish) and `:583` scopes M5 as "LPAR activation + HMC; second architecture". The
adopt-only decision removes two declared pieces, one from each: **PXE** from M4, and **LPAR
activation** from M5 (see Non-goals — KDIVE never creates, deletes, or resizes an LPAR). What
the single-provider decision ([ADR-0538](../adr/0538-byo-host-provider-package.md)) merges into
this milestone is therefore M5's **HMC control plane and second architecture**, not its LPAR
lifecycle half. That half is not rescheduled — it is out of scope for the adopt-only approach
and would need its own decision to return.

**Relationship to epic #1760.** #1760 selected MAAS as an external provisioning control plane;
this milestone selects none. It was **closed as `NOT_PLANNED` on 2026-08-04**, with all 27 of
its sub-issues closed, so adopt-only is the surviving direction and **no cross-epic hold
applies** to any entry here. Roughly ten of #1760's sub-issues described host-facing planes
that overlap entries 11–16 and 19–20; they are closed, not adopted, and this milestone owns
that surface outright. The MAAS approach stands as a recorded alternative in #1760's history —
returning to it would be a new decision, not the resumption of an outstanding one. The
motivating findings survive the closure and are why adopt-only was chosen: `packer-maas`
publishes no ppc64el image template (#1793), Beaker was a closer fit than MAAS (#1792), and
neither control plane can issue an NMI, which a provider whose central operation is
`force_crash` cannot accept (#1808).

- **Decisions:** [ADR-0538](../adr/0538-byo-host-provider-package.md) (one `byo_host` package
  for both architectures + `ResourceKind.BYO_HOST` + bind-only registration),
  [ADR-0539](../adr/0539-out-of-band-control-port.md) (the typed OOB driver port, its Redfish
  / IPMI / HMC implementations, and the leased console channel),
  [ADR-0540](../adr/0540-adopt-only-provisioning.md) (adopt-only `provision`, the
  `adopted-host` boot method, the three verification moments, the declared-and-verified
  baseline kernel, and the `arch_traits()` decision),
  [ADR-0541](../adr/0541-baseline-restore-or-cordon-teardown.md) (restore-verify-or-cordon
  teardown and the reconciler drift arm),
  [ADR-0542](../adr/0542-kgdb-over-leased-serial-channel.md) (the `kgdb` debug transport over
  the leased serial channel). All build on
  [ADR-0063](../adr/0063-typed-provider-runtime.md) (the typed port seam),
  [ADR-0071](../adr/0071-per-kind-provider-runtime-registry.md) (the per-kind runtime
  registry), [ADR-0076](../adr/0076-remote-libvirt-provider-package.md) (the provider
  independence precedent and the portability hypothesis),
  [ADR-0078](../adr/0078-object-store-in-target-install-seam.md) /
  [ADR-0082](../adr/0082-remote-install-in-guest-kernel.md) (the in-target install seam this
  reuses), [ADR-0012](../adr/0012-secret-backend.md) /
  [ADR-0073](../adr/0073-forced-secret-resolution-redaction.md) (secret resolution and forced
  redaction), and [ADR-0021](../adr/0021-reconciler-loop-drift-repair.md) (the drift-repair
  loop teardown recovery hangs off).
- **Parent:** [`top-level-design.md`](top-level-design.md) §Roadmap (M4, M5).

## Resolved open questions

Epic #1814 opened seven. **Five are settled here** (1, 2, 3, 4, 7) and **two stay open** (5, 6)
with the entries that own them. Questions 1–4 are the architecture questions this issue was cut
to resolve; question 7 arrived in the epic already proposed with a stated answer, and is
confirmed rather than decided here — so the scope claim of "open questions 1–4" still holds.

| # | Question | Resolution | Record |
|---|---|---|---|
| 1 | How is `baseline_kernel` identified? | Operator-declared, verified present at `doctor` **and** at adopt. A captured value records whatever was booted — after a failed teardown, a KDIVE debug kernel — so the host would restore to it indefinitely with no signal. | ADR-0540 |
| 2 | What `boot_method` does an adopted host declare? | A third value, `adopted-host`, and the pairing validator generalizes from a biconditional to an explicit provider-to-boot-method map. | ADR-0540 |
| 3 | Console multiplexing between KGDB and log capture | KGDB takes the console lease exclusively. Collection suspends and a live read reports `pumped=False` (the persisted artifact carries no gap marker); `supports_crash_watch` stays `True` and the tools refuse with `transport_conflict` naming the holder. | ADR-0539, ADR-0542 |
| 4 | Does `arch_traits()` split? | No. Four of its six fields are libvirt domain-render facts; of the two that generalize, `console_device` is a per-host firmware setting on metal. One arch-keyed field does not justify a second table. The module docstring is amended to mark field scope. | ADR-0540 |
| 5 | fadump detection on real firmware | Open. Owned by entry 13 (#1829), which replaces the QEMU version floor at `src/kdive/providers/shared/fadump_detect.py:18`. Not architecture — a probe choice made against real hardware. |  |
| 6 | Cost-class coefficient value | Open. An operator pricing input to entry 2 (#1817). The migration must seed *a* row; which number is not a design decision. |  |
| 7 | Concurrency ceiling | Settled as `concurrent_allocation_cap = 1`: a force-crash takes the whole machine, so no partitioning case survives the provider's central operation. | ADR-0540 |

## What M4 adds

- **One `byo_host` provider package serving both architectures.**
  `src/kdive/providers/byo_host/` with its own discovery, adopt, install, boot, console,
  control, retrieve, debug, and teardown modules over the same typed `ProviderRuntime` ports.
  The x86/POWER split is visible in exactly two places: behind the OOB driver port, and in one
  arch-keyed bootloader module. A new `ResourceKind.BYO_HOST = "byo-host"` and one migration
  (CHECK widen, cost-class seed, the console-lease table, and the adopt-facts column) register
  the fourth kind behind the per-kind `ProviderResolver`.
- **An out-of-band control plane as a first-class provider capability.** A typed driver port
  under `providers/byo_host/oob/` with three implementations — Redfish (x86, primary), IPMI
  (x86, legacy labs), HMC (PowerVM LPAR). A driver is constructed from one host's declaration
  and thereafter answers for that host alone, which is what lets the HMC's
  managed-system-plus-partition addressing and a BMC's one-endpoint-one-host addressing satisfy
  the same protocol. Credentials are username/password refs resolved through the SecretRegistry
  and registered for redaction before first use; unlike ADR-0077's x509 pair, nothing is
  materialized to disk.
- **A leased console channel.** One serial line, four consumers — log collection, SysRq
  injection, crash watch, KGDB. The channel is leased with a named holder, and a second
  acquirer is refused with `transport_conflict` naming the holder and the release action. Log
  collection is itself an ordinary lease holder rather than a privileged background reader, so
  a **live** console read reports the suspension through `ConsoleWindowRead.pumped`
  ([ADR-0429](../adr/0429-remote-console-read-seam.md)). The persisted per-Run artifact carries
  no such marker, so a lease-shaped hole in it reads as a silent kernel — an accepted
  consequence, recorded in ADR-0539 rather than papered over.
- **Adopt-only provisioning.** `provision` validates preconditions against the live machine,
  records what it established on the System row, and returns a stable handle. No OS install, no
  re-image, no boot-order change, no firmware write. `boot_method: adopted-host` is the profile
  contract that says so, and the same precondition module runs at three moments: deploy-time
  (schema only, ADR-0121's no-I/O contract preserved), operator pre-flight (`doctor`, live),
  and adopt (live, re-checked because the world drifts between the two).
- **The in-target install seam, reused unchanged.** The kernel goes into the running OS's
  bootloader over SSH — `grubby` on x86 EFI, grub2-PReP or petitboot on PowerVM — pulled
  in-target via presigned GET (ADR-0078, ADR-0082). The worker never pushes a kernel. Readiness
  is a boot-id change, with OOB console evidence on the failure path. This is the mechanism M2
  built and predicted would survive to bare metal; M4 is where that prediction is tested.
- **KGDB over the leased serial channel.** A third `DebugTransportKind`, `kgdb`, driven through
  the existing gdb-MI engine with arch selection from
  `providers/shared/debug_common/gdbmi/policy/arch.py:59`. The connector bridges the leased
  console to a worker-local loopback port and returns an ordinary `gdbstub://host:port` handle,
  so `TransportHandleKind` is unchanged and the engine, the handle codec, and every consumer of
  realization are untouched. `kgdboc` is composed into the target cmdline at install time.
- **Cordon-restore-verify-uncordon teardown.** Cordon the Resource, re-point the bootloader at
  the declared `baseline_kernel`, power-cycle out-of-band, re-run the adopt preconditions
  against the rebooted host, and clear the cordon only on success. The cordon comes **first**
  because the allocation's capacity slot frees synchronously at `allocations.release`
  (`services/allocation/release.py:229`, `admission/core.py:586`), minutes before the teardown
  job runs — so without it the next tenant can be granted the machine mid-restore. Any failure
  leaves the cordon in place with a reason persisted on the Resource, surfaced as
  `restore_incomplete`; the clear requires that this teardown set the cordon **and** re-reads the
  reason at the moment it clears, so it cannot lift an operator's maintenance cordon taken either
  before or during the window. The reconciler gains a BYO drift arm keyed on that cordon reason —
  not on a System state, since `torn_down` is terminal and there is no teardown-in-progress
  member — which never retries the restore.

## Non-goals (scoped out)

- **No OS install or re-image.** No PXE, kickstart, NIM, or Beaker integration. The operator
  brings a host that already boots a kdive-ready OS.
- **No LPAR lifecycle management.** KDIVE does not create, delete, or DLPAR-resize an LPAR. It
  adopts one and uses the HMC for power and vterm only.
- **No firmware management** — no BIOS/UEFI settings, firmware updates, or boot-order changes
  as a normal operation.
- **No snapshot or restore** (`supports_snapshots` stays `False`) and **no host-side traffic
  capture** (`supports_traffic_capture` stays `False`) — both need a hypervisor vantage that
  does not exist here.
- **No `host_dump` capture method**, for the same reason. `byo-host` advertises `kdump` on both
  architectures and `fadump` on ppc64le, and nothing else.
- **No PowerNV / OpenPOWER bare metal.** The ppc64le scope is PowerVM LPARs.
- **Not a build host.** A BYO host is a target; ADR-0099 build-host targets are unaffected.
- **No `libvirt_common` refactor** and no re-litigation of ADR-0076's bounded-duplication
  decision. BYO touches no libvirt code.
- **No new dispatch architecture.** This rides the ADR-0063 typed seam, as AGENTS.md directs.
- **No new agent-facing tool** beyond the `kgdb` transport value on `debug.start_session`.
- **No widening of the OOB port beyond power and console.** Sensors, boot-device override,
  virtual media, firmware inventory, and HMC dump management are surveyed by entry 7 (#1816)
  and added only against a named caller.

## Postgres schema (M4 delta)

- **One migration**, `0112_resources_kind_byo_host.sql` (next free after
  `src/kdive/db/schema/0111_restrict_pinned_job_deletion.sql`), owned by entry 2. Forward-only.
  It carries **four** things:
  1. the `resources_kind_check` widen admitting `'byo-host'`;
  2. a seeded cost-class coefficient row;
  3. `byo_console_leases` — a Resource-keyed table for the ADR-0539 console lease (holder,
     expiry, acquired-at), with a unique constraint on the resource so acquisition is an insert
     that either wins or conflicts; and
  4. `systems.byo_adopt_facts jsonb` — where adopt records what it established (ADR-0540), and
     what ADR-0541's teardown compares the returned host against.
- **Items 3 and 4 land ahead of their writers**, and that is the deliberate trade. The gap
  differs per item: the lease table is written by entry 4 in the very next wave, and the
  adopt-facts column by entry 8 two waves later. ADR-0517 makes migration numbers strictly ascending across merges, so a second
  schema-touching entry — entry 4 for the lease, entry 8 for the facts — would make every
  rebase between them a renumber, which is precisely the cost the "one migration, claimed up
  front" rule exists to avoid. Unused schema for two waves is cheaper than two serialization
  points in the merge order.
- The lease needs a table rather than a jsonb key because it is contended. Every persisted
  lease in this repository got one for the same reason — `build_host_leases` (0027),
  `object_write_leases` (0084), `rootfs_fetch_leases` (0087) — and a `capabilities` key has no
  unique constraint, so two acquirers would race on a read-modify-write. It is keyed on the
  **Resource**, not the System: the serial channel belongs to the physical host, and log
  collection is an ordinary holder (ADR-0539), so a System-keyed or `debug_sessions`-keyed
  lease could not represent every holder.
- The cost-class seed is not optional. Admission resolves the coefficient fail-closed, so a
  cost class with no row denies every allocation with a message about cost classes rather than
  about the host. `src/kdive/db/schema/0032_remote_cost_class_coefficient.sql` is the record of
  that defect shipping once already.
- **The `resources.capabilities` jsonb carries the host facts**, as remote-libvirt's connection
  config does. Written by the reconcile arm from the declaration
  (`src/kdive/inventory/reconcile/resources.py:268`): SSH target, OOB endpoint and driver kind,
  credential refs, `baseline_kernel`, console device, `concurrent_allocation_cap`, the
  `guest_arches` sentinel entry (ADR-0538), and **`pseries_fadump`** on a ppc64le host. The last
  is not optional and is easy to miss: fadump admission is per-Resource and fail-closed
  (`src/kdive/services/systems/admission.py:277`, `services/systems/validation.py:75`), so a
  host that does not publish the key is denied fadump at `systems.create` — and entry 20's
  POWER proof includes fadump. `CAPTURE_COVERAGE` is provider-level and cannot carry the
  per-arch split by itself. Entry 2 owns the declaration field; entry 13 owns where the value
  comes from (open question 5).
- The teardown cordon reason is a further namespaced key there; reconcile merges rather than
  replaces (`:292`, `:493`), so a key written by another writer survives a pass.
- The migration merges **only** alongside a registered, buildable runtime.
  `tests/db/test_resource_kind_parity.py:30` asserts the CHECK set by exact equality and `:44`
  asserts every admitted kind resolves to a buildable runtime, so a CHECK widen without a
  registered runtime fails CI. Fail-closed stub ports satisfy it, which is why entry 2 is cut
  that way.

## Provider model (M4 delta)

`providers/assembly/composition.py` stays the only production assembly point and gains a fourth
map entry behind the `ProviderResolver`:

```
ResourceKind ──▶ ProviderRuntime
  local-libvirt ─▶ build_local_runtime()         # default; unchanged
  fault-inject  ─▶ build_faultinject_runtime()   # opt-in (M1.5)
  remote-libvirt▶ build_remote_runtime(...)      # opt-in: operator supplies host URI + cert ref
  byo-host      ─▶ build_byo_host_runtime(...)   # opt-in: operator declares [[byo_host]] entries
```

Registration is **bind-only** (`creates=False`,
`src/kdive/providers/core/discovery_registration.py:39`): `reconcile_resources` is the sole
creator of BYO Resource rows. The runtime composes only when an operator declares at least one
`[[byo_host]]` entry, so a deployment without one has no bookable BYO resource.

Three runtime fields are set explicitly because their defaults are wrong here:

| field | default | BYO value | why |
|---|---|---|---|
| `platform_root_cmdline` | `"root=/dev/vda"` (`providers/core/runtime.py:152`) | `None` | The adopted host's own bootloader owns its root device; a wrong `root=` boots to an unreachable root (ADR-0183). |
| `binding` | `None` (`runtime.py:162`) → `for_resource()` returns identity (`:174-183`) | `ResourceBindingCapabilities(...)` | BYO serves many hosts; without a rebind hook every op silently drives one arbitrary machine (ADR-0187). |
| `support.supports_crash_watch` | `False` | `True` | The capability is real whenever no debug session holds the console lease. The dynamic conflict is a refusal, not a static flag (ADR-0542). |

The declared architecture reaches `Resource.capabilities` under the `guest_arches` key, so
`resolve_accel()` (`src/kdive/services/systems/validation.py:50`) and `resource_supports_arch()`
(`src/kdive/services/allocation/admission/affinity.py:45`) stop fail-opening and admission
rejects an arch-mismatched profile at `systems.create` rather than at install. The exact record
BYO writes, and why it needs no core change, is fixed by
[ADR-0538](../adr/0538-byo-host-provider-package.md) — the shape is libvirt's, so BYO's entry
carries explicit sentinels rather than a fabricated accelerator.

## The out-of-band plane (the load-bearing mechanism)

The seam every host-facing entry consumes, and the one whose shape entries 5, 6, 11, 12, 14,
and 16 are written against:

1. **Construct per host.** A driver is built from one `[byo_host.oob]` block. `endpoint` plus
   the credential refs identify the service processor; `managed_system` and `lpar_name` are the
   HMC's additional coordinates, resolved at construction so nothing downstream carries them.
2. **Resolve, register, then use.** The worker resolves each credential ref at the op boundary
   and registers the resolved value with the redaction registry **before** the first call that
   could echo it, releasing the scope only after redact-and-persist (ADR-0073). Credentials go
   by argument or environment, never on a command line.
3. **Two capabilities, both mandatory.** Power actions and a serial console channel. A service
   processor that cannot do both cannot serve the plane's purpose.
4. **Lease the console.** Acquire, hold for a bounded scope, release. A second acquirer is
   refused with `transport_conflict` naming the holder and the release action.
5. **Fail closed.** An unreachable endpoint, a rejected credential, or a console that cannot be
   established fails with a specific existing `ErrorCategory`. There is **no** silent fallback
   to in-band SSH — a fallback would make an OOB failure indistinguishable from a healthy host
   until the moment it mattered.

Two contract points are load-bearing and tested: **(a)** no OOB credential reaches a persisted
transcript, a response snippet, or an argv unmasked; **(b)** every console consumer goes
through the lease, so a suspended collector reports `pumped=False` to a live reader rather than
empty bytes, and two consumers never interleave on one line.

## MCP tool surface (M4 delta)

One value, no new tools. `debug.start_session` accepts a third `transport` value, `kgdb`. Its
wrapper docstring and `Field` text state what it means operationally — it stops the **machine**,
not a guest, so in-band probes will not answer and kdump cannot run while it is attached — and
name the console-lease conflict, its holder, and the release action. A BYO resource is otherwise
discovered and registered service-side and driven through the existing surface.

The OOB power and reboot windows and the console lease are **limits handed to an agent**, so
each states all five parts: unit, reference clock, scope, consequence of violation, and
recovery action. Firmware POST on real metal is a materially different wait from a VM boot, and
an agent given a bare relative number treats it as a wall to route around.

## Auth / RBAC delta

**None.** A BYO resource registers under the service identity at discovery, the same path every
other provider uses. No new role, claim, or gate. The destructive-op gate
(`src/kdive/security/authz/gate.py`) applies unchanged: `force_crash` requires the allocation
project's RBAC role and an explicit profile opt-in, and power and teardown use their existing
lifecycle paths (ADR-0130).

## Error taxonomy (M4 delta)

**None.** Every BYO failure maps to an existing value; no strings are invented.

| condition | category |
|---|---|
| SSH or OOB endpoint unreachable; console cannot be established | `transport_failure` |
| Console lease held by another consumer | `transport_conflict` |
| OOB credential rejected | `authorization_denied` |
| Service processor refuses a power action | `control_failure` |
| Malformed declaration; unknown arch; unknown transport kind | `configuration_error` |
| Adopt precondition fails (missing baseline kernel, arch mismatch, bootloader flavor) | `provisioning_failure` |
| In-target kernel install fails | `install_failure` |
| Host never reaches readiness after boot | `boot_timeout` |
| `kgdboc` absent from the booted cmdline | `missing_dependency` |
| Teardown restore incomplete; host state indeterminate | `restore_incomplete` |

`restore_incomplete` is reused rather than extended.
[ADR-0513](../adr/0513-restore-incomplete-failure-category.md) defined it for a snapshot revert
whose worker died mid-flight; it means a restore that did not
complete over indeterminate state, needing an operator rather than a retry, which is exactly
the teardown case.

## Portability gate: what is actually enforced, and the allowlist

The epic's R9 listed seven touch-points. **Four of them are outside the gate's reach**, and
recording that here is what keeps entry 17 (#1820) from adding allowlist entries for files the
gate never inspects. `CORE_PREFIXES` (`scripts/m2_portability_gate.py:44-53`) is exactly
`domain/`, `db/`, `jobs/`, `reconciler/`, `services/`, `store/`, `security/`, and `mcp/`.

**Not gated** — no allowlist entry is owed, because these paths are not core prefixes:
`src/kdive/profiles/provisioning.py` (the `adopted-host` value, the pairing map, and
`ProviderSection.byo_host_section`), `src/kdive/inventory/model.py` and
`src/kdive/inventory/reconcile/` (the declaration model and reconcile arm),
`src/kdive/profiles/provider_sections.py` (the `PROVIDER_SECTIONS` row),
`src/kdive/providers/` in its entirety (the package, the OOB port, and the
`DebugTransportKind` literal at `providers/ports/lifecycle.py:27-28`).

**Gated** — the allowlist entries this milestone owes:

| path | why | owning entry | already allowlisted? |
|---|---|---|---|
| `src/kdive/domain/catalog/resources.py` | `ResourceKind.BYO_HOST` | 2 | yes (ADR-0076 touch-point) |
| `src/kdive/db/schema/0112_resources_kind_byo_host.sql` | the one migration | 2 | **no — new entry** |
| `src/kdive/domain/platform/arch_traits.py` | the docstring amendment marking field scope (ADR-0540) | 8 | **no — new entry** |
| `src/kdive/jobs/handlers/systems.py` | cordon-on-teardown-failure with a reason (ADR-0541) — the provider port cannot do it: `Provisioner.teardown(domain_name)` (`src/kdive/providers/ports/lifecycle.py:130-138`) takes a domain name, gets no connection, and documents only `INFRASTRUCTURE_FAILURE` / `TRANSPORT_FAILURE`. The caller is `teardown_handler` (`src/kdive/jobs/handlers/systems.py:751`, provider call at `:783`). | 16 | **no — new entry** |
| `src/kdive/mcp/tools/debug/sessions/lifecycle.py` and `.../sessions/registrar.py` | the `kgdb` transport arm: `lifecycle.py` carries the per-transport branching (`_GDBSTUB` / `_DRGN_LIVE` at `:80-81`, the `DEBUG_TRANSPORT_KINDS` check at `:305`, the arms at `:409` and `:427`); `registrar.py` carries the agent-facing `Field` text | 14 | **no — new entry** (see below) |
| `src/kdive/mcp/tools/ops/resources/host_ops.py` | surfacing the cordon reason and clearing it on uncordon (ADR-0541) — `_apply_cordon` is at `:141`, `resources.set_scheduling` at `:47` | 16 | **no — new entry** (see below) |
| `src/kdive/jobs/handlers/control/diagnostic_sysrq.py` and `.../control/watch_for_crash.py` | **holding a console-lease scope** and propagating its `transport_conflict` (ADR-0542) — two changes, not one. Each handler brackets its whole multi-read console interaction in a lease scope, because a per-read lease would let KGDB take the channel between SysRq's mark read (`diagnostic_sysrq.py:111`) and its injection. Each also surfaces the conflict instead of core's current handling: `diagnostic_sysrq.py:164-169` raises `configuration_error` / `console_not_pumped` naming no holder, and `watch_for_crash.py:191-193` discards `pumped` deliberately. These two are `read_window`'s only consumers in the tree. | 12, 14 | **no — new entry** |
| `src/kdive/mcp/tools/catalog/resources.py` (or `.../tools/_resource_envelopes.py`) | surfacing the cordon reason on `resources.describe` (ADR-0541). `describe_resource` is at `catalog/resources.py:185` and its envelope body comes from `resource_capability_data` (`_resource_envelopes.py:25-53`), which projects a **fixed** key set — kind, arch, the three int ceilings, transports, host_cpu, selectable_cpus — so a new namespaced key is silently dropped. The allowlist's `src/kdive/domain/catalog/resources.py` is a different file. | 16 | **no — new entry** |
| `src/kdive/reconciler/loop.py` | the BYO mid-teardown drift arm and the stranded-console-lease reclaim | 16 | yes (entered for ADR-0086; BYO reuses it) |

**Nine new entries across five issues, not three** — and two of them look like reuse until you
check. `ALLOWED_FILES` is matched by exact path (`violations()`,
`scripts/m2_portability_gate.py:229-231`); there is no prefix or directory matching. The
entries ADR-0085 and ADR-0541's surface would have reused —
`src/kdive/mcp/tools/debug/sessions.py`, `.../debug/introspect.py`, and
`.../ops/resources.py` — **name files that no longer exist**. Each became a package, so the
allowlist strings match nothing while every file inside those packages sits under
`src/kdive/mcp/`, a core prefix.

This is not confined to the three BYO would have leaned on. **14 of the 54 `ALLOWED_FILES`
entries point at paths absent from the tree** — roughly a quarter of the allowlist protects
nothing, silently, and a gate that reports no violation over a stale entry is the same failure
class as the drift guard above. Entry 17 owns re-pointing them and adding the cheap guard that
stops it recurring: fail the gate on an `ALLOWED_FILES` member that does not exist on disk.
That is pre-existing rot rather than BYO's — it affects local-libvirt and remote-libvirt's own
measurement too — so it is tracked separately as #1835.

The three `jobs/` rows are the ones a reader is most likely to miss on their own merits:
`src/kdive/jobs/` is a core prefix whose allowlisted members are only `worker.py`,
`worker_telemetry.py`, `queue.py`, `payloads.py`, and `handlers/image_build.py`. Nothing in the
tree cordons on teardown failure today. There are six existing cordon write sites —
`inventory/reconcile/prune.py:52` and `:85` (not a core prefix),
`reconciler/cleanup/runtime_resources.py:148` (gated, unallowlisted; the allowlisted reconciler
modules that still exist are `loop.py` and `loop_telemetry.py`),
`mcp/tools/ops/resources/deregister.py:283` and `:347` (also gated and unallowlisted), and
`host_ops.py:145` — and **every one writes the boolean with no reason**, which is why ADR-0541's
step 0 has to read the flag before setting it rather than trusting a reason key to tell an
operator's cordon from its own.

Three further facts about the gate, each verified against the tree rather than inherited from
M2's design doc. Two of them say the enforcement M2's document describes is not there:

- **The gate does not run in CI.** `just m2-gate` appears in no workflow and is not a member of
  the `ci` recipe (`justfile:546`); it is reachable only by hand, alongside `just m2-report`.
  M2's design doc describes a per-PR gate, and that wiring is not present. Entry 17 owns
  deciding whether to wire it or to state plainly that the measurement is milestone-end only —
  a gate nobody runs measures nothing, and claiming otherwise in this document would be the
  same defect one layer up.
- **Nothing detects a missing `CAPTURE_COVERAGE` row.** The drift guard
  (`tests/scripts/test_m2_portability_gate.py:33-52`) imports the real builders and asserts two
  **hardcoded** keys — `CAPTURE_COVERAGE["remote-libvirt"]` and `["local-libvirt"]` — against
  `build_remote_runtime` and `build_local_runtime`. Nothing enumerates the resolver's registered
  kinds, in either direction. So entry 2 can register `byo-host` with no coverage row and
  `just test` stays green; the omission surfaces only as `just m2-report` quietly leaving out
  the provider this milestone exists to add. **Entry 17 owns closing that**: an assertion that
  iterates the resolver's registered kinds and requires a `CAPTURE_COVERAGE` entry for each,
  which is what makes epic exit criterion 8 checkable and what turns the entry-17-after-entry-2
  ordering from a convention into an enforced one. Until it lands, the ordering is a convention
  with no automated signal, and `"byo-host": frozenset({"kdump", "fadump"})` is a row a human
  has to remember.
- **`BASELINE_TAG = "pre-M2"`** (`:24`) would measure BYO's diff against a tag two milestones
  old, folding every intervening core change into this milestone's total. Entry 17 owns a
  `pre-M4` baseline alongside it.

## Decomposition into single-PR issues

Each entry is one PR, dependency-ordered. Entry 1 is this document and the ADR set; entries 19
and 20 are the operator-run proofs on real hardware.

| # | Issue | Depends on | Area |
|---|---|---|---|
| 1 | **The ADR set + this document.** Five ADRs and the milestone contract; open questions 1–4 resolved. | — | design |
| 2 | **#1817 — `ResourceKind.BYO_HOST`, migration, inventory schema, package skeleton.** `ByoHostInstance` + `InventoryDoc` field + the host-coordinate uniqueness check (ADR-0540) + the reconcile arm, which is the sole writer of BYO's *declared host-fact* capability keys (the
teardown cordon reason is written by entry 16 and cleared through `resources.set_scheduling`) **including `guest_arches` and `pseries_fadump`**; `systems.toml.example`; and `providers/byo_host/` with a buildable runtime whose ports are fail-closed stubs. Inseparable: the CHECK widen and the registered runtime must land together. A second inseparability, alongside the CHECK/runtime one: entry 2 makes `byo-host` composable, and `src/kdive/mcp/tools/lifecycle/systems/profile_examples.py:120` indexes `PROVIDER_SECTIONS[kind]` for every composed kind **unguarded** (its sibling `src/kdive/profiles/provider_sections.py:58` guards the same lookup). So entry 2 adds the `PROVIDER_SECTIONS` row too — a placeholder until entry 3 supplies the real model — or an operator who opts in between the two merges gets a `KeyError` from the profile-examples tool rather than a fail-closed stub refusal. Test work is larger than a rename: `build_provider_resolver` gains an `enable_byo_host` parameter and the two resolver-constructing parity tests that enable the opt-ins (`tests/db/test_resource_kind_parity.py:40`, `:55`) pass it; a third, `test_default_production_registry_registers_only_local_libvirt` (`:70`), deliberately builds with the opt-ins off and is what pins BYO's default to unregistered, or `assert allowed <= buildable` (`:44`) fails the moment the CHECK admits the kind; `test_check_admits_all_three_kinds` (`:25`, asserting at `:30`) is renamed and widened, and `test_every_registered_kind_is_check_allowed` (`:49`) is the third test in the set. | 1 | providers + db |
| 3 | **#1819 — provisioning profile section and policy.** `ByoHostProfile`, `ProviderSection.byo_host_section`, `ByoHostProfilePolicy`, and the `adopted-host` boot method with the generalized pairing map. | 2 | provisioning |
| 4 | **#1818 — the OOB control port seam and the Redfish driver.** The typed protocol under `providers/byo_host/oob/`, the console lease **and its stranded-lease reconciler reclaim**, and Redfish. The reclaim lands here rather than with teardown: ADR-0539 rejects expiry-alone outright, so shipping the lease without it would run the epic in a configuration that record refuses — for four waves, from the first real holder (entry 11) until entry 16. It is a `reconciler/loop.py` arm, an already-allowlisted file, so it costs no extra touch-point here. Depends on entry 2 as well as entry 1: the lease writes `byo_console_leases`, which entry 2's migration creates, and it lives in the `providers/byo_host/` package entry 2 skeletons. | 1, 2 | providers + security |
| 5 | **#1821 — the IPMI driver.** Additive behind the entry-4 seam. | 4 | providers |
| 6 | **#1822 — the HMC driver for PowerVM LPARs.** Power, partition identity, vterm. Sequenced early: it is the driver most likely to force a port revision. | 4 | providers |
| 7 | **#1816 — survey the OOB surface beyond power and console.** Read-only spike; produces findings and follow-on issues, not code. | — | providers |
| 8 | **#1823 — adopt: the Provisioner, the precondition module, and runtime binding.** Also sets `platform_root_cmdline=None` and `binding`. The declared arch is **not** advertised here — reconcile owns that write (entry 2), because an entry written at adopt arrives after the `systems.create` check it exists to close. | 3, 4 | provisioning |
| 9 | **#1824 — the `doctor` diagnostics contribution.** Second caller of the entry-8 precondition module, plus an arm reporting a present-but-unparseable `[[byo_host]]` declaration (ADR-0538) — the case the runtime degrades on silently. | 8 | diagnostics |
| 10 | **#1825 — install and boot/readiness.** In-target presigned-GET pull, arch-keyed bootloader write, `kgdboc` on the cmdline, boot-id readiness. | 8 | build-install |
| 11 | **#1826 — the console plane over the leased OOB channel.** Collect, rotate, snapshot; reuses `providers/console_parts/`. | 4, 8 | console |
| 12 | **#1827 — control: SysRq force-crash and OOB power.** Sets `supports_diagnostic_sysrq` and `supports_crash_watch`. | 11 | control |
| 13 | **#1829 — crash capture and retrieve.** kdump on both arches, fadump on POWER; owns replacing the QEMU-version-floor fadump probe (open question 5). | 10, 12 | capture |
| 14 | **#1828 — the KGDB transport.** The third `DebugTransportKind`, the debug-session registrar arm, the allowlist entry, and the console-to-loopback bridge. | 11 | debug |
| 15 | **#1831 — in-target drgn and vmcore postmortem.** | 13 | debug |
| 16 | **#1830 — teardown: baseline restore, OOB power-cycle, cordon-and-clear, reconciler drift arm.** The drift arm keys on a Resource cordoned with a restore-in-progress reason and no live worker, **not** on a System state: `torn_down` is terminal (`domain/capacity/state.py:249`) and there is no teardown-in-progress member (ADR-0541). Shares the dead-worker shape and the `reconciler/loop.py` entry with the lease reclaim entry 4 lands. | 12 | lifecycle |
| 17 | **#1820 — extend the portability gate.** The `pre-M4` baseline tag, the `byo-host` `CAPTURE_COVERAGE` row, the **registered-kinds completeness assertion** that makes a missing row detectable at all, the nine new allowlist entries, **re-pointing the 14 stale `ALLOWED_FILES` paths** plus a guard failing the gate on a member absent from disk, and the CI-wiring decision above. | 2 | tooling |
| 18 | **#1832 — operator runbook and agent-facing documentation.** | 13, 16 | docs |
| 19 | **#1833 — live proof: the full spine on an x86 host with a BMC.** | 14, 15, 16 | proof |
| 20 | **#1834 — live proof: the full spine on a PowerVM LPAR via HMC**, including fadump. | 6, 14, 15, 16 | proof |

**Merge wave:** `1` → `{2, 7}` → `{3, 4, 17}` → `{5, 6, 8}` → `{9, 10, 11}` → `{12, 14}` →
`{13, 16}` → `{15, 18}` → `{19, 20}`. Entry 4 sits *after* entry 2 rather than beside it, because
it writes a table entry 2 creates — entries are worked in parallel worktrees, so entry 4 merging
first would otherwise be the ordinary case, not the unlucky one.

### Sequencing & shared seams (no separate plan)

This milestone follows the M2 model: detailed design lives here and in the five ADRs, and each
entry is planned and implemented end-to-end by `work-issue`. There is no separate
implementation plan. The cross-entry concerns no single entry owns are pinned here:

- **One migration, claimed up front.** Entry 2 owns `0112_resources_kind_byo_host.sql` — the
  only DDL this milestone — and lands early and alone, because ADR-0517 makes migration numbers
  strictly ascending across merges and a second schema-touching entry would force a
  renumber-on-rebase. That is why it also carries the console-lease table (written by entry 4,
  one wave later) and the adopt-facts column (entry 8, three waves later): briefly-unused schema
  is the price of keeping the epic to one serialization point.
- **One precondition module, three callers.** Entry 8 writes it, entry 9 (`doctor`) calls it,
  entry 16 (teardown) calls it. Writing the checks twice is how they come to disagree, which is
  why 9 follows 8 rather than preceding it.
- **The port before its consumers.** Entry 4 is strictly before 5, 6, 11, 12, 14, and 16.
  Entry 6 (HMC) is sequenced immediately after 4 rather than last, because its
  managed-system-plus-partition addressing is what stresses the port shape — a revision it
  forces is cheap there and expensive after four planes are written.
- **Entry 17 follows entry 2 by convention, not by a failing test.** Nothing today detects a
  registered kind with no `CAPTURE_COVERAGE` row (see the gate section above), so the ordering
  is a discipline until entry 17 lands the completeness assertion that makes it enforceable.
  Treat the row as owed the moment entry 2 merges.
- **Expected rebase zones:** `providers/assembly/composition.py` (entry 2 registers; 3–16 wire
  ports), `src/kdive/domain/catalog/resources.py` (one enum value, entry 2),
  `tests/db/test_migrate.py` (entry 2's migration), and the generated `docs/guide/reference/*`
  only if a tool docstring shifts — regenerate with `just docs`, never hand-edit.
- **Provisioning parity is the extender's job.** Every host tool a live tier needs is declared
  in the owning Ansible role in the same PR that introduces it. Entries 19 and 20 are where this
  bites: an undeclared host dep passes on a warmed dev box and breaks the next clean runner
  reprovision.
- **Each entry runs in its own worktree**, placed outside the repo tree, whenever entries are
  worked in parallel.

## Carried invariants

1. **The provider seam is unchanged** (ADR-0063) — `byo_host` satisfies the same
   `ProviderRuntime` ports and registers into a resolver that already exists. The portability
   hypothesis is measured a third time rather than abandoned, and all nine genuinely new core
   touches — the migration, the `arch_traits` docstring, `jobs/handlers/systems.py`, the two
   debug-session modules, `ops/resources/host_ops.py`, the two control handlers that must hold a
   console-lease scope, and the `resources.describe` projection — are declared up front rather
   than
   discovered.
2. **Secrets never leak** (ADR-0012, ADR-0073) — OOB credentials resolve at the worker
   boundary, register for redaction before first use, and release only after
   redact-and-persist. Nothing is materialized to disk, and no credential travels on a command
   line.
3. **The out-of-band path never falls back in-band** (ADR-0539) — the plane exists to work when
   in-band access is gone, so a fallback would make an OOB failure invisible until the moment
   it mattered. `doctor` reports OOB reachability before allocation instead.
4. **One console, one holder** (ADR-0539, ADR-0542) — every consumer goes through the lease. A
   suspended collector reports `pumped=False` to a live reader rather than empty bytes, and a
   refused acquirer is told who holds the channel and how to get it. The per-Run artifact carries
   no gap marker; ADR-0539 records that as accepted rather than solved.
5. **A crashed host never silently becomes the next allocation's starting point** (ADR-0541) —
   and the guarantee is continuous, not just true at the endpoints. The host is cordoned from
   the start of the restore until it has been verified back on its declared baseline, so there
   is no window in which a machine mid-restore is schedulable, even though its allocation and
   System both went terminal before the restore began. A worker that dies mid-restore leaves an
   already-cordoned host; the reconciler never retries a restore over unknown state.
6. **`byo_host` is opt-in** — the runtime and its discovery registrar compose only when an
   operator declares a `[[byo_host]]` entry, so a deployment without one has no bookable BYO
   resource. Registration is bind-only; `reconcile_resources` is the sole creator.
