# Guest-image + debuginfo provisioning (epic #1289, sub-issue C)

- **Date:** 2026-07-19
- **Status:** Draft
- **Issue:** [#1292](https://github.com/randomparity/kdive/issues/1292)
- **Epic:** [#1289](https://github.com/randomparity/kdive/issues/1289) · epic spec
  [`docs/design/2026-07-18-live-test-framework.md`](../../design/2026-07-18-live-test-framework.md)
- **ADR:** [0388 — guest-image + debuginfo provisioning](../../adr/0388-guest-image-debuginfo-provisioning.md)
  (this sub-issue implements it)
- **Depends on:** sub-issue A ([#1290](https://github.com/randomparity/kdive/issues/1290),
  merged) — the `live_vm` environment contract these stores satisfy;
  sub-issue B ([#1291](https://github.com/randomparity/kdive/issues/1291),
  merged) — the runner host whose Ansible role owns the persistent warm-store dir.

## Problem

The live tiers boot a real kernel and then introspect it. That needs three
staged artifacts kept in agreement: a bootable **rootfs**, the **kernel** the
domain direct-boots, and the **`vmlinux` debuginfo** that matches that exact
kernel. A debuginfo/kernel mismatch does not error — `drgn`/`crash` silently
resolve wrong symbols, so a "passing" introspection proof can be meaningless.

Two tiers consume these artifacts with different lifetimes, and the epic design
is explicit that they are **separate stores**:

- The **self-hosted native-KVM** tier reboots the same throwaway rootfs nightly
  on a persistent host. Rebuilding rootfs + refetching ~1 GB of debuginfo every
  night is waste; the artifacts should stay **warm** (persistent, reused) and be
  refreshed only when the pinned kernel changes.
- The **hosted `ubuntu-latest` TCG** tier is ephemeral: a fresh runner every
  run, no persistent disk, but a large (~70 GB) `/mnt` scratch shared with the
  compose backends and the workspace. Its ppc64le image set is staged on `/mnt`
  per run and its debuginfo is fetched **on demand**, so the total must stay
  **under a measured, enforced disk budget** or it can evict the backing
  services mid-run.

Today neither store exists as tooling. There is a rootfs *builder* (`build-fs`
/ `LocalLibvirtRootfsBuildPlane`, ADR-0092/0345) but no orchestration that
produces the kernel+debuginfo pairing, keeps the self-hosted store warm, or
bounds the hosted store's disk use.

## Goals

1. A self-hosted **warm store**: an idempotent, NVR-pinned refresh that ensures a
   bootable rootfs + its kernel + matching `vmlinux` debuginfo are present and
   reused across nightly runs, rebuilding only when the pinned kernel changes.
2. A hosted **TCG image set** staged on `/mnt`: ppc64le rootfs + kernel with
   debuginfo fetched on demand, kept **under an enforced disk budget** whose
   derivation and measured actual are documented.
3. Matching debuginfo per guest kernel, fetched **by the kernel's ELF build-id**
   (extracted from the staged kernel) via `debuginfod`, so the debuginfo matches
   the booted kernel by construction (decided in ADR-0388).

## Non-goals

- **No CI workflow wiring.** `.github/workflows` edits (the nightly matrix, the
  fail-loud env preflight that *invokes* these scripts) are sub-issue D. C
  produces the scripts, the persistent dir, and the budget doc; D consumes them.
- **No provisioned-System stand-up.** Standing up the live stack + S3 on the
  self-hosted box (or wiring an externally provisioned System) is sub-issue D.
  C's warm store is the throwaway-domain rootfs + kernel + debuginfo only.
- **No new image-build machinery.** The rootfs is produced by the existing
  `build-fs` plane; C orchestrates it, it does not replace it.
- **No database migration.** Test/ops infrastructure only.

## Decisions (see ADR-0388)

1. **Provenance: distro stock kernel; debuginfo fetched *by build-id* via
   `debuginfod`.** The rootfs is built on a stock distro base (the base image is
   the pin); the guest boots that distro's own kernel. The kernel's ELF build-id
   is **extracted from the staged kernel artifact** (decompress the
   `bzImage`/`vmlinuz` to the `vmlinux` ELF via `extract-vmlinux`, then
   `eu-readelf -n`), and the matching debuginfo is fetched by that build-id
   (`debuginfod-find debuginfo <build-id>`). Fetching by build-id means the
   debuginfo matches the booted kernel *by construction* — `debuginfod` returns
   the debuginfo for exactly that build-id or nothing — and it is arch-neutral, so
   the same mechanism fetches a ppc64le kernel's debuginfo from an x86_64 host.
   The kernel NVR is a recorded, human-readable label (which base/kernel), **not**
   the match key; NVR-string equality was rejected as a match proof (a distro can
   rebuild an NVR; a foreign-arch package can carry the same NVR). A post-fetch
   `build_ids_match` is a belt-and-suspenders assertion.
2. **Two asymmetric stores.** The warm store is *sized for* its content
   (persistent, on the host's own disk, generous headroom) and only **reports**
   measured usage. The hosted `/mnt` set is *under a measured budget* (ephemeral,
   shared scratch) and gated twice: a **pre-stage `require_free_space`** that
   reserves the *whole* budget (staged set + run-time vmcore headroom + margin)
   before any write, preventing co-tenant eviction, and a **post-stage
   `enforce_budget`** that caps only the *staged set's* footprint. The staging
   gate bounds what C stages; the run-time vmcore is captured later and lives
   inside the space the pre-stage reservation already secured (its runtime
   capture is sub-issue D's boot concern).
3. **Idempotent refresh, integrity-verified, temp-then-swap.** A store manifest
   records the kernel NVR (label), the build-id, and a `sha256` for each of the
   rootfs, kernel image, and debuginfo. The refresh is warm only when the build-id
   matches **and** all three staged files re-hash to their recorded digests —
   presence and build-id alone are not warm, because a debuginfo truncated after
   its `.note.gnu.build-id` (which sits near the ELF header, before the large
   DWARF sections) keeps a valid build-id but has lost its symbols; only a content
   digest catches that. Otherwise the refresh builds into a **temp location**,
   verifies build-ids + digests there, **atomically swaps** it into place, then
   removes only artifact sets whose NVR *differs* from the one just written (so a
   same-NVR corruption rebuild cannot delete its own fresh output). The refresh
   holds an exclusive `flock` so a slow nightly and a concurrent operator run
   cannot interleave writes. The manifest predicate, the build-id/digest checks,
   and the budget gates are the unit-tested seam.

## Architecture

Three bash scripts under `scripts/live-vm/`, mirroring `scripts/live-stack/`
(shared `lib.sh` + a fail-loud `die`, the `require_free_http_port` pattern the
epic design points at). `just lint-shell` (`shfmt -f scripts | shellcheck`) picks
them up automatically.

### `scripts/live-vm/lib.sh` — the shared, unit-tested seam

Pure-ish helpers, source-able and callable in a subprocess (the repo's
mutation-proven shell-test pattern, e.g. `tests/scripts/test_setup_local_libvirt.py`):

- `die MSG` — print to stderr, exit non-zero (fail loud).
- `du_bytes PATH` — measured apparent size in bytes (`du -sb`).
- `report_usage LABEL PATH` — print a stable, greppable measured-usage line.
- `enforce_budget PATH CEILING_BYTES WHAT` — measure `PATH`; `die` with an
  actionable message naming `WHAT`, the measured size, and the ceiling if over;
  otherwise print the measured usage. The post-stage footprint cap. The boundary
  (measured == ceiling passes; measured == ceiling + 1 fails) is asserted and
  mutation-checked.
- `require_free_space PATH NEEDED_BYTES WHAT` — `df -B1` the filesystem holding
  `PATH`; `die` if free space is below `NEEDED_BYTES` **before** any large write.
  The eviction guard for the shared `/mnt` scratch: it prevents an overrun rather
  than detecting one after the disk is already full. `NEEDED_BYTES` is the *whole*
  budget (staged set + vmcore headroom + margin), so the run-time vmcore has
  reserved room even though it is captured after staging.
- `kernel_build_id KERNEL_IMAGE` — decompress the `bzImage`/`vmlinuz` to a
  `vmlinux` ELF (`extract-vmlinux`) and read its `.note.gnu.build-id`
  (`eu-readelf -n`). Reads the id from the **actual staged artifact**, not repo
  metadata, so a corrupted/swapped image is caught. Host-only; the string it
  returns is the testable input.
- `build_ids_match A B` — return 0 iff two build-id strings are equal; `die` with
  both ids on mismatch. The post-fetch sanity assertion; the string compare is the
  testable seam.
- `sha256_of FILE` / `verify_sha256 FILE EXPECTED` — content digest and its
  fail-loud re-check, used symmetrically for rootfs, kernel image, and debuginfo
  (build-id proves identity, not completeness — only a digest catches truncation).
- `store_manifest_matches MANIFEST TARGET_NVR` — read the recorded NVR label from
  `MANIFEST`; return 0 iff it equals `TARGET_NVR`, else 1 (stale/absent). Absent
  manifest is stale, not an error. Necessary, not sufficient — warmth also
  requires the digest + build-id re-checks below.
- `manifest_field MANIFEST KEY` — read a recorded field (`kernel_nvr`,
  `build_id`, `rootfs_sha256`, `kernel_sha256`, `debuginfo_sha256`) for the
  warm-path re-verification.
- `write_manifest MANIFEST NVR BUILD_ID ROOTFS_SHA256 KERNEL_SHA256 DEBUGINFO_SHA256`
  — record the inputs atomically (write-temp-then-rename).

### `scripts/live-vm/warm-store.sh` — self-hosted warm store refresh

Idempotent, holds an exclusive `flock` on the store for the whole refresh.
`KDIVE_WARM_STORE_DIR` (default `/var/lib/kdive/warm-store`, the dir B's Ansible
owns).

1. Resolve the target kernel NVR label from the distro base. If resolution fails
   (distro metadata unreachable), `die` — do not silently serve a stale set as if
   current.
2. **Warm check** — declare warm only when `store_manifest_matches` **and** all
   three staged files (`rootfs`, kernel image, debuginfo) re-hash to their
   recorded digests **and** the staged debuginfo's build-id equals the manifest
   `build_id`. Any failure → treat as stale (rebuild), not warm. On warm:
   `report_usage`, emit the full wiring block, exit 0.
3. **Refresh** (into a temp dir, then atomic swap) — build the rootfs via
   `build-fs` (host-only, libguestfs); `kernel_build_id` the staged kernel; fetch
   the matching debuginfo by that build-id (`debuginfod-find`); `build_ids_match`
   the fetched debuginfo against the kernel (fail loud on mismatch — never stage
   mismatched debuginfo); digest all three files; **atomically swap** the temp set
   into place; `write_manifest`; **remove only sets whose NVR differs** from the
   one just written; `report_usage`; emit the wiring block.

**Wiring block** (both paths) — the store produces three matched artifacts, so it
emits all three env vars the `live_vm` consumers read (`external_env.py`), not
just the rootfs, as eval-safe stdout lines:

- `KDIVE_LIVE_VM_ROOTFS=<rootfs.qcow2>`
- `KDIVE_LIVE_VM_BZIMAGE=<kernel image>`
- `KDIVE_LIVE_VM_VMLINUX=<vmlinux debuginfo>` (the same path also satisfies
  `KDIVE_LIVE_VM_GDBMI_VMLINUX`, and pairs with a run-captured
  `KDIVE_LIVE_VM_VMCORE`).

Stdout is the eval-safe wiring block only (like `build-fs`); the human summary
goes to stderr.

### `scripts/live-vm/stage-tcg-images.sh` — hosted TCG image set on `/mnt`

Runs on the hosted `ubuntu-latest` (**x86_64**) runner but stages a **ppc64le**
set, so debuginfo is fetched cross-arch — which the build-id `debuginfod` path
handles arch-neutrally. `KDIVE_TCG_STAGE_DIR` (default `/mnt/kdive-tcg`),
`KDIVE_TCG_BUDGET_BYTES` (default = the documented ceiling). A cleanup `trap`
removes a partially-staged dir on any failure, so a failed run never leaves a
half-populated `/mnt` that the next run reads as complete.

1. `require_free_space /mnt <whole budget + margin> "hosted TCG image set"`
   **before** any fetch — the eviction guard: refuse to start if `/mnt` lacks room
   for the staged set *and* the run-time vmcore headroom, rather than filling it
   and evicting the compose backends mid-stage.
2. Stage the ppc64le rootfs + kernel into the stage dir; `kernel_build_id` the
   staged ppc64le kernel (`extract-vmlinux` decompresses regardless of host arch).
3. Fetch the matching debuginfo by that build-id **on demand** (not pre-baked) via
   `debuginfod-find debuginfo <build-id>` — the committed mechanism; its
   prerequisite is `DEBUGINFOD_URLS` set to a server that indexes the distro's
   ppc64le kernel debuginfo (the distro debuginfod, e.g.
   `debuginfod.ubuntu.com`/`debuginfod.debian.net`/`debuginfod.fedoraproject.org`).
   Three **distinct** fail-loud outcomes so an operator can tell them apart:
   `DEBUGINFOD_URLS` unset/unreachable → **fetch infra not configured**; a clean
   *not-found* for a valid build-id → **debuginfo not yet published** (distro lag);
   a network/5xx error → **transient** (bounded retry). Then `build_ids_match` the
   fetched debuginfo against the staged kernel. None stages mismatched/missing
   debuginfo.
4. `enforce_budget STAGE_DIR CEILING "hosted TCG image set"` — post-stage
   footprint cap on the *staged set only*; fail loud if it exceeds the ceiling.
5. `report_usage`, emit the same three-var wiring block (`KDIVE_LIVE_VM_ROOTFS`,
   `KDIVE_LIVE_VM_BZIMAGE`, `KDIVE_LIVE_VM_VMLINUX`) to stdout.

### Consumer boundary (sub-issue D)

The refresh's exclusive `flock` serializes concurrent *refreshes*, but a refresh
that swaps the rootfs while a domain booted from it is still live is a
refresh-vs-consume TOCTOU. That boundary belongs to the consumer: **sub-issue D's
boot must take a shared lock on the same store lockfile** for the life of the
domain, so a refresh cannot swap artifacts out from under an in-flight boot. C
provides the lockfile and documents the contract; D honors it.

### Ansible — the persistent warm-store dir (the B-role delta)

`deploy/ansible/inventory/group_vars/live_vm_runners.yml` gains
`warm_store_dir: /var/lib/kdive/warm-store`, appended to the existing
`live_vm_staging_dirs` loop so `live_vm_host` creates+owns it (persistent across
jobs, runner-owned, world-traversable, AppArmor-dynamic — no static label).
Additive; no new role or task block beyond the loop entry.

## Disk budget

The **enforced** budget is the hosted `/mnt` ceiling (the warm store reports but
does not fail — it is the host's own disk). Derivation for the ceiling:

| Component | Derived size | Bounded by |
| --- | --- | --- |
| ppc64le rootfs qcow2 | ~2 GB | staged set (`enforce_budget`) |
| distro kernel (image + initramfs) | ~0.1 GB | staged set (`enforce_budget`) |
| matching `vmlinux` debuginfo | ~1.2 GB | staged set (`enforce_budget`) |
| **staged-set ceiling** | **~3.5 GB** | `enforce_budget` (post-stage cap) |
| kdump/vmcore + working headroom | ~2 GB | reserved up front, captured at run time |
| **whole budget reserved** | **~6 GB** (well under `/mnt`'s ~70 GB) | `require_free_space` (pre-stage) |

These are *derived* sizes. The scripts print the **measured actual**; the runbook
records the first real measurement from an operator run (the same
CI-cannot-prove-it-live posture A and B shipped).

The two gates bound **different things at different times**, so the derivation and
the enforcement agree on what each covers. `require_free_space` reserves the
*whole* ~6 GB (staged set + vmcore headroom + margin) *before* any write — the
"don't evict co-tenants" guarantee, and it is what secures room for the run-time
vmcore that is captured *after* staging (that capture is sub-issue D's boot
concern, into the space this reservation already holds). `enforce_budget` caps
only the *staged set's* ~3.5 GB footprint *after* staging — catching an artifact
set that grew past its derivation. Both fail loud with the measured number.

## Testing

- `tests/scripts/test_live_vm_stores.py` — subprocess-source `lib.sh`:
  - `enforce_budget`: passes under ceiling; fails loud (non-zero, message names
    the measured size + ceiling) over ceiling; boundary at exactly the ceiling.
  - `require_free_space`: passes when free ≥ needed; fails loud when free <
    needed (stubbed `df`, so the test is deterministic and host-independent).
  - `build_ids_match`: returns 0 on equal ids; fails loud naming both on
    mismatch — the post-fetch match assertion.
  - `verify_sha256`: passes on a matching digest; fails loud on a byte-changed or
    truncated file — the completeness check build-id cannot give.
  - `report_usage`: stable, greppable format.
  - `store_manifest_matches` / `manifest_field`: warm (NVR equal), stale (NVR
    differ), absent manifest (stale, not error); round-trip of every field
    (`build_id`, `rootfs_sha256`, `kernel_sha256`, `debuginfo_sha256`).
  - `write_manifest`: round-trips through `manifest_field`; atomic (no partial
    manifest on interrupted write).
  - **Warm-path integrity (the corrupt-but-present regression)**: manifest NVR +
    build-id match, but the staged **debuginfo is truncated past its build-id
    note** (digest differs) → the warm predicate rejects (rebuild), does not
    report warm. Same for a byte-changed rootfs or kernel image. This is the case
    build-id alone misses.
  - **Same-NVR rebuild removal**: a corruption-triggered rebuild with an unchanged
    NVR removes only differing-NVR sets and never deletes its own freshly-swapped
    output (a `write_manifest`-then-prune sequence over a stubbed store layout).
- The Ansible dir change is covered by `live_vm_host`'s existing verify gate and
  idempotence (`test-ansible`); no new role test needed for one loop entry.
- The heavy operations (real `build-fs`, real debuginfo download, real boot) are
  host-only and land as the operator live-proof, not a CI check — a clean skip in
  CI is correct.
- Guardrails: `just lint-shell lint-ansible test-ansible test`.

## Rollout

Independent of D and E (it produces inputs they consume). Ships as: `lib.sh` +
the two store scripts + their tests, the Ansible dir entry, ADR-0388, the
runbook budget section, this spec. The live proof (a real warm-store refresh and
a real `/mnt` stage under budget) is the operator step, recorded in the runbook.
