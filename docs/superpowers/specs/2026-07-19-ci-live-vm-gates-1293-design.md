# CI wiring: hosted TCG gate + self-hosted live_vm gate (epic #1289, sub-issue D)

- **Date:** 2026-07-19
- **Status:** Draft
- **Issue:** [#1293](https://github.com/randomparity/kdive/issues/1293)
- **Epic:** [#1289](https://github.com/randomparity/kdive/issues/1289) · epic spec
  [`docs/design/2026-07-18-live-test-framework.md`](../../design/2026-07-18-live-test-framework.md)
- **ADR:** [0389 — CI wiring for the live-test tiers](../../adr/0389-ci-live-vm-gates.md)
  (this sub-issue implements ADR-0386's sub-issue-D obligations and records the two
  choices the epic spec deferred to D)
- **Depends on (all merged):**
  - A ([#1290](https://github.com/randomparity/kdive/issues/1290)) — the `live_vm`
    environment contract + `require_live_vm_*` gates + the additive
    `live_vm_throwaway` / `live_vm_provisioned` sub-markers this preflight keys on.
  - B ([#1291](https://github.com/randomparity/kdive/issues/1291), ADR-0387) — the
    self-hosted Ubuntu 26.04 KVM runner: the persistent `/opt/kdive` venv
    (`KDIVE_PYTHON`), the runner `.env` (`XDG_RUNTIME_DIR`, `KDIVE_SECRETS_ROOT`),
    the `[self-hosted, kvm, x64]` label, and the runner service **installed
    stopped** pending D's trusted-events guard.
  - C ([#1292](https://github.com/randomparity/kdive/issues/1292), ADR-0388) — the
    warm store (`scripts/live-vm/warm-store.sh`) and the hosted TCG image set
    (`scripts/live-vm/stage-tcg-images.sh`), each emitting the `KDIVE_LIVE_VM_*`
    wiring block; and the store shared-lock contract D honors.

## Problem

The `live-vm` CI job today is inert theater (`.github/workflows/ci.yml`):
`workflow_dispatch`-only, `runs-on: [self-hosted, kvm]`, sets no env, stages no
image, and runs `pytest -m live_vm` — which, with no env, skips every test and
reports **green**. The product's core boundary (boot a real kernel, crash it,
introspect the vmcore) therefore has no automated gate, and the "green run that
is no coverage" is the exact failure this epic exists to kill.

Sub-issues A–C built everything the job needs but never wired a runnable job:

- **A** fixed the test-side contract: `tests/live_vm/__init__.py` resolves each
  family's env and exposes `require_live_vm_*` gates; the `live_vm_throwaway` /
  `live_vm_provisioned` **additive sub-markers** (both also carry bare `live_vm`)
  give "declare which family" a real handle.
- **B** built the host: a registered runner (`[self-hosted, kvm, x64]`) whose
  **service is installed stopped** because a self-hosted runner that accepts fork
  PRs is arbitrary code execution on the host — B deliberately deferred the
  workflow `if:` guard that closes that window **to D**. B also fixed the
  persistent `/opt/kdive` venv (with the libguestfs symlinks the worker's
  `guestfs`/`drgn` import needs) and exposed it as `KDIVE_PYTHON`.
- **C** built the image stores: `warm-store.sh` (the self-hosted warm rootfs +
  kernel + matching debuginfo) and `stage-tcg-images.sh` (the ephemeral hosted
  `/mnt` ppc64le set under a measured budget), each emitting a `KDIVE_LIVE_VM_*`
  wiring block, plus a store lockfile whose **shared-lock consume contract D
  honors**.

D turns these inputs into two real gates and the fail-loud env preflight that
keeps them honest.

## Goals

1. **Hosted TCG gate** — a job on `ubuntu-latest` that stands up the compose
   backends + S3 + host processes, stages the ppc64le TCG image set, and runs the
   `live_vm_tcg` spine (four ppc64le proofs) under an **explicit `timeout-minutes`
   derived from a measured boot-to-panic wall-time**. No `/dev/kvm` needed (TCG).
2. **Finish the self-hosted `live_vm` gate** — `schedule` + `workflow_dispatch`,
   reuse the persistent `/opt/kdive` venv, refresh the warm store, set each
   family's env, and run **both** families natively (`-m "live_vm and not
   live_vm_tcg"`): the throwaway-domain family from the warm-store rootfs, and the
   provisioned-System family against a System stood up **on the box** (the epic's
   open C/D detail, resolved here — see ADR-0389).
3. **Fail-loud env preflight keyed on the declared family** — a script in the
   `require_free_http_port` / `die` idiom that, given the family a job intends to
   run, **fails the job** when that family's required env (or S3 credential
   material) is absent, instead of skipping green.
4. **Security: no fork-PR exposure** — the self-hosted job never runs on
   `pull_request`; secrets ride `schedule`/`workflow_dispatch` only. This closes
   the RCE window B left open and lets the operator enable the runner service.

## Non-goals

- **No product code, no database migration.** CI/test-infra only.
- **No new harness or test migration** — A built the harness, E
  ([#1294](https://github.com/randomparity/kdive/issues/1294), open) migrates the
  tests and adds `live_vm_provisioned` to the real provisioned-System tests. D
  wires the job and the preflight; whether a specific provisioned-System test is
  *selected* depends on E, but D's preflight and job stand independently and land
  before E (rollout order A→(B∥C)→D→E→F). D does not block on E.
- **No throwaway-domain foreign-arch TCG** — the TCG tier rides the `live_stack`
  spine (ADR-0353), not the `boot_throwaway_domain` harness. D wires the existing
  spine proofs; it authors no new cross-arch throwaway test.
- **Standing up the ppc64le self-hosted runner** — out of scope (epic non-goal);
  the job's arch-labeled `runs-on` must not block it (a POWER runner joins as a
  new matrix entry).
- **The runbook prose** (canonical live-testing guide) is sub-issue F
  ([#1295](https://github.com/randomparity/kdive/issues/1295)). D adds only the
  operator-facing CI notes its jobs require (trusted-events ordering, secrets).

## Architecture

### A separate workflow file, not new jobs in `ci.yml` (the security spine)

GitHub triggers are **workflow-level, not job-level**: adding a `schedule:` to
`ci.yml`'s shared `on:` would fire *every* existing job (lint, type, test, image
builds) on the nightly cron. So the two live gates live in a **new
`.github/workflows/live.yml`** with its own `on:` and `concurrency`, and the
inert `live-vm` job is **removed from `ci.yml`** (replace, not deprecate). This
also makes fork-PR exposure structurally impossible: `live.yml` carries **no
`pull_request` trigger at all**, so no PR event — fork or same-repo — can dispatch
either job to a runner, independent of any `if:` guard.

`live.yml` `on:`: `schedule` (nightly cron) · `workflow_dispatch` · `push` to
`main`. Per-job `if:` guards then split those events by job:

| Job | `runs-on` | Runs on | `if:` guard |
| --- | --- | --- | --- |
| `tcg` (hosted) | `ubuntu-latest` | `schedule` · `workflow_dispatch` · `push` to `main` | `github.event_name != 'pull_request'` (belt-and-suspenders; `live.yml` has no PR trigger) |
| `native` (self-hosted) | `[self-hosted, kvm, x64]` | `schedule` · `workflow_dispatch` **only** | `github.event_name == 'schedule' \|\| github.event_name == 'workflow_dispatch'` |

The self-hosted job uses a **positive allowlist**, not `!= 'pull_request'`, so a
`push` to `main` does **not** fire the RCE-sensitive, minutes-scale self-hosted
boot on every maintainer commit — only the nightly cron and explicit dispatch do.
The hosted TCG job additionally runs on `push` to `main` (it bears no fork-PR
risk — hosted, no `/dev/kvm`, secrets ride trusted events only). The paired
repository setting — **"Require approval for all outside collaborators"** — is an
operator step the runbook orders *before* enabling the runner service (defense in
depth: the `if:` allowlist is the code control, the repo setting is the platform
control, the missing `pull_request` trigger is the structural control).

**Why the hosted TCG job is nightly + on-`main`, not a PR gate** (ADR-0389,
Decision 1): a ppc64le boot-to-panic under TCG is minutes-scale and variable, and
the job stands up the full compose stack first. Making every PR wait on it — and
flake on it — is the "slower theater" the epic's risk section warns against.
Nightly + `workflow_dispatch` + `push`-to-`main` gives default-branch regression
coverage and on-demand proof (the introducing PR proves it green via
`workflow_dispatch`) without taxing every contributor. Promotion to a PR gate
later is a one-line trigger addition with no job-body change, once the measured
wall-time and flake rate justify it (the number is from the local proof — see
Testing — and recorded in the ADR).

### Job 1 — `tcg` (hosted, TCG spine)

Runs the `live_vm_tcg` proofs (`test_ppc64le_*` in
`tests/integration/test_live_stack.py`) over the live-stack spine. These are
`live_stack`-marked and read the spine env, **not** the throwaway `KDIVE_LIVE_VM_*`
harness env — so D maps C's store output onto the spine's env (below).

**Enforced scope: the three core proofs** — `ssh_reachable`, `kdump_captures`,
`fadump_captures` — whose inputs the stage step produces. The fourth marked proof,
`uploaded_kernel_bundle_boots` (#1146), needs a separately-built ppc64le kernel
bundle (`kernel.tar.gz` + `initrd.img`); **no repo script produces one** (only the
#1146 design record documents the manual build), so building it on the hosted
runner is disproportionate scope for this sub-issue. The gate therefore enforces
the three core proofs and treats the bundle proof as an **operator opt-in**: it
runs only when `KDIVE_PPC64LE_BUNDLE` is supplied (a dispatch input) and otherwise
skips cleanly. This is an *explicit, documented* scope — goals, the ADR, and the
shape guard all say "three core proofs (+ bundle when staged)" — not a silent
green skip: the shape test pins that exactly these three are the required set and
the bundle proof is the one intentional opt-in, so an *accidental* drop of a core
proof's env still fails the `tcg` preflight. Producing the bundle in-gate is a
named follow-up, not a hidden gap.

Step order (each fails loud):

1. **Checkout** (`persist-credentials: false`, pinned action SHA).
2. **Host deps** — apt-install the ppc64le system emulator (`qemu-system-ppc64` /
   the `qemu-system-ppc` package) so `require_guest_arch("ppc64le")` does not skip;
   the build-fs/libguestfs deps `stage-tcg-images.sh` needs (`libguestfs-tools`,
   `e2fsprogs`); and `elfutils` + `debuginfod` for the build-id debuginfo fetch.
   *(Parity obligation, AGENTS.md: every host tool a live job needs is declared in
   the owning Ansible role. The hosted `ubuntu-latest` runner is GitHub's image,
   not one we provision, so these live only in the workflow — but the self-hosted
   deps stay in `live_vm_host`; D adds no undeclared host dep to a provisioned
   host.)*
3. **uv + deps** — `uv sync --locked --group live` (the hosted runner has no
   persistent venv; unlike the self-hosted job, a throwaway venv here is correct —
   the TCG tier drives the stack over HTTP and needs no in-process libguestfs).
4. **Bring up the stack** — compose backends (`just stack-up`: Postgres + MinIO +
   the bucket + mock-OIDC + migrations) and the host processes (`docker compose up
   -d migrate server worker reconciler`), waited healthy on `/readyz`.
5. **Stage the TCG image set** — `eval "$(scripts/live-vm/stage-tcg-images.sh)"`
   with `KDIVE_TCG_IMAGE` set to the ppc64le catalog rootfs and `DEBUGINFOD_URLS`
   set to a ppc64le-indexing server. This exports `KDIVE_LIVE_VM_ROOTFS` (the
   staged ppc64le `rootfs.qcow2`), `KDIVE_LIVE_VM_BZIMAGE`, and
   `KDIVE_LIVE_VM_VMLINUX` (the matching debuginfo).
6. **Map store output → spine env** (the naming reconciliation, made explicit):
   - `KDIVE_GUEST_IMAGE_PPC64LE` ← `KDIVE_LIVE_VM_ROOTFS` (the spine proof reads
     the ppc64le rootfs from `KDIVE_GUEST_IMAGE_PPC64LE`; the store emits it as
     `KDIVE_LIVE_VM_ROOTFS`).
   - `KDIVE_KERNEL_SRC` ← a kernel tree from `scripts/fetch-kernel-tree.sh` (the
     spine's build/upload step needs an arch-opaque kernel tree; the x86_64 tree is
     valid for a ppc64le guest, ADR-0272/#1146).
   - `KDIVE_STACK_BASE_URL`, `KDIVE_OIDC_ISSUER`, `KDIVE_DATABASE_URL`,
     `KDIVE_S3_ENDPOINT_URL`, `KDIVE_S3_BUCKET`, and the S3 credential env
     (`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`, the object-store auth the
     compose stack uses — docker-compose.yml) ← the compose stack's values.
   - `KDIVE_PPC64LE_BUNDLE` is the **opt-in** for the fourth proof (above); unset →
     the bundle proof skips, the three core proofs run. The `tcg` preflight does
     **not** require it (an opt-in, not a core input).
7. **Fail-loud preflight** — `scripts/live-vm/preflight-env.sh tcg` asserts every
   var the three core proofs need (the mapped spine env + the S3 credential env +
   `qemu-system-ppc64` on PATH) is present and the OIDC issuer is reachable; a gap
   fails the job here with the exact missing var, not as a mid-suite skip.
8. **Run** — `just test-live-tcg` (`-m live_vm_tcg --strict-markers`), under the
   job's `timeout-minutes` (measured; see Testing).

Steps 5–8 (the `stage-tcg-images.sh` `eval`, the env mapping, the preflight, the
run) execute as **one `run:` block** for the same reason Job 2 does: each Actions
`run:` is a fresh shell, so the staged `KDIVE_LIVE_VM_*` exports and the mapped
spine vars would be gone by the preflight if split across steps. `AWS_ACCESS_KEY_ID`
/ `AWS_SECRET_ACCESS_KEY` are the compose MinIO's `minioadmin` and are exported
explicitly by the block (the tcg job does not source `env.sh`); the `tcg` preflight
requiring them catches a block that forgot to export the S3 creds the presigned
upload needs — meaningful here, unlike the on-box `env.sh`-defaulted native path.

The store's shared-lock consume contract (C) does not apply here — the hosted set
is single-tenant and ephemeral (its own `KDIVE_TCG_STAGE_DIR`, no concurrent
refresh), so no `flock` is taken; the concurrency group (below) prevents two TCG
jobs racing the same `/mnt`.

### Job 2 — `native` (self-hosted, both native families)

Uses the persistent `/opt/kdive` venv as the **interpreter** (its libguestfs
`.so` + `guestfs.py` symlinks and `drgn` live only there — B's explicit D
constraint), but tests the **checked-out sources** via a `PYTHONPATH` overlay
rather than by mutating `/opt/kdive`'s git checkout. This resolves the shared-
mutable-state hazards a "update `/opt/kdive` to the dispatched ref" approach would
create (stale-ref bleed into the next `schedule` run, unserialized concurrent
different-ref dispatches, and a re-`uv sync` dropping the hand-placed symlinks).

1. **Checkout** the tested ref into `$GITHUB_WORKSPACE` (per-job, clean; the test
   sources).
2. **Resolve the interpreter + source overlay:**
   `KDIVE_PYTHON=/opt/kdive/.venv/bin/python` (the venv with libguestfs/drgn +
   every dependency) and `PYTHONPATH=$GITHUB_WORKSPACE/src` prepended, so both the
   test process and the worker/build-fs subprocesses **import the tested
   `kdive` sources** (`PYTHONPATH` precedes site-packages) while resolving the
   C-extension deps (`libvirt`, `drgn`, `guestfs`) from the venv. `/opt/kdive` is
   **never mutated** — no git update, no re-sync — so a feature-branch dispatch
   cannot corrupt the next nightly's sources and no run can drop the symlinks.
   *(Bound: the overlay assumes the tested ref does not add a new PyPI dependency
   absent from `/opt/kdive`'s venv. The preflight asserts `kdive` imports under the
   overlay before the suite runs, so a dependency drift fails loud at the job
   boundary — the documented limitation, not a silent mis-test.)* The runner `.env`
   already supplies `XDG_RUNTIME_DIR` and `KDIVE_SECRETS_ROOT` to every job process.
3. **Pre-job reaper** — before any bring-up, `virsh destroy` any leftover
   `kdive-*` domains and `docker compose down -v` a stale stack (idempotent), so a
   prior run cancelled mid-boot (below) cannot collide with this run's on-box
   stand-up. Fail-safe: the reaper tolerates "nothing to clean".
4. **Refresh the warm store** — `eval "$(scripts/live-vm/warm-store.sh)"`, exporting
   `KDIVE_LIVE_VM_ROOTFS` / `KDIVE_LIVE_VM_BZIMAGE` / `KDIVE_LIVE_VM_VMLINUX` from
   the committed `current/` set (the throwaway family's rootfs).
5. **Stand up the provisioned-System family on the box** (ADR-0389, Decision 2) —
   `scripts/live-stack/up.sh --skip-obs` brings up the compose backends (MinIO with
   the well-known `minioadmin` root, docker-compose.yml) + host processes on the
   runner (the box has libvirt from `libvirt_stack`). The on-box MinIO **is** the
   object store, so `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` are the
   `minioadmin` default `scripts/live-stack/env.sh` supplies — not a repo secret;
   an external-S3 deployment is out of scope for the nightly. Then a **new
   `scripts/live-vm/mint-system.sh`** mints the System the family needs, in order:
   (i) fund/onboard a project (reuse `scripts/live-stack/onboard.sh` — `up.sh`
   itself prints "no project funded yet … just onboard", so funding is a required
   prerequisite, not an assumption); (ii) mint an admin token from the mock issuer;
   (iii) allocate → provision from the warm rootfs (`KDIVE_LIVE_VM_ROOTFS`); (iv)
   poll to `ready`; (v) print the System id, captured into
   `KDIVE_LIVE_VM_SYSTEM_ID`. It fails loud at each step (the `die`/`require_*`
   idiom). Self-contained: the reaper (step 3) is what guarantees "nothing external
   must stay alive between nightly runs", since a cancelled run cannot run its own
   teardown.
   **Host-dep parity (a real gap):** `up.sh` / `just stack-up` require `docker` +
   the compose plugin, which **B's Ansible roles do not provision**. Per the
   AGENTS.md provisioning-parity convention, D declares `docker` + the compose
   plugin as a `live_vm_host` dependency **in the same change** (the role edit ships
   with D), so a freshly-reprovisioned cattle runner is stack-capable.
6. **Fail-loud preflight** — `scripts/live-vm/preflight-env.sh throwaway provisioned`
   asserts **both** declared families' env. Its teeth over A's resolver is
   **`KDIVE_LIVE_VM_SYSTEM_ID` non-empty** — A's resolver returns `AVAILABLE` on
   `KDIVE_S3_ENDPOINT_URL` + `KDIVE_S3_BUCKET` alone, so a family declared but with
   no System minted (a `mint-system.sh` failure the workflow missed) would skip
   green; the preflight fails loud instead. (It also checks endpoint + bucket set;
   the `AWS_*` creds are the on-box `minioadmin` default and so are not a meaningful
   absence check on this path — an *external*-S3 misconfiguration is caught at the
   ADR-0089 worker secrets boundary, not here.)
7. **Run** — `just test-live` (`-m "live_vm and not live_vm_tcg"`), both families.

**Single-shell env continuity (GitHub Actions plumbing):** each `run:` step in
Actions executes in a **fresh shell**, so a var `eval`'d/`export`ed in one step
does **not** survive to the next. Steps 4–7 (warm-store `eval`, `up.sh` +
`env.sh` source, `mint-system.sh` capture, preflight, `just test-live`) therefore
run as **one `run:` block** — a single shell keeps `KDIVE_LIVE_VM_*`,
`KDIVE_LIVE_VM_SYSTEM_ID`, and the sourced `env.sh` exports live through the
preflight and the suite. (The alternative — forwarding each producer's exports to
`$GITHUB_ENV` — is more boilerplate for the same effect; the plan states the
single-`run:` shape so an implementer does not emit the natural-but-broken
multi-step form where every staged var is gone by the preflight.)

**Store consume lock (C's contract):** for the throwaway family, the boot holds a
**shared** `flock` on the store lockfile for the domain's life so a concurrent
`warm-store.sh` refresh cannot swap `current/` out from under an in-flight boot.
In CI the concurrency group already serializes jobs, but honoring the lock keeps
the contract intact for an operator's manual concurrent refresh.

### The fail-loud env preflight (`scripts/live-vm/preflight-env.sh`)

The shared handle the epic requires. A small POSIX-`sh`/bash script in the
`scripts/live-vm/lib.sh` `die` idiom (reused, not reinvented), taking one or more
family names as args:

- `throwaway` → require `KDIVE_LIVE_VM_ROOTFS` set **and the file exists**, and a
  resolvable libvirt URI.
- `provisioned` → require `KDIVE_LIVE_VM_SYSTEM_ID` non-empty (**the teeth over A**:
  A's resolver returns `AVAILABLE` on `KDIVE_S3_ENDPOINT_URL` + `KDIVE_S3_BUCKET`
  alone, so a family declared but with no System minted skips green; this fails
  loud), plus `KDIVE_S3_ENDPOINT_URL` + `KDIVE_S3_BUCKET` set. The `AWS_*` creds are
  **not** checked here: the on-box path authenticates to the compose MinIO with the
  `minioadmin` default `env.sh` supplies, so credential *absence* is not a reachable
  failure on this path (an external-S3 misconfiguration is caught at the ADR-0089
  worker secrets boundary, not by a vacuous non-empty check the `minioadmin` default
  would always satisfy).
- `tcg` → require `KDIVE_STACK_BASE_URL`, `KDIVE_OIDC_ISSUER`, `KDIVE_DATABASE_URL`,
  `KDIVE_S3_ENDPOINT_URL`, `KDIVE_S3_BUCKET`, `AWS_ACCESS_KEY_ID`,
  `AWS_SECRET_ACCESS_KEY` (here the check **has teeth** — the tcg job does not source
  `env.sh`, so the run-block must export the presigned-upload creds explicitly; a
  forgotten export fails loud), `KDIVE_GUEST_IMAGE_PPC64LE` (file exists),
  `KDIVE_KERNEL_SRC` (path exists), and `qemu-system-ppc64` on PATH.
  (`KDIVE_PPC64LE_BUNDLE` is **not** required — the opt-in fourth proof.)

**Skip-vs-fail discipline** (mirrors A's contract): the preflight's job is to
distinguish "the operator asked for family X but the runner is not set up for it"
(→ **fail loud**, non-zero, name the missing var) from "no family requested" (not
this script's concern — a job always declares at least one family). It never
`pytest.skip`s; skipping is A's per-test gate for an *unrequested* capability. The
preflight asserts the *requested* families are runnable, so a mis-provisioned
runner fails the job instead of masquerading as green.

### Concurrency & permissions

- `permissions: contents: read` at the workflow level (least privilege; neither
  job writes the repo or packages).
- **Per-job** `concurrency` groups with **`cancel-in-progress: false`** —
  deliberately *not* the `true` that `ci.yml` uses. On a non-ephemeral self-hosted
  runner, cancelling a job mid-boot does not reliably run trap cleanup, leaving an
  orphaned libvirt domain, provisioned System, compose stack, and a held `flock`;
  `cancel-in-progress: false` lets a running boot finish rather than be killed by a
  later dispatch, and the pre-job reaper (Job 2 step 3) reclaims any orphan a
  crash/timeout still leaves. The two jobs get **distinct** group keys
  (`live-tcg-${{ github.ref }}` and `live-native-${{ github.ref }}`), so a long or
  wedged self-hosted `native` run does not queue an unrelated `tcg` push-to-`main`
  run behind it (and vice versa) — the `false` protects an in-flight self-hosted
  boot without cross-blocking the independent hosted gate.
- Each job carries its own `timeout-minutes` (the TCG job's is measured; see
  Testing) so a wedged emulator/boot fails the job instead of hanging the runner.
- Actions pinned by SHA (the `zizmor` gate enforces this); no
  `pull_request_target`, no untrusted checkout with secrets, and — structurally —
  no `pull_request` trigger in `live.yml` at all.

## Failure modes and how they are handled

| Failure | Handling |
| --- | --- |
| Fork PR triggers a self-hosted run (RCE) | `live.yml` has **no `pull_request` trigger** (structural), the self-hosted job's `if:` is a positive `schedule`/`workflow_dispatch` allowlist (code), and the "require approval for outside collaborators" repo setting is ordered before enabling the runner service (platform). Three layers; secrets ride trusted events only. |
| `push` to `main` fires the RCE-sensitive self-hosted boot on every commit | The self-hosted job's `if:` is `schedule \|\| workflow_dispatch` — **not** `!= 'pull_request'`, which would admit `push`. Only the hosted TCG job runs on `push`. |
| Adding `schedule` fans out to every existing `ci.yml` job | The live jobs are a **separate `live.yml`** with its own `on:`; `ci.yml` is unchanged except the inert `live-vm` job is removed. No existing job gains a nightly run. |
| A declared family's env is absent → suite skips green | `preflight-env.sh <family>` fails the job with the exact missing var *before* pytest runs — never a mid-suite skip. |
| Provisioned family: endpoint/bucket env set but S3 credentials absent | The preflight requires `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` non-empty (the specific credentials the object-store client uses), the gap A's resolver leaves — a runner with only the endpoint/bucket env fails loud, not `AVAILABLE`. |
| Self-hosted job builds a throwaway venv → `guestfs` import fails at test time | The job uses `/opt/kdive/.venv` as the interpreter (`KDIVE_PYTHON`) and overlays the tested sources via `PYTHONPATH=$GITHUB_WORKSPACE/src`; never `uv sync` in the workspace. |
| `workflow_dispatch` on a feature branch tests stale sources / drops symlinks | The job **never mutates `/opt/kdive`** — it overlays `$GITHUB_WORKSPACE/src` via `PYTHONPATH`, so a prior dispatch cannot bleed into the next nightly and no re-sync can drop the libguestfs symlinks. The preflight asserts `kdive` imports under the overlay, so a new-dependency drift fails loud. |
| A run cancelled mid-boot orphans a domain / System / compose stack / `flock` | `live.yml`'s own concurrency group sets `cancel-in-progress: false`, so a running boot is not killed; the Job 2 pre-job reaper (`virsh destroy` leftover `kdive-*` domains + `docker compose down -v`) reclaims any orphan a crash/timeout still leaves. |
| Warm-store refresh swaps `current/` mid-boot (TOCTOU) | The throwaway boot holds a shared `flock` on C's store lockfile for the domain's life; a refresh's exclusive lock waits. |
| Self-hosted stack bring-up needs `docker`/compose that B never provisioned | D declares `docker` + the compose plugin as a `live_vm_host` dependency **in the same change** (parity convention), so a reprovisioned cattle runner is stack-capable. |
| TCG boot-to-panic exceeds the deadline | The per-proof reachability deadline is already generous (900 s, #1144); the job `timeout-minutes` bounds the whole suite (measured; see Testing) so a wedged emulator fails the job instead of burning hosted minutes indefinitely. |
| ppc64le emulator absent on the hosted runner → TCG proofs skip green | The `tcg` preflight requires `qemu-system-ppc64` on PATH and fails the job if the apt-install step did not provide it — the gate does not silently degrade to a skip. |
| Store output (`KDIVE_LIVE_VM_ROOTFS`) not mapped to the spine env (`KDIVE_GUEST_IMAGE_PPC64LE`) | The mapping is an explicit job step; the `tcg` preflight asserts `KDIVE_GUEST_IMAGE_PPC64LE` (file exists), so a missed mapping fails loud, not as a mid-suite skip. |
| The opt-in bundle proof silently drops a *core* proof's coverage | The shape test pins the three required core proofs as the enforced set and the bundle proof as the single intentional opt-in, so dropping a core proof's env fails the `tcg` preflight; only the bundle proof may skip. |
| Two TCG jobs race the same `/mnt` scratch | `live.yml`'s concurrency group serializes same-ref runs (`cancel-in-progress: false` — the later run waits); `stage-tcg-images.sh`'s own `trap` cleanup + `require_free_space` guard the scratch. |

## Testing

The CI **behavior** (a live job actually booting a ppc64le guest) cannot run in
ordinary hosted PR CI — that is the whole reason the tiers exist. So D's tests
split into what ordinary CI proves and what the operator/local proof proves:

- **Ordinary CI (every PR) — the guardrails that gate the change itself:**
  - `actionlint` + `zizmor` (`just lint-workflows`) on the new `live.yml` — syntax,
    pinned-SHA, and security posture (`permissions`, no `pull_request_target`).
    These fail the PR if a pin or a trigger is wrong.
  - `shellcheck` + `shfmt` (`just lint-shell`) on `preflight-env.sh`.
  - **`tests/scripts/test_live_vm_preflight.py`** — subprocess-source the preflight
    (the pattern C's `test_live_vm_stores.py` uses), asserting for each family:
    present env → exit 0; each required var missing → non-zero, message names the
    missing var; the provisioned family's **missing-`AWS_*`-credential** case fails
    loud (endpoint/bucket set, `AWS_ACCESS_KEY_ID` empty → fail) **and** the
    non-empty-but-wrong case (an unrelated var set, the credential var still empty →
    fail); an unknown family arg fails loud rather than passing vacuously; `tcg`
    with `qemu-system-ppc64` absent (stubbed empty PATH) fails loud.
  - **A workflow-shape guard** — a test (YAML parse, no marker) over `live.yml`
    asserting: (a) `on:` has **no `pull_request`** trigger (structural fork-PR
    exclusion); (b) the self-hosted job's `if:` is the **positive** `schedule ||
    workflow_dispatch` allowlist (not merely "not `pull_request`", which would admit
    `push`); (c) the hosted job does not run on `pull_request`; (d) `concurrency`
    sets `cancel-in-progress: false`. This pins the security + cleanup posture at
    the source, analogous to `test_live_vm_tcg_tier.py` pinning the marker set — a
    future edit that re-exposes the runner or re-enables mid-boot cancellation fails
    here. It also asserts `ci.yml` no longer defines a `live-vm` job (the removal is
    permanent, not a dangling inert copy).
- **Local live proof (grounds the timeout, records the wall-time):** on this
  x86_64 host, ppc64le is foreign → TCG anyway, so the hosted-gate boot path is
  reproducible locally. Bring up the stack, apt/dnf the ppc64le emulator, stage the
  TCG set, and run `just test-live-tcg`; record the measured boot-to-panic
  wall-time and set `timeout-minutes` = **⌈measured wall-time × 1.5⌉, floored at
  30 min** (a concrete, falsifiable headroom rule, not "with headroom"). The
  measured number and the resulting timeout are recorded in ADR-0389 (whose Status
  stays **Proposed** until the number lands) and this spec, the same
  CI-cannot-prove-it-live posture A/B/C shipped. The self-hosted job's
  *process-context* proof (a real nightly on the enabled runner) is the operator
  step, deferred to the runbook.
- Guardrails to run before commit: `just lint-workflows lint-shell test`
  (workflow + shell + the preflight/shape tests), and the full `just ci` before
  push (env-docs, docs guards, the whole PR gate).

## Rollout / rollback

- **What lands:** a new `.github/workflows/live.yml` (the two gates), the inert
  `live-vm` job **removed** from `ci.yml` (replace, not deprecate), a
  `live_vm_host` Ansible role edit declaring `docker` + the compose plugin,
  `scripts/live-vm/preflight-env.sh` + its test, **`scripts/live-vm/mint-system.sh`**
  (fund/onboard → token → allocate → provision → poll-ready → print id) + its
  shell lint/behavioral coverage, the workflow-shape guard, ADR-0389, this spec,
  the plan, and the operator CI notes. `ci.yml`'s existing
  jobs are otherwise untouched, so **no existing job gains a nightly run** (the
  separate `on:` is why). Any new `KDIVE_*` the preflight introduces is added to
  `external_env.py` (env-docs guard); the vars it *reads* are already documented
  (A/B/C); `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` are standard AWS-SDK
  credential env, documented as external env if the guard requires it.
- **Enabling order (operator, runbook):** the self-hosted runner service stays
  stopped until D's `live.yml` is merged **and** the "require approval for outside
  collaborators" repo setting is applied; only then `github_runner_service_enabled:
  true`. Merging D does not by itself expose the runner — enabling the service does,
  and only after both controls are in place.
- **Rollback:** delete `live.yml` (both gates disappear) and leave the runner
  service disabled; the preflight script, the Ansible dep, and the tests are inert
  without the workflow. The `ci.yml` `live-vm`-job removal need not be reverted (the
  job was inert theater). No migration, no data — nothing to undo beyond the
  workflow file, the script/test, and the role edit.
