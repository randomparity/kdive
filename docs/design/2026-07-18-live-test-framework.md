# Live-test framework — a reusable harness for exercising live local-libvirt setups

- **Date:** 2026-07-18
- **Status:** Draft
- **Epic:** [#1289](https://github.com/randomparity/kdive/issues/1289) (sub-issues #1290–#1295)
- **ADR:** [0386 — live-test framework and arch-additive runner topology](../adr/0386-live-test-framework-runner-topology.md)

## Problem

The repository has two tiers of live test, at very different levels of maturity.

**Tier 1 — `live_stack` (drive the running stack over MCP/HTTP): already well-factored.**
`tests/integration/live_stack/spine.py`, `src/kdive/mcp/dev_harness.py`
(`LiveStackClient.over_http`), and `tests/integration/live_stack/conftest.py`
(`require_issuer` / `require_stack` / `require_guest_arch`) are shared cleanly
between the local and remote spine drivers. This tier is not a subject of this
work.

**Tier 2 — `live_vm` (boot a real throwaway libvirt domain, run a provider op
against it): no shared harness at all.** Every provider test re-derives the same
sequence:

- Throwaway-domain boot + cleanup is copy-pasted between
  `tests/providers/local_libvirt/test_traffic_capture_live.py` and
  `test_snapshot_live.py` (same `qemu-img create` overlay, same inline
  `<domain type='kvm'>` XML, `defineXML → create() → sleep → isActive`, same
  `finally` teardown).
- The "Kernel panic" console-wait loop exists in three independent copies
  (`test_debug_live_attach.py`, `test_debug_gdbmi_live_smoke.py`,
  `test_live_preserve_attach.py`).
- The `libvirt.open(uri)` connect dance is repeated across roughly fourteen
  sites.
- Env-var skip preflights (`KDIVE_LIVE_VM_ROOTFS`, `KDIVE_LIBVIRT_URI`) are
  re-implemented per module instead of sitting beside the existing `require_*`
  gates.

The environment knowledge that makes these tests pass — use `qemu:///session`
to avoid the root-readback wall, keep `XDG_RUNTIME_DIR` short for the QMP
socket-path length limit, run the modular libvirt daemons, relabel staged
images `virt_image_t` under SELinux — lives only in one test file and in
maintainer memory. Each new live test re-derives it from scratch. This is the
"the agent had to relearn how to test what we built" symptom that motivated the
work.

**The `live_vm` CI job is inert.** `.github/workflows/ci.yml` defines a
`live-vm` job, but it is `workflow_dispatch`-only, targets `[self-hosted, kvm]`,
sets no environment, and stages no guest image — so it skips even when
dispatched. The product's core boundary (boot a real kernel, crash it,
introspect the vmcore) therefore has no automated coverage. Fakes
(`FakeLibvirtConn`, `FakeDomain`) are structurally blind to exactly the failures
that matter here: libvirt rejecting domain XML, QEMU `filter-dump` emitting no
packets, a real panic not detected on a real console, snapshot-revert
corruption, arch/accel resolving wrong on real silicon.

## Goals

1. A thin, arch-parameterized `live_vm` harness that is the single reusable way
   to boot a throwaway libvirt domain, wait for a chosen condition
   (`active` / `panic` / `ssh`), and tear it down — with the environment quirks
   encoded once and the skip gates centralized.
2. Make the live tests actually run somewhere on a schedule:
   emulated `live_vm_tcg` on a hosted `ubuntu-latest` runner for breadth;
   native-KVM `live_vm` on per-arch self-hosted runners for depth.
3. An **arch-additive** runner topology: x86_64 self-hosted now as the
   cost-effective proof-of-concept; ppc64le self-hosted as the primary target
   the design must not block. Adding a POWER runner is additive, not a rewrite.
4. Migrate the existing `live_vm` tests onto the harness and delete any that
   only re-prove what the fakes already cover.
5. Stop the relearning: one canonical live-testing guide, an `AGENTS.md`
   pointer, and a runbook.

## Non-goals

