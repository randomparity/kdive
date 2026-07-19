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

1. **Provenance: distro stock kernel + on-demand distro debuginfo.** The rootfs
   is built on a stock distro base; the guest boots that distro's own pinned
   kernel NVR; the matching `vmlinux` debuginfo is fetched from the distro
   debuginfo repo for that exact NVR. "Fetched on demand" maps naturally to the
   distro repo, and the NVR is recorded and asserted to match its debuginfo.
2. **Two asymmetric stores.** The warm store is *sized for* its content
   (persistent, on the host's own disk, generous headroom) and only **reports**
   measured usage. The hosted `/mnt` set is *under a measured budget*
   (ephemeral, shared scratch) and its staging script **enforces** a ceiling —
   fail loud if exceeded.
3. **NVR-pinned idempotent refresh.** A store manifest records the pinned kernel
   NVR (plus rootfs digest). The refresh is warm when the manifest's NVR equals
   the target and the files are present; otherwise it rebuilds. This is the
   "kept warm between runs" mechanism and the unit-tested seam.

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
  otherwise print the measured usage. The `/mnt` gate with teeth. The boundary
  (measured == ceiling passes; measured == ceiling + 1 fails) is asserted and
  mutation-checked.
- `store_manifest_matches MANIFEST TARGET_NVR` — read the recorded kernel NVR
  from `MANIFEST`; return 0 (warm) iff it equals `TARGET_NVR`, else 1
  (stale/absent). Absent manifest is stale, not an error.
- `write_manifest MANIFEST NVR ROOTFS_SHA256` — record the pinned inputs
  (`kernel_nvr`, `rootfs_sha256`) atomically (write-temp-then-rename).

### `scripts/live-vm/warm-store.sh` — self-hosted warm store refresh

Idempotent. `KDIVE_WARM_STORE_DIR` (default `/var/lib/kdive/warm-store`, the dir
B's Ansible owns).

1. Resolve the target kernel NVR from the distro base.
2. If `store_manifest_matches` and the rootfs + kernel + debuginfo files are all
   present → **warm**: `report_usage`, emit the `KDIVE_LIVE_VM_ROOTFS=…` wiring
   line to stdout, exit 0.
3. Else **refresh**: build the rootfs via `build-fs` (host-only, libguestfs),
   record the kernel image + its NVR, fetch the matching `vmlinux` debuginfo for
   that exact NVR from the distro debuginfo repo, `write_manifest`, then
   `report_usage` and emit the wiring line.

Stdout is the eval-safe wiring line only (like `build-fs`); the human summary
goes to stderr.

### `scripts/live-vm/stage-tcg-images.sh` — hosted TCG image set on `/mnt`

`KDIVE_TCG_STAGE_DIR` (default `/mnt/kdive-tcg`), `KDIVE_TCG_BUDGET_BYTES`
(default = the documented ceiling).

1. Stage the ppc64le rootfs + kernel into the stage dir.
2. Fetch the matching ppc64le `vmlinux` debuginfo **on demand** (not pre-baked).
3. `enforce_budget STAGE_DIR CEILING "hosted TCG image set"` — fail the job loud
   if the staged set exceeds the ceiling.
4. `report_usage`, emit the stage-dir wiring line to stdout.

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
CI-cannot-prove-it-live posture A and B shipped). The ceiling is a hard gate, so
if real artifacts exceed the derivation the job fails loud with the measured
number rather than silently evicting the backends.

## Testing

- `tests/scripts/test_live_vm_stores.py` — subprocess-source `lib.sh`:
  - `enforce_budget`: passes under ceiling; fails loud (non-zero, message names
    the measured size + ceiling) over ceiling; boundary at exactly the ceiling.
  - `report_usage`: stable, greppable format.
  - `store_manifest_matches`: warm (NVR equal), stale (NVR differ), absent
    manifest (stale, not error).
  - `write_manifest`: round-trips through `store_manifest_matches`; atomic
    (no partial manifest on interrupted write).
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
