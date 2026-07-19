# ADR 0388 — Guest-image + debuginfo provisioning

- **Status:** Proposed <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-07-19
- **Deciders:** Maintainer (randomparity), Claude Code

## Context

Epic #1289 (directional ADR-0386) runs live tiers that boot a real kernel and
then introspect it. Introspection (`drgn`/`crash`, kdump/vmcore) needs three
staged artifacts kept in agreement: a bootable **rootfs**, the **kernel** the
domain direct-boots, and the **`vmlinux` debuginfo** matching that exact kernel.
A kernel/debuginfo mismatch does not raise — it silently resolves wrong symbols,
so a green introspection proof can be worthless. Sub-issue C (#1292) must produce
and stage these artifacts.

Two tiers consume them with different lifetimes, and the epic design is explicit
that they are **separate stores** (design §"Runner topology"):

- The **self-hosted native-KVM** tier runs nightly on a persistent host
  (sub-issue B, ADR-0387). Rebuilding the rootfs and refetching ~1 GB of
  debuginfo every night is waste; the store should stay warm and refresh only
  when the pinned kernel changes.
- The **hosted `ubuntu-latest` TCG** tier is ephemeral — a fresh runner per run,
  no persistent disk, a ~70 GB `/mnt` scratch shared with the compose backends
  and `$GITHUB_WORKSPACE`. Its image set is staged on `/mnt` per run with
  debuginfo fetched on demand, so it must stay under a bounded disk budget or it
  can evict the backing services mid-run.

The repository already has a rootfs *builder* — `build-fs` /
`LocalLibvirtRootfsBuildPlane` (ADR-0092/0345), which can even boot a foreign
arch under TCG to self-install packages — but nothing that produces the
kernel+debuginfo pairing, keeps the self-hosted store warm, or bounds the hosted
store's disk use.

## Decision

Provide the two stores as three bash scripts under `scripts/live-vm/` (a shared
`lib.sh` plus `warm-store.sh` and `stage-tcg-images.sh`), mirroring the existing
`scripts/live-stack/` structure and its fail-loud `die`/`require_*` pattern, plus
a persistent warm-store directory owned by B's `live_vm_host` Ansible role and a
documented disk budget. Four decisions:

1. **Provenance — distro stock kernel + on-demand distro debuginfo, matched by
   build-id.** The rootfs is built on a stock distro base via the existing
   `build-fs` plane; the guest boots that distro's own **pinned kernel NVR**; the
   matching `vmlinux` debuginfo is fetched for that exact NVR. The NVR pins which
   build to fetch, but the **match guarantee is the ELF build-id** — the fetched
   debuginfo's `.note.gnu.build-id` must equal the kernel image's, or the refresh
   fails loud and stages nothing. NVR-string equality is not a match proof (a
   distro can rebuild an NVR; a foreign-arch package can carry the same NVR), so
   the build-id is the recorded, checked pin; the manifest stores NVR + build-id +
   rootfs digest.

2. **Two asymmetric stores, asymmetric enforcement.** The warm store is *sized
   for* its content — persistent, on the host's own disk, generous headroom for
   kdump/vmcore + debuginfo — and only **reports** measured usage. The hosted
   `/mnt` set is *under a measured budget* — ephemeral, shared scratch — and its
   staging script gates it two ways: a **pre-stage free-space check** (refuse to
   start if `/mnt` free space is below the ceiling + margin) that *prevents*
   evicting the compose backends, plus a **post-stage footprint cap** that fails
   loud if the staged set grew past the derivation. Enforcement lives where
   blowing the budget evicts other tenants of the disk (the hosted scratch), not
   where it does not (the dedicated host).

3. **NVR-pinned idempotent refresh keeps the store warm, integrity-verified.** A
   manifest records the pinned kernel NVR, the build-id, and the rootfs digest.
   The refresh is a no-op ("warm") only when the manifest NVR equals the target
   **and** the staged rootfs re-hashes to the recorded digest **and** the staged
   debuginfo build-id still matches — presence alone is not warm, because a
   truncated debuginfo or corrupted rootfs is present but broken and would
   persist that break every subsequent nightly. Otherwise it rebuilds, **replaces
   the superseded NVR's artifacts** (so the store holds exactly one set, not an
   accumulating pile of ~1.2 GB debuginfo per past kernel), and rewrites the
   manifest atomically. The refresh holds an exclusive `flock` so a slow nightly
   and a concurrent operator run cannot interleave large-artifact writes. The
   manifest predicate, the build-id compare, and the budget gates are the
   unit-tested seam (`lib.sh`, exercised by subprocess-source behavioral tests,
   the repo's mutation-proven shell-test pattern).

4. **Scope boundary — tooling only; CI wiring and System stand-up are D.** C
   ships the scripts, the persistent dir, the budget doc, and their tests. It
   does **not** edit `.github/workflows` (the nightly matrix and the fail-loud env
   preflight that *invoke* these scripts are sub-issue D) and does **not** stand
   up the provisioned-System (live stack + S3 on the box, or an externally
   provisioned System) — that too is D. C's warm store is the throwaway-domain
   rootfs + kernel + debuginfo only.

## Consequences

Easier:

- The self-hosted nightly reuses a warm rootfs+kernel+debuginfo instead of
  rebuilding and refetching ~1 GB every run; a kernel bump is the only trigger
  for a rebuild.
- The hosted TCG set cannot silently overrun `/mnt` and evict the compose
  backends — the budget gate fails loud with the measured size.
- Kernel/debuginfo agreement is a recorded, checkable pin (the manifest build-id,
  verified on both the refresh and the warm path), not an assumption, so a
  mismatched — or corrupt-but-present — introspection input is caught before it
  reaches `drgn`/`crash`.
- Reuses `build-fs` and the `scripts/live-stack/` idioms; no new build machinery
  and no new lint/test infrastructure.

Harder / new obligations:

- A distro kernel bump changes the pinned NVR, so the matching debuginfo must be
  available in the distro debuginfo repo for that NVR; if the repo lags the
  kernel, the refresh fails loud rather than staging mismatched debuginfo.
- The warm store is persistent state on the runner host that an operator must
  provision (the dir); the refresh itself replaces the superseded NVR's
  artifacts, so the store holds one live set and does not accumulate old
  debuginfo across kernel bumps. The `flock` makes concurrent refreshes safe but
  means a second run blocks on the first rather than racing.
- The measured disk budget is a *derived* ceiling until an operator records the
  first real measurement (the scripts print it); like A/B, the live proof is an
  operator step, not a CI check.

No database migration; test/ops infrastructure only. The CI job that invokes
these scripts (nightly matrix, schedule trigger, fail-loud env preflight) and the
provisioned-System stand-up are sub-issue D, built on the stores this decision
produces.

## Alternatives considered

- **kdive-built kernel + its own `vmlinux` instead of a distro kernel.**
  Rejected for C: it couples every store refresh to a full kernel compile and
  makes the hosted tier's "fetch debuginfo on demand" become "fetch a built
  artifact", for no gain over a pinned distro kernel whose debuginfo the distro
  already publishes. The product's own build→boot→debug path is exercised
  elsewhere; the live tiers need a *stable, matched* kernel, which the distro
  provides most cheaply.
- **One symmetric store for both tiers.** Rejected: the tiers have opposite
  lifetimes (persistent-warm vs ephemeral-budgeted) and opposite failure modes
  (waste vs eviction). Forcing one policy either re-fetches on the warm host
  every night or removes the teeth that stop the hosted set from overrunning
  `/mnt`. The epic design explicitly keeps them separate.
- **Enforce a hard budget on the warm store too.** Rejected: the warm store is
  the dedicated host's own disk, sized for its content; a hard ceiling there adds
  a failure mode (kdump/vmcore headroom exhaustion) without protecting any other
  tenant. Reporting is enough; enforcement belongs on the shared `/mnt` scratch.
- **Pre-bake debuginfo into the hosted image set (ship it staged).** Rejected:
  ~1 GB of debuginfo per arch baked into a persisted artifact bloats the store
  and the transfer for a value the distro repo already serves on demand; fetching
  it at stage time keeps the artifact lean and the budget gate meaningful.
- **A Python orchestrator instead of bash.** Rejected: the work is `du`,
  fail-loud gating, invoking `build-fs`, and a distro debuginfo fetch — the exact
  shape of `scripts/live-stack/lib.sh`, which the epic design points at as the
  fail-loud pattern to mirror. Bash keeps it under the existing `lint-shell`
  guardrail with no new surface.
- **Fold the CI wiring / provisioned-System stand-up into C.** Rejected: the
  epic sequences D after C precisely so the image inputs exist before a job is
  wired onto them; doing both overlaps D's scope and couples image provisioning
  to workflow-YAML churn (the same boundary ADR-0387 drew for B).