- No change to the `live_stack` HTTP tier — it is already factored well.
- No product feature work and no database migration; this is test
  infrastructure only.
- Standing up the ppc64le runner is out of scope for this phase. The obligation
  here is that nothing in the design blocks it.

## Architecture

### The live tiers and their vehicles (target state)

There are two distinct test *vehicles*, and the markers map onto them — they are
not one harness:

| Marker | Vehicle | What it drives | Accel | Where it runs |
| --- | --- | --- | --- | --- |
| `live_stack` | live-stack spine (`spine.py`, existing) | the running stack over MCP/HTTP | n/a | unchanged |
| `live_vm_tcg` | live-stack spine (existing, ADR-0353) | the ppc64le provision→boot→crash→retrieve spine, emulated | TCG | hosted `ubuntu-latest` (compose backends + S3, no KVM) |
| `live_vm` — throwaway-domain family | **new** `boot_throwaway_domain` harness | a short-lived domain + one provider op | KVM | self-hosted, arch-labeled |
| `live_vm` — provisioned-System family | externally provisioned System (`KDIVE_LIVE_VM_SYSTEM_ID`) + S3 | kdump/install against a real System | KVM | self-hosted, arch-labeled |

Two consequences follow, and they correct an easy misconception:

- `live_vm_tcg` is **not** a throwaway-domain test. By ADR-0353 and the
  `test_live_vm_tcg_tier.py` guard, every `live_vm_tcg` proof is also
  `live_stack`-marked and runs the spine over the stack; its cross-arch
  KVM-vs-TCG resolution already lives in the spine (`expected_accel` /
  `kvm_probe_for_uri`). Because TCG needs no `/dev/kvm`, that spine runs on a
  hosted `ubuntu-latest` runner once the compose backends + S3 are stood up — no
  special hardware, but a full stack bring-up, not a lightweight boot.
- The **new** work this epic adds is the `boot_throwaway_domain` harness for the
  throwaway-domain `live_vm` family only. It does not replace the spine and does
  not by itself produce cross-arch TCG coverage. Authoring throwaway-domain tests
  that boot a foreign arch under TCG is possible but out of scope here, and would
  revise the ADR-0353 guard.

### Runner topology (arch-additive)

- **Hosted tier — `ubuntu-latest` (x64), no KVM.** Runs the existing
  `live_vm_tcg` spine, so it stands up the live-stack compose backends + S3
  (Postgres/MinIO/OIDC) and runs the ppc64le provision→boot→crash→retrieve path
  under TCG — no `/dev/kvm`, but a full stack bring-up. A ppc64le boot-to-panic
  under TCG is minutes-scale and variable, so this tier carries an explicit job
  timeout and a target boot-to-panic wall-time, and the PR-gate-vs-nightly choice
  is made on the measured wall-time and flake rate, not left as "either". The
  runner's guaranteed ~14 GB workspace is supplemented by a larger (~70 GB)
  `/mnt` scratch volume, so sub-issue C stages the compose backends and the
  ppc64le image set (rootfs + kernel + matching debuginfo) there and fetches
  debuginfo on demand rather than pre-baking it, with a measured disk budget as
  an acceptance criterion. This image set is a distinct, ephemeral input from the
  self-hosted warm store; C produces both and keeps them separate.
- **Self-hosted tier — arch-labeled, native-KVM for the host's own arch.**
  - `[self-hosted, kvm, x64]`, Rocky Linux 10 — provisioned in this phase.
  - `[self-hosted, kvm, ppc64le]`, Rocky Linux 10 ppc64le — the north-star
    drop-in. The HW-validation environment already has POWER10 hardware to
    target. (Confirm Rocky 10 ppc64le image availability at provisioning time.)

The CI job selects self-hosted runners by arch label, so a POWER runner joins as
a new matrix entry with no change to the runner-selection topology. The harness
itself still carries an arch branch: the domain-XML builder must resolve machine
type (`pseries` vs `q35`), console device (`hvc0` vs `ttyS0`), and kernel format
(ppc64le `vmlinux` vs x86 `bzImage`), and the panic-wait must read the right
console. Sub-issue A owns that arch-parameterized builder;
`test_introspect_ppc64le_live.py` is the pseries reference. Rocky Linux 10 on
both arches keeps the host-setup codification arch-parameterized rather than
duplicated.

