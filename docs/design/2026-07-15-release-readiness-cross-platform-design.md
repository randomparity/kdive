# Release-readiness: cross-platform + local-libvirt hardening — design spec

Status: approved (brainstorming) · Date: 2026-07-15 · Target release: **v0.3.0**

## Goal

Cut the next KDIVE release around one headline: **the local-libvirt kernel-debug loop now
runs on x86_64 *and* ppc64le.** The prior release-readiness effort (ADR-0114, 2026-06-14)
built the *release surface* (audience-tiered docs, host preflight, governance files); v0.1.0
and v0.2.0 have shipped since. This release **hardens what exists** — cross-platform
correctness, local-libvirt stability, honest documentation of the supported surface — and
deliberately **adds no new agent debug tools**.

**Falsifiable acceptance signal:** a newcomer follows the operator docs to a running
local-libvirt debug loop (provision → boot → crash → vmcore/drgn) on **both** an x86_64 host
and a ppc64le (POWER) host, using only `operating/` docs and the platform-support matrix — no
source reading — and the published multi-arch release image + OIDC mirror pull unauthenticated
on both arches.

## Governing principle (new, enshrined here)

**A capability earns an MCP tool only if it is out-of-band** — something an agent cannot do
from inside the guest over its existing root SSH access. Anything achievable over root SSH
(running commands, tracing via `/sys/kernel/{tracing,debug}`, loading modules, staging files,
running selftests/reproducers) stays a **documented prompt pattern**, not a tool. This keeps
the agent surface small on purpose and is the filter applied to epic #998 below. Rationale:
the guest is the agent's as root (external-build lane + `systems.authorize_ssh_key`), so a
tool that merely wraps SSH adds governance overhead, not capability. Out-of-band observation
(watching a guest as it panics, non-halting live introspection) is the genuine differentiator
and is where tooling investment belongs.

## Scope

Five workstreams. **In scope** is the union of their issue lists below; **out of scope** is
everything else in the 46-issue backlog, notably the SSH-equivalent half of epic #998, the
Keycloak real-OIDC build-out, runtime-mutable inventory (#429), and all post-first-release
upgrade concerns.

### Workstream A — Cross-platform finish

| Issue | Why in scope |
|-------|--------------|
| **#1181** (bug) | fadump native-POWER capture fails run-readiness at the 2 GiB guest-RAM profile — the only real bug in the backlog; cross-platform correctness. Resolve, or document as a known limitation in the matrix. |
| **#1176** | Differential cost support for alternate architectures — arch-aware accounting the release needs. |
| **#1174** | EL9 customize-boot not verified end-to-end (ppc64le TCG dnf stall + x86_64) — the gated live-proof from #1152; verify or document the gap. |

### Workstream B — local-libvirt hardening

| Issue | Why in scope |
|-------|--------------|
| **#958** | Gate `vmcore.fetch` (KDUMP method) on the computed kdump capability — correctness guard. |
| **#980** *(candidate)* | Advertise per-host guest CPU model/capabilities at System selection — arch-capability transparency. Confirm before build. |
| **Security posture** | Document the supported auth for the release (dev mock-OIDC vs. real). **#351** (retire the mock-only `kdivectl login`) is in scope; the Keycloak build-out (**#349/#350/#352/#353**) is deferred to its own security epic. |

### Workstream C — Out-of-band #998 only (add nothing SSH already does)

Applying the governing principle to every open #998 item:

**Keep (genuinely out-of-band):**
- **#986** — non-halting observation of a kernel value under race load. May resolve as a
  *docs* decision (route races to the existing out-of-band drgn-live + tracepoints) rather
  than a new tool; the non-stop/hardware-watchpoint gdbstub mode is the only code delta.
- **#984** *(out-of-band half only)* — the console sidecar catches the panic signature as the
  guest dies and auto-captures; an SSH loop dies with the guest and can't observe its own
  crash. Keep the watch-and-capture trigger; the reproducer loop itself stays root SSH.

**Close as SSH-equivalent** (principled comment citing this spec's governing principle):
#909 (guest exec), #910 (exec records), #911 (kselftest), #912/#913/#914/#915 (trace-buffer /
ftrace / kprobe / dynamic-debug — all `echo … > …; cat trace` over SSH), #926 (payload upload
= scp), #927 (module load/unload = insmod/rmmod), #928 (syzkaller replay = run in guest).
(#916/#917/#918/#919 are already excluded by the #998 epic on the same reasoning — the agent
sets `CONFIG_KASAN`/`FAILSLAB` in its own external build.)

