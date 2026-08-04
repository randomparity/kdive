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
| 3 | Console multiplexing between KGDB and log capture | KGDB takes the console lease exclusively. Collection suspends and the read seam reports not-pumped; `supports_crash_watch` stays `True` and the tools refuse with `transport_conflict` naming the holder. | ADR-0539, ADR-0542 |
| 4 | Does `arch_traits()` split? | No. Four of its six fields are libvirt domain-render facts; of the two that generalize, `console_device` is a per-host firmware setting on metal. One arch-keyed field does not justify a second table. The module docstring is amended to mark field scope. | ADR-0540 |
| 5 | fadump detection on real firmware | Open. Owned by entry 13 (#1829), which replaces the QEMU version floor at `src/kdive/providers/shared/fadump_detect.py:17`. Not architecture — a probe choice made against real hardware. |  |
| 6 | Cost-class coefficient value | Open. An operator pricing input to entry 2 (#1817). The migration must seed *a* row; which number is not a design decision. |  |
| 7 | Concurrency ceiling | Settled as `concurrent_allocation_cap = 1`: a force-crash takes the whole machine, so no partitioning case survives the provider's central operation. | ADR-0540 |

## What M4 adds

- **One `byo_host` provider package serving both architectures.**
  `src/kdive/providers/byo_host/` with its own discovery, adopt, install, boot, console,
  control, retrieve, debug, and teardown modules over the same typed `ProviderRuntime` ports.
  The x86/POWER split is visible in exactly two places: behind the OOB driver port, and in one
  arch-keyed bootloader module. A new `ResourceKind.BYO_HOST = "byo-host"` and one migration
  (CHECK widen **plus** cost-class seed) register the fourth kind behind the per-kind
  `ProviderResolver`.
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
  a gap in a console artifact is legible as a lease rather than as silence
  ([ADR-0429](../adr/0429-remote-console-read-seam.md)).
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
- **Restore-verify-or-cordon teardown.** Re-point the bootloader at the declared
  `baseline_kernel`, power-cycle out-of-band, re-run the adopt preconditions against the
  rebooted host, and only then free the Resource. Any failure cordons with a reason persisted
  on the Resource, surfaced as `restore_incomplete`. The reconciler gains a BYO drift arm that
  drives a mid-teardown host to cordoned, never to available, and never retries the restore.

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
  `src/kdive/db/schema/0111_restrict_pinned_job_deletion.sql`; ADR-0517 makes migration numbers
  strictly ascending across merges, so it lands early and alone). It widens
  `resources_kind_check` to admit `'byo-host'` **and** seeds a cost-class coefficient row in
  the same file. Forward-only.
- The seed is not optional. Admission resolves the coefficient fail-closed, so a cost class
  with no row denies every allocation with a message about cost classes rather than about the
  host. `src/kdive/db/schema/0032_remote_cost_class_coefficient.sql` is the record of that
  defect shipping once already.
- **No new columns or tables.** A BYO host's SSH target, OOB endpoint and driver kind,
  credential refs, declared architecture, `baseline_kernel`, console device, and
  `concurrent_allocation_cap` are keys in the existing `resources.capabilities` jsonb, as
  remote-libvirt's connection config is. The teardown cordon reason is a further namespaced key
  there; reconcile merges rather than replaces
  (`src/kdive/inventory/reconcile/resources.py:292`, `:493`), so it survives a pass.
- The migration merges **only** alongside a registered, buildable runtime.
  `tests/db/test_resource_kind_parity.py:25` asserts the CHECK set by exact equality and `:33`
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
through the lease, so a suspended collector reports not-pumped rather than empty and two
consumers never interleave on one line.

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
`src/kdive/providers/` in its entirety (the package, the OOB port, and the
`DebugTransportKind` literal at `providers/ports/lifecycle.py:27-28`).

**Gated** — the allowlist entries this milestone owes:

| path | why | already allowlisted? |
|---|---|---|
| `src/kdive/domain/catalog/resources.py` | `ResourceKind.BYO_HOST` | yes (ADR-0076 touch-point) |
| `src/kdive/db/schema/0112_resources_kind_byo_host.sql` | the one migration | **no — new entry** |
| `src/kdive/domain/platform/arch_traits.py` | the docstring amendment marking field scope (ADR-0540) | **no — new entry** |
| `src/kdive/mcp/tools/debug/sessions.py` | the `kgdb` transport arm on the debug-session registrar | yes (entered for ADR-0085; BYO reuses it) |
| `src/kdive/reconciler/loop.py` | the BYO mid-teardown drift arm | yes (entered for ADR-0086; BYO reuses it) |

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
| 2 | **#1817 — `ResourceKind.BYO_HOST`, migration, inventory schema, package skeleton.** `ByoHostInstance` + `InventoryDoc` field + the host-coordinate uniqueness check (ADR-0540) + reconcile arm, `systems.toml.example`, and `providers/byo_host/` with a buildable runtime whose ports are fail-closed stubs. Inseparable: the CHECK widen and the registered runtime must land together. Also renames `test_check_admits_all_three_kinds` (`tests/db/test_resource_kind_parity.py:25`), whose exact-equality assertion at `:30` names three kinds. | 1 | providers + db |
| 3 | **#1819 — provisioning profile section and policy.** `ByoHostProfile`, `ProviderSection.byo_host_section`, `ByoHostProfilePolicy`, and the `adopted-host` boot method with the generalized pairing map. | 2 | provisioning |
| 4 | **#1818 — the OOB control port seam and the Redfish driver.** The typed protocol under `providers/byo_host/oob/`, the console lease, and Redfish. | 1 | providers + security |
| 5 | **#1821 — the IPMI driver.** Additive behind the entry-4 seam. | 4 | providers |
| 6 | **#1822 — the HMC driver for PowerVM LPARs.** Power, partition identity, vterm. Sequenced early: it is the driver most likely to force a port revision. | 4 | providers |
| 7 | **#1816 — survey the OOB surface beyond power and console.** Read-only spike; produces findings and follow-on issues, not code. | — | providers |
| 8 | **#1823 — adopt: the Provisioner, the precondition module, and runtime binding.** Also sets `platform_root_cmdline=None` and `binding`, and advertises the declared arch. | 3, 4 | provisioning |
| 9 | **#1824 — the `doctor` diagnostics contribution.** Second caller of the entry-8 precondition module. | 8 | diagnostics |
| 10 | **#1825 — install and boot/readiness.** In-target presigned-GET pull, arch-keyed bootloader write, `kgdboc` on the cmdline, boot-id readiness. | 8 | build-install |
| 11 | **#1826 — the console plane over the leased OOB channel.** Collect, rotate, snapshot; reuses `providers/console_parts/`. | 4, 8 | console |
| 12 | **#1827 — control: SysRq force-crash and OOB power.** Sets `supports_diagnostic_sysrq` and `supports_crash_watch`. | 11 | control |
| 13 | **#1829 — crash capture and retrieve.** kdump on both arches, fadump on POWER; owns replacing the QEMU-version-floor fadump probe (open question 5). | 10, 12 | capture |
| 14 | **#1828 — the KGDB transport.** The third `DebugTransportKind`, the debug-session registrar arm, the allowlist entry, and the console-to-loopback bridge. | 11 | debug |
| 15 | **#1831 — in-target drgn and vmcore postmortem.** | 13 | debug |
| 16 | **#1830 — teardown: baseline restore, OOB power-cycle, cordon, reconciler drift arm.** Also owns the **stranded-console-lease reclaim** (ADR-0539), which shares the dead-worker shape and the same `reconciler/loop.py` allowlist entry as the mid-teardown arm. | 12 | lifecycle |
| 17 | **#1820 — extend the portability gate.** The `pre-M4` baseline tag, the `byo-host` `CAPTURE_COVERAGE` row, the **registered-kinds completeness assertion** that makes a missing row detectable at all, the two new allowlist entries, and the CI-wiring decision above. | 2 | tooling |
| 18 | **#1832 — operator runbook and agent-facing documentation.** | 13, 16 | docs |
| 19 | **#1833 — live proof: the full spine on an x86 host with a BMC.** | 14, 15, 16 | proof |
| 20 | **#1834 — live proof: the full spine on a PowerVM LPAR via HMC**, including fadump. | 6, 14, 15, 16 | proof |

**Merge wave:** `1` → `{2, 4, 7}` → `{3, 5, 6, 17}` → `8` → `{9, 10, 11}` → `{12, 14}` →
`{13, 16}` → `{15, 18}` → `{19, 20}`.

### Sequencing & shared seams (no separate plan)

This milestone follows the M2 model: detailed design lives here and in the five ADRs, and each
entry is planned and implemented end-to-end by `work-issue`. There is no separate
implementation plan. The cross-entry concerns no single entry owns are pinned here:

- **One migration, claimed up front.** Entry 2 owns `0112_resources_kind_byo_host.sql` — the
  only DDL this milestone — and lands early and alone, because ADR-0517 makes migration numbers
  strictly ascending across merges and a second schema-touching entry would force a
  renumber-on-rebase.
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
   hypothesis is measured a third time rather than abandoned, and the two genuinely new core
   touches (the migration, the `arch_traits` docstring) are declared up front rather than
   discovered.
2. **Secrets never leak** (ADR-0012, ADR-0073) — OOB credentials resolve at the worker
   boundary, register for redaction before first use, and release only after
   redact-and-persist. Nothing is materialized to disk, and no credential travels on a command
   line.
3. **The out-of-band path never falls back in-band** (ADR-0539) — the plane exists to work when
   in-band access is gone, so a fallback would make an OOB failure invisible until the moment
   it mattered. `doctor` reports OOB reachability before allocation instead.
4. **One console, one holder** (ADR-0539, ADR-0542) — every consumer goes through the lease. A
   suspended collector reports not-pumped rather than empty, and a refused acquirer is told who
   holds the channel and how to get it.
5. **A crashed host never silently becomes the next allocation's starting point** (ADR-0541) —
   every path out of a Run that crashed the machine ends either in a host verified back on its
   declared baseline, or in a host no scheduler will pick. The reconciler fails closed to
   cordoned and never retries a restore over unknown state.
6. **`byo_host` is opt-in** — the runtime and its discovery registrar compose only when an
   operator declares a `[[byo_host]]` entry, so a deployment without one has no bookable BYO
   resource. Registration is bind-only; `reconcile_resources` is the sole creator.