### Two families under the `live_vm` marker

The `live_vm` marker today spans two kinds of test with different needs, and the
harness and the nightly must treat them distinctly:

- **Throwaway-domain tests** (e.g. `test_traffic_capture_live.py`,
  `test_snapshot_live.py`, `test_debug_live_attach.py`) boot a short-lived
  domain from a staged rootfs and run a provider op against it. These are what
  `boot_throwaway_domain` serves. Required input: a staged bootable rootfs and a
  resolved libvirt URI.
- **Provisioned-System tests** (e.g. `test_retrieve_kdump.py`,
  `test_install.py`) run against a fully provisioned System through the live
  stack plus the required S3 object store (#1133). They need
  `KDIVE_LIVE_VM_SYSTEM_ID`, `KDIVE_LIBVIRT_URI`, and the `KDIVE_S3_*` backend —
  not a throwaway rootfs — and do not fit `boot_throwaway_domain`.

`pytest -m live_vm` selects both families. A nightly that sets only the
throwaway-domain env would silently skip every provisioned-System test and still
report green — the "green run that is no coverage" failure this epic exists to
kill. So the nightly must declare which families it intends to run, and a
preflight must **fail loud** (the `require_free_http_port` pattern from
`scripts/live-stack/lib.sh`) when a declared family's required env is absent,
rather than skipping to green. As a roadmap success criterion, the self-hosted
KVM nightly **must** run the provisioned-System family natively (kdump/install
on a real System) — that native depth is why the KVM box exists, and emulated
TCG does not substitute for it (the design's own point that emulation misses
"arch/accel on real silicon"). The open C/D detail is *how* the runner stands up
that System (live stack + S3 on the box, or an externally provisioned System by
id), not *whether* it runs: the family is declared to the fail-loud preflight so
a missing System fails the job instead of going green.

Because `pytest -m live_vm` selects both families and no sub-marker exists to
pick one, sub-issue A must add a **family-selection primitive** — distinct
`live_vm_throwaway` / `live_vm_provisioned` sub-markers under the `live_vm`
umbrella, or a documented keyword convention — so "declare which families" has a
real handle and D's fail-loud preflight has a declared-family input. The
sub-markers are **additive**: every test keeps the bare `live_vm` marker and
adds its sub-marker, so `-m live_vm` still selects both and the shipped
`test-live` recipe (`-m "live_vm and not live_vm_tcg"`) is unaffected. Registering
the new markers in `pyproject.toml` `[tool.pytest.ini_options].markers` is
required — the tcg lane runs under `--strict-markers`.

### The environment contract (the seam)

The contract is the interface between "the runner host" and "the tests" — the
thing whose absence forces relearning. Fixing it is the point of sub-issue A,
and the host build (sub-issue B) is built to satisfy it. The libvirt **mode is a
resolved contract variable, not a single pin**, because the two families need
different modes:

- **libvirt URI / mode:** a **per-test** default that `boot_throwaway_domain`
  takes as a parameter, with `KDIVE_LIBVIRT_URI` as the operator escape hatch —
  not a per-family or global pin. The throwaway-domain family itself splits:
  `test_traffic_capture_live.py` needs `qemu:///session` (unprivileged, dodges
  the root-readback wall, #1258) while `test_snapshot_live.py` uses
  `qemu:///system` (the product default). The harness carries each test's mode
  rather than forcing one; "per-family" governs only the env-var *set* each
  family needs (below), never the mode.
- **Environment variables:** the throwaway rootfs (`KDIVE_LIVE_VM_ROOTFS`) and
  guest arch for the throwaway family; `KDIVE_LIVE_VM_SYSTEM_ID` and the
  `KDIVE_S3_*` backend for the provisioned-System family — read in one place,
  not per module.
- **`XDG_RUNTIME_DIR`:** kept short enough for the QMP socket path (session
  mode).
- **libvirt daemons:** modular (`virtqemud` / `virtnetworkd`).
- **Guest confinement (mechanism named per environment):** SELinux
  `virt_image_t` relabel for staged images under **system mode** on the
  RHEL-family self-hosted runner; AppArmor's `libvirt-qemu` profile on the
  Ubuntu hosted runner if it runs system mode. **Session mode engages neither**
  (qemu runs as the invoking user with no sVirt relabel), so a session-mode tier
  sidesteps both.
- **Guest image and matching debuginfo:** staged at a known location, kept warm
  between runs on the self-hosted host.

### Harness surface (sketch — detailed in sub-issue A's spec)

- `boot_throwaway_domain(rootfs, *, arch, mode=…, netdev=…, wait_for="active"|"panic"|"ssh")`
  as a context manager that yields a live domain and guarantees teardown. `mode`
  (session/system) is per-test; `arch` drives the machine-type / console /
  kernel-format branch.
- A family-selection primitive (`live_vm_throwaway` / `live_vm_provisioned`
  sub-markers, or a keyword convention) so a run can target one family and the
  fail-loud preflight has a declared-family input.
- Centralized `require_live_vm_*` skip gates alongside the existing
  `require_issuer` / `require_stack` / `require_guest_arch`.
- One libvirt-connect helper, replacing the ~14 open sites.
- Shared panic-wait, `qemu-img` overlay creation, and an arch-parameterized
  domain-XML builder, replacing the three panic-loop copies and the per-test XML.

## Sub-issues

| Sub | Title | Kind | Depends on |
| --- | --- | --- | --- |
| **A** | `live_vm` harness + environment contract | code | — |
| **B** | Self-hosted KVM runner: reproducible, arch-parameterized host setup | infra/ops | A (contract) |
| **C** | Guest-image + debuginfo provisioning — self-hosted warm store **and** the hosted TCG image set (staged on `/mnt` scratch, measured budget) | code + ops | A |
| **D** | CI wiring — TCG on hosted; finish the self-hosted `live_vm` job; fail-loud env preflight | code | A, B, C |
| **E** | Migrate + prune `live_vm` tests — throwaway family onto `boot_throwaway_domain`, provisioned-System family onto the shared `require_live_vm_*` gates | code | A |
| **F** | Discoverability — canonical guide + `AGENTS.md` pointer + runbook | docs | A–E |

Sub-issue A is the root: it is the reusable framework the work is named for, and
it defines the contract the host build targets, so it lands first. B and C can
proceed in parallel once the contract is fixed. B leaves room for non-x86
runners by keeping every host-setup step arch-parameterized and selecting the
qemu emulator and rootfs by arch rather than hard-coding x86.

B also owns the runner's GitHub registration token and how scheduled runs obtain
the `KDIVE_S3_*` credentials for the provisioned-System family: repository or
organization secrets are available to `schedule` and `workflow_dispatch` runs on
the base repo (but never to fork pull requests), so D's fail-loud preflight
asserts they are present for a declared family and fails the job if not, rather
than skipping to green.

## Risks and mitigations

- **A self-hosted nightly can rot** — a flaky job that gets ignored is slower
  theater. Mitigation: the hosted TCG tier is the hedge (it needs no special
  hardware and cannot rot the same way); keep the self-hosted job on
  `schedule` + `workflow_dispatch` only, never on fork pull requests.
- **Disk and debuginfo cost** — kdump/vmcore and matching `vmlinux` debuginfo
  are large. Mitigation: persistent, warm storage on the self-hosted host
  (sub-issue C), sized for it; the hosted tier stages images on the ~70 GB
  `/mnt` scratch and fetches debuginfo on demand, under a measured budget.
- **Rocky 10 ppc64le availability** — confirmed at provisioning time, not
  assumed; it does not block the x86_64 phase.

## Rollout order

`A → (B ∥ C) → D → E → F`. A first, because it is the shared dependency and it
fixes the environment contract the runner host is built against.
