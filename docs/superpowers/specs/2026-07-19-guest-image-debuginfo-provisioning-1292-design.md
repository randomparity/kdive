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
3. Matching debuginfo per guest kernel, sourced from the distro debuginfo repo
   for the exact pinned kernel NVR (decided in ADR-0388).

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

1. **Provenance: distro stock kernel + on-demand distro debuginfo, matched by
   build-id.** The rootfs is built on a stock distro base; the guest boots that
   distro's own pinned kernel NVR; the matching `vmlinux` debuginfo is fetched
   for that exact NVR. The NVR pins *which* build to fetch, but the **match
   guarantee is the ELF build-id**, not the NVR string: the fetched debuginfo's
   `.note.gnu.build-id` must equal the kernel image's build-id, or the refresh
   fails loud. NVR-string equality is not a match proof (a distro can rebuild the
   same NVR; a foreign-arch package can carry the same NVR), so the build-id is
   the recorded, checked pin — the manifest stores both.
2. **Two asymmetric stores.** The warm store is *sized for* its content
   (persistent, on the host's own disk, generous headroom) and only **reports**
   measured usage. The hosted `/mnt` set is *under a measured budget*
   (ephemeral, shared scratch) and its staging script **enforces** a ceiling —
   fail loud if exceeded.
3. **NVR-pinned idempotent refresh, integrity-verified.** A store manifest
   records the pinned kernel NVR, the kernel/debuginfo build-id, and the rootfs
   `sha256`. The refresh is warm only when the manifest NVR equals the target
   **and** the staged rootfs re-hashes to the recorded digest **and** the staged
   debuginfo's build-id still matches — presence alone is not warm, because a
   truncated debuginfo or a corrupted rootfs is "present" but hands `drgn`/`crash`
   a broken `vmlinux`, reproducing the exact silent-mismatch this feature exists
   to prevent, persistently. Otherwise it rebuilds and **replaces** the prior
   NVR's artifacts (the store holds one set). The refresh holds an exclusive
   `flock` so a slow nightly and a concurrent operator run cannot interleave
   large-artifact writes. The manifest predicate + build-id compare + budget gate
   are the unit-tested seam.

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
  This is the actual eviction guard for the shared `/mnt` scratch: it prevents an
  overrun rather than detecting one after the disk is already full.
- `build_ids_match A B` — return 0 iff two build-id strings are equal; `die` with
  both ids on mismatch. Extraction of each id (`eu-readelf -n` on the debuginfo
  `vmlinux`; the distro `.build-id` farm / package metadata for the kernel image)
  is host-only; the string compare is the testable seam.
- `store_manifest_matches MANIFEST TARGET_NVR` — read the recorded kernel NVR
  from `MANIFEST`; return 0 iff it equals `TARGET_NVR`, else 1 (stale/absent).
  Absent manifest is stale, not an error. (Warmth also requires the integrity
  re-checks below; NVR match is necessary, not sufficient.)
- `manifest_field MANIFEST KEY` — read a recorded field (`kernel_nvr`,
  `build_id`, `rootfs_sha256`) for the warm-path re-verification.
- `write_manifest MANIFEST NVR BUILD_ID ROOTFS_SHA256` — record the pinned inputs
  atomically (write-temp-then-rename).

### `scripts/live-vm/warm-store.sh` — self-hosted warm store refresh

Idempotent, holds an exclusive `flock` on the store for the whole refresh.
`KDIVE_WARM_STORE_DIR` (default `/var/lib/kdive/warm-store`, the dir B's Ansible
owns).

1. Resolve the target kernel NVR from the distro base.
2. **Warm check** — declare warm only when `store_manifest_matches` **and** the
   staged rootfs re-hashes to the manifest `rootfs_sha256` **and** the staged
   debuginfo's build-id equals the manifest `build_id` and matches the staged
   kernel image (`build_ids_match`). Any failure → treat as stale (rebuild), not
   warm. On warm: `report_usage`, emit the full wiring block, exit 0.
3. **Refresh** — build the rootfs via `build-fs` (host-only, libguestfs); record
   the kernel image and its NVR; fetch the matching `vmlinux` debuginfo for that
   exact NVR; `build_ids_match` the debuginfo against the kernel image (fail loud
   on mismatch — never stage mismatched debuginfo); **remove the superseded NVR's
   artifacts** so the store holds exactly one set; `write_manifest` (NVR,
   build-id, rootfs sha256); `report_usage`; emit the wiring block.

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
set, so debuginfo is fetched cross-arch. `KDIVE_TCG_STAGE_DIR` (default
`/mnt/kdive-tcg`), `KDIVE_TCG_BUDGET_BYTES` (default = the documented ceiling). A
cleanup `trap` removes a partially-staged dir on any failure, so a failed run
never leaves a half-populated `/mnt` that the next run reads as complete.

1. `require_free_space /mnt <ceiling + margin> "hosted TCG image set"` **before**
   any fetch — the eviction guard: refuse to start if `/mnt` lacks room, rather
   than filling it and evicting the compose backends mid-stage.
2. Stage the ppc64le rootfs + kernel into the stage dir.
3. Fetch the matching ppc64le `vmlinux` debuginfo **on demand** (not pre-baked),
   **arch-targeted** at the ppc64le distro debuginfo repo (or `debuginfod` by
   build-id, which is arch-neutral) — not the x86_64 host's native repo — then
   `build_ids_match` it against the staged kernel. A fetch that returns *no
   package for this exact NVR/build-id* is a **repo-lag failure** (fail loud,
   distinct message: the distro has not published debuginfo for the pinned
   kernel); a network/5xx error is **transient** (bounded retry, distinct
   message). Neither stages mismatched or missing debuginfo.
4. `enforce_budget STAGE_DIR CEILING "hosted TCG image set"` — post-stage
   footprint cap; fail loud if the staged set exceeds the ceiling.
5. `report_usage`, emit the same three-var wiring block (`KDIVE_LIVE_VM_ROOTFS`,
   `KDIVE_LIVE_VM_BZIMAGE`, `KDIVE_LIVE_VM_VMLINUX`) to stdout.

### Ansible — the persistent warm-store dir (the B-role delta)

`deploy/ansible/inventory/group_vars/live_vm_runners.yml` gains
`warm_store_dir: /var/lib/kdive/warm-store`, appended to the existing
`live_vm_staging_dirs` loop so `live_vm_host` creates+owns it (persistent across
jobs, runner-owned, world-traversable, AppArmor-dynamic — no static label).
Additive; no new role or task block beyond the loop entry.

## Disk budget

The **enforced** budget is the hosted `/mnt` ceiling (the warm store reports but
does not fail — it is the host's own disk). Derivation for the ceiling:

| Component | Derived size |
| --- | --- |
| ppc64le rootfs qcow2 | ~2 GB |
| distro kernel (`vmlinux`/image + initramfs) | ~0.1 GB |
| matching `vmlinux` debuginfo | ~1.2 GB |
| kdump/vmcore + working headroom | ~2 GB |
| **enforced ceiling** | **~6 GB** (well under `/mnt`'s ~70 GB; leaves room for compose backends + `$GITHUB_WORKSPACE`) |

These are *derived* sizes. The scripts print the **measured actual**; the runbook
records the first real measurement from an operator run (the same
CI-cannot-prove-it-live posture A and B shipped).

Two gates protect the shared `/mnt`, addressing distinct failure modes: a
**pre-stage `require_free_space`** (free space on `/mnt` ≥ ceiling + margin
*before* any fetch) prevents mid-run eviction of the compose backends, and the
**post-stage `enforce_budget`** caps the staged set's own footprint. The former is
the real "don't evict co-tenants" guarantee; the latter catches an artifact set
that grew past the derivation. Both fail loud with the measured number.

## Testing

- `tests/scripts/test_live_vm_stores.py` — subprocess-source `lib.sh`:
  - `enforce_budget`: passes under ceiling; fails loud (non-zero, message names
    the measured size + ceiling) over ceiling; boundary at exactly the ceiling.
  - `require_free_space`: passes when free ≥ needed; fails loud when free <
    needed (stubbed `df`, so the test is deterministic and host-independent).
  - `build_ids_match`: returns 0 on equal ids; fails loud naming both on
    mismatch — the concrete kernel↔debuginfo match assertion.
  - `report_usage`: stable, greppable format.
  - `store_manifest_matches` / `manifest_field`: warm (NVR equal), stale (NVR
    differ), absent manifest (stale, not error); field round-trip.
  - `write_manifest`: round-trips through `store_manifest_matches`; atomic
    (no partial manifest on interrupted write).
  - **Warm-path integrity**: manifest NVR matches but the staged rootfs digest or
    debuginfo build-id no longer matches → the warm predicate rejects (rebuild),
    does not report warm — the corrupt-but-present regression.
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