**Already done (moot):** #985, #988, #989, #991, and the six doc guides #992–#997.

### Workstream D — Docs & release notes

- **NEW — Platform & Architecture support matrix** (user-facing; the confirmed gap). Honest
  tiers: x86_64 KVM (primary), ppc64le KVM on POWER (supported), ppc64le TCG (cross-arch,
  slow/CI-only), and which distros are customize-boot-verified — flagging the **#1174 EL9**
  gap and **#1181 fadump** limitation rather than implying full parity.
- **NEW — race-debugging guide** — where **#986** lands as docs: route race investigation to
  the existing out-of-band drgn-live + tracepoints, and explicitly document the *"the guest is
  yours as root — run commands / loop reproducers over SSH"* contract, so the outcomes of every
  discarded #998 tool are covered by documentation, not tools.
- **UPDATE** `operating/providers/local-libvirt.md` + walkthrough — currency pass for ppc64le
  accel (KVM/TCG), the TCG boot-deadline multiplier, arch-aware host deps (rustc on ppc64le,
  #1186).
- **UPDATE** `operating/install.md` — arch prerequisites.
- **UPDATE** `development/releasing.md` — the multi-arch image + OIDC-mirror publish now in the
  pipeline (#1184/#1185) is undocumented there.
- **CHANGELOG** — curate the large `[Unreleased]` section into the v0.3.0 release.

### Workstream E — Release process & the cut

The process **exists and is exercised** (v0.1.0/v0.2.0 tagged; ADR-0041, `just set-version` /
`release` / `changelog`, `release.yml` + `release-image.yml`). This workstream closes the gaps
a cross-platform cut exposes — it does **not** rebuild the process.

- **Version alignment** — reconcile `pyproject` `0.3.0` vs Chart `0.4.0`: confirm the target
  (**v0.3.0**) and either align the chart or record that it versions independently (and make
  `chart-version-check` reflect the decision).
- **Multi-arch in `releasing.md`** — documented in Workstream D.
- **Dry-run the cut** — exercise `set-version → changelog → tag → release.yml →
  release-image.yml` and verify the published **multi-arch** kdive image + OIDC mirror pull on
  both arches (the ppc64le pull path is already proven on the local POWER VM).
- **Supply chain** — confirm cosign keyless signing + SBOM cover the ppc64le digests.

## Out of scope

- The SSH-equivalent #998 tools (closed, not built) and all Tier-3/4 ergonomics not listed.
- Keycloak real-OIDC build-out (#349/#350/#352/#353) — separate security epic.
- Runtime-mutable inventory (#429), mkosi research (#961), Helm projected secrets (#550).
- **All upgrade/migration concerns**, including **#1172** (capability refresh on upgrade) —
  its own body notes it degrades fail-closed and "only bites in-place upgrades; kdive is
  pre-first-release, so there are no deployments to upgrade today." Deferred to when
  deployments exist.
- New product features; changing the release/versioning process (ADR-0041 stands).

## Sequencing

1. **A (cross-platform correctness) + B (hardening)** land first — they gate an honest support
   matrix. #1181 and #1174 must be resolved-or-documented before D's matrix can be truthful.
2. **C** — close the SSH-equivalent #998 issues (execute after this spec is approved); design
   #986/#984 if kept as code (else #986 → a D doc).
3. **D (docs)** follows A/B so the matrix and guides describe shipped reality (no phantom steps).
4. **E (the cut)** closes out last: version reconcile, releasing.md, dry-run, verify multi-arch
   publish, then tag v0.3.0.

Each workstream item is its own branch/PR (no squash for code), tracked under a single
release-readiness epic issue + a `v0.3.0` GitHub milestone.

## Acceptance

- The falsifiable signal above passes on both arches.
- `just ci` green (incl. generated tool/config reference currency via `docs-check`).
- Every in-scope issue closed or merged; every SSH-equivalent #998 issue closed with the
  principled comment; the platform-support matrix published and honest about tiers/gaps.
