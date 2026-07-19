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

1. **Provenance — distro stock kernel; debuginfo fetched *by build-id* via
   `debuginfod`.** The rootfs is built on a stock distro base (the base image is
   the pin) via the existing `build-fs` plane; the guest boots that distro's own
   kernel. The kernel's ELF build-id is **extracted from the staged kernel
   artifact** (a bare `vmlinux` ELF — common for ppc64le `pseries` — is read
   directly; a compressed `bzImage`/`vmlinuz` is first `extract-vmlinux`'d; then
   `eu-readelf -n`), and the matching debuginfo is fetched by that build-id
   (`debuginfod-find`). An empty or unparseable extraction is **fatal**, and the
   match compare rejects an empty id even against another empty id, so a failed
   extraction can never pass the guard vacuously. Fetching by build-id makes the
   debuginfo match the booted
   kernel *by construction* (`debuginfod` returns the debuginfo for exactly that
   id or nothing) and is arch-neutral, so a ppc64le kernel's debuginfo is fetched
   from an x86_64 host by the same mechanism. The kernel NVR is a recorded label,
   **not** the match key — NVR-string equality was rejected as a match proof (a
   distro can rebuild an NVR; a foreign-arch package can carry the same NVR). The
   manifest stores NVR, build-id, and a `sha256` for each of the rootfs, kernel
   image, and debuginfo; a post-fetch `build_ids_match` is a belt-and-suspenders
   assertion.

2. **Two asymmetric stores, asymmetric enforcement.** The warm store is *sized
   for* its content — persistent, on the host's own disk, generous headroom for
   kdump/vmcore + debuginfo — and only **reports** measured usage. The hosted
   `/mnt` set is *under a measured budget* — ephemeral, shared scratch — and its
   staging script gates it two ways at two times: a **pre-stage free-space
   pre-check** for the *whole* budget (staged set + run-time vmcore headroom +
   margin) before any write, plus a **post-stage footprint cap** on the *staged
   set only*, which fails loud if it grew past its derivation. The pre-check is a
   best-effort `df` read, **not a reservation** — on a `/mnt` shared with the
   compose backends and `$GITHUB_WORKSPACE` it catches "already too full to start"
   but does not hold space against a co-tenant that grows afterward; the run-time
   vmcore is captured later and a hard reservation (a `fallocate` placeholder the
   capture consumes) belongs to sub-issue D, which owns that capture. The vmcore
   headroom assumes a ≤~2 GB-RAM guest; D must keep guest RAM within it or raise
   `KDIVE_TCG_BUDGET_BYTES`. Enforcement lives where blowing the budget can evict
   other tenants of the disk (the hosted scratch), not where it does not (the
   dedicated host).

3. **Idempotent refresh keeps the store warm, integrity-verified, temp-then-swap.**
   The manifest records the NVR label, the build-id, and a `sha256` for each of
   the rootfs, kernel image, and debuginfo. The refresh is a no-op ("warm") only
   when the build-id matches **and all three staged files re-hash to their
   recorded digests** — build-id and presence alone are not warm, because a
   debuginfo truncated after its `.note.gnu.build-id` (near the ELF header, before
   the large DWARF sections) keeps a valid build-id but has lost its symbols; only
   a content digest catches that, and the break would otherwise persist every
   nightly. Otherwise the refresh builds into a fresh `mktemp -d` set dir (a
   same-filesystem sibling, asserted), verifies build-id + digests there, and
   **commits by a single atomic rename of a `current` symlink** onto the new set
   dir — the symlink flip, not a directory rename, is the commit point, because a
   directory rename cannot atomically replace a *populated* destination (the
   same-NVR corruption-rebuild case) whereas the pointer swap is one atomic op
   regardless. After the flip it prunes set dirs no longer pointed at, so the store
   holds one live set rather than accumulating ~1.2 GB of debuginfo per past
   kernel; a crash mid-prune leaves an orphan the sweep reclaims, never a torn live
   set. `build-fs`'s own eval-safe stdout is captured (not passed through) so the
   store emits a single authoritative three-var wiring block through `current/…`,
   never a duplicate or a pre-commit build path. A cleanup `trap` and an entry
   sweep reclaim a crashed refresh's orphan dirs so the report-only store does not
   leak. Freshness is keyed on the NVR
   label (the base image is the pin); a same-NVR distro rebuild is not
   auto-detected — the store serves its self-consistent old set until an operator
   forces a refresh, the intentional boundary (not a correctness hazard: the
   staged set is internally matched either way). The refresh holds an exclusive
   `flock`; the consuming boot (sub-issue D) takes a
   *shared* lock on the same file so a refresh cannot swap artifacts out from
   under an in-flight domain. The manifest predicate, the build-id/digest checks,
   and the budget gates are the unit-tested seam (`lib.sh`, subprocess-source
   behavioral tests, the repo's mutation-proven shell-test pattern).

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
- The hosted TCG set is bounded on `/mnt`: a pre-stage `df` pre-check refuses to
  start when the disk is already too full, and a post-stage cap fails loud with
  the measured size if the staged set grew past its derivation (the pre-check is
  best-effort on shared scratch, not a hard reservation — see Decision 2).
- Kernel/debuginfo agreement is a recorded, checkable pin (the manifest build-id,
  verified on both the refresh and the warm path), not an assumption, so a
  mismatched — or corrupt-but-present — introspection input is caught before it
  reaches `drgn`/`crash`.
- Reuses `build-fs` and the `scripts/live-stack/` idioms; no new build machinery
  and no new lint/test infrastructure.

Harder / new obligations:

- The `debuginfod` path is a runtime dependency: `DEBUGINFOD_URLS` must point at a
  server that indexes the distro's kernel debuginfo, and `DEBUGINFOD_CACHE_PATH`
  must be pinned onto the budgeted filesystem (`debuginfod-find` caches the
  ~1.2 GB download under `$HOME` by default — the small root fs on the hosted
  runner — an `ENOSPC` no budget gate would otherwise see). A kernel bump can
  outrun the debuginfod index, so the fetch distinguishes three fail-loud outcomes
  — infra not configured, debuginfo-not-yet-published (index lag), and transient
  error — rather than staging mismatched or missing debuginfo.
- The warm store is persistent state on the runner host that an operator must
  provision (the dir); the refresh replaces the superseded NVR's artifacts and a
  cleanup trap/entry-sweep reclaims a crashed refresh's temp set, so the
  report-only store holds one live set and does not accumulate old debuginfo or
  leaked temp sets. The `flock` makes concurrent refreshes safe but means a second
  run blocks on the first rather than racing. A same-NVR distro rebuild is not
  auto-detected; an operator forces the refresh (`KDIVE_WARM_STORE_FORCE=1`).
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
- **Fetch debuginfo by NVR from the distro debuginfo repo (not `debuginfod` by
  build-id).** Rejected: keying the fetch on the NVR string is the match weakness
  this ADR set out to avoid, and it forces the hosted x86_64 runner to configure a
  foreign-arch (ppc64le) debuginfo repo. Fetching by the build-id extracted from
  the staged kernel makes the match hold by construction and is arch-neutral, so
  the same command works cross-arch with only `DEBUGINFOD_URLS` as a prerequisite.
- **Verify the kernel↔debuginfo match by NVR string equality alone.** Rejected: an
  NVR can be rebuilt and a foreign-arch package can carry the same NVR, so string
  equality does not prove the debuginfo describes the booted binary. The build-id
  extracted from the staged artifact is the real identity; a content `sha256` is
  what proves completeness (a truncated debuginfo keeps its build-id but loses its
  DWARF), so the manifest records both and the warm path re-checks both.
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
