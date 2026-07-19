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

### Trigger matrix (the security spine)

| Job | `runs-on` | Triggers | Never on |
| --- | --- | --- | --- |
| `live-vm-tcg` (hosted) | `ubuntu-latest` | `schedule` (nightly) · `workflow_dispatch` · `push` to `main` | fork PR (no secrets reach it) |
| `live-vm` (self-hosted) | `[self-hosted, kvm, x64]` | `schedule` (nightly) · `workflow_dispatch` | **any** `pull_request` (fork-PR RCE) |

Both jobs are gated with `if: github.event_name != 'pull_request'`, so neither
runs on any PR (fork or same-repo) — a self-hosted runner cannot distinguish a
trusted contributor's PR from a fork's at the `runs-on` layer, so the guard
excludes *all* PRs and relies on `schedule`/`workflow_dispatch`/`push` for
coverage. The paired repository setting — **"Require approval for all outside
collaborators"** — is an operator step the runbook orders *before* enabling the
runner service; the ADR records why (defense in depth: the `if:` guard is the
code control, the repo setting is the platform control).

**Why the hosted TCG job is nightly, not a PR gate** (ADR-0389, Decision 1): a
ppc64le boot-to-panic under TCG is minutes-scale and variable, and the job stands
up the full compose stack first. Making every PR wait on it — and flake on it —
is the "slower theater" the epic's risk section warns against. Nightly +
`workflow_dispatch` + `push`-to-`main` gives regression coverage on the default
branch and on demand (the introducing PR proves it green via `workflow_dispatch`)
without taxing every contributor. If the measured wall-time and flake rate later
prove cheap and stable, promoting it to a PR gate is a one-line trigger change —
the job body does not change. The measured number that grounds this choice comes
from the local proof (see Testing) and is recorded in the ADR.

### Job 1 — `live-vm-tcg` (hosted, TCG spine)

Runs the four `live_vm_tcg` proofs (`test_ppc64le_*` in
`tests/integration/test_live_stack.py`) over the live-stack spine. These are
`live_stack`-marked and read the spine env, **not** the throwaway `KDIVE_LIVE_VM_*`
harness env — so D maps C's store output onto the spine's env (below).

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
     `KDIVE_S3_ENDPOINT_URL`, `KDIVE_S3_BUCKET` ← the compose stack's values.
   - `KDIVE_PPC64LE_BUNDLE` is optional; unset → the #1146 bundle proof skips
     cleanly, which is acceptable for the gate (the reachability + kdump + fadump
     proofs are the core three).
7. **Fail-loud preflight** — `scripts/live-vm/preflight-env.sh tcg` asserts every
   var step 6 must have set is present and the OIDC issuer is reachable; a gap
   fails the job here with the exact missing var, not as a mid-suite skip.
8. **Run** — `just test-live-tcg` (`-m live_vm_tcg --strict-markers`), under the
   job's `timeout-minutes` (measured; see Testing).

The store's shared-lock consume contract (C) does not apply here — the hosted set
is single-tenant and ephemeral (its own `KDIVE_TCG_STAGE_DIR`, no concurrent
refresh), so no `flock` is taken; the concurrency group (below) prevents two TCG
jobs racing the same `/mnt`.

### Job 2 — `live-vm` (self-hosted, both native families)

Reuses the persistent `/opt/kdive` venv and warm store B/C built. **Does not**
`uv sync` a throwaway venv in `$GITHUB_WORKSPACE` — the libguestfs `.so` symlinks
live only in `/opt/kdive/.venv`, so a fresh workspace venv gets `drgn` but not
`guestfs` and the worker's introspection import fails at test time (B spec's
explicit D constraint). The workflow therefore:

1. **Checkout** the tested ref into `$GITHUB_WORKSPACE` (the test sources).
2. **Resolve `KDIVE_PYTHON`** = `/opt/kdive/.venv/bin/python` (the runner `.env`
   already carries `XDG_RUNTIME_DIR` and `KDIVE_SECRETS_ROOT` into every job
   process). The worker/build-fs subprocesses use `KDIVE_PYTHON`; the test process
   itself runs from `/opt/kdive` (the venv where `kdive` + `guestfs`/`drgn` are
   importable), driving the sources — see "Which checkout runs" below.
3. **Refresh the warm store** — `eval "$(scripts/live-vm/warm-store.sh)"`, exporting
   `KDIVE_LIVE_VM_ROOTFS` / `KDIVE_LIVE_VM_BZIMAGE` / `KDIVE_LIVE_VM_VMLINUX` from
   the committed `current/` set (the throwaway family's rootfs).
4. **Stand up the provisioned-System family on the box** (ADR-0389, Decision 2) —
   bring up the compose backends + host processes on the runner (the box already
   has libvirt from `libvirt_stack`), provision one System from the warm rootfs,
   and export its id as `KDIVE_LIVE_VM_SYSTEM_ID`. `KDIVE_S3_*` + credential
   material come from repo/org secrets → `KDIVE_SECRETS_ROOT`. Self-contained:
   nothing external must stay alive between nightly runs.
5. **Fail-loud preflight** — `scripts/live-vm/preflight-env.sh throwaway provisioned`
   asserts **both** declared families' env, and — the gap A does not cover — that
   the S3 credential *material* under `KDIVE_SECRETS_ROOT` is present for the
   provisioned family (A's resolver checks only the `KDIVE_S3_*` env, per its
   docstring). A missing System id or empty secrets dir fails the job.
6. **Run** — `just test-live` (`-m "live_vm and not live_vm_tcg"`), both families.

**Store consume lock (C's contract):** for the throwaway family, the boot holds a
**shared** `flock` on the store lockfile for the domain's life so a concurrent
`warm-store.sh` refresh cannot swap `current/` out from under an in-flight boot.
In CI the concurrency group already serializes jobs, but honoring the lock keeps
the contract intact for an operator's manual concurrent refresh.

**Which checkout runs (the workspace-vs-persistent-venv seam):** a `schedule`
run tests `main`, which equals `/opt/kdive`'s pinned `main` — running from the
persistent venv is exact. A `workflow_dispatch` on a feature branch diverges, so
the job **updates `/opt/kdive` to the dispatched ref and re-syncs** before running
(a documented, bounded step), keeping the libguestfs symlinks while testing the
requested sources. This is stated so the venv-reuse constraint and branch-dispatch
both hold, rather than silently only working on `main`.

### The fail-loud env preflight (`scripts/live-vm/preflight-env.sh`)

The shared handle the epic requires. A small POSIX-`sh`/bash script in the
`scripts/live-vm/lib.sh` `die` idiom (reused, not reinvented), taking one or more
family names as args:

- `throwaway` → require `KDIVE_LIVE_VM_ROOTFS` set **and the file exists**, and a
  resolvable libvirt URI.
- `provisioned` → require `KDIVE_LIVE_VM_SYSTEM_ID` non-empty, `KDIVE_S3_ENDPOINT_URL`
  + `KDIVE_S3_BUCKET` set, **and** the S3 credential material present under
  `KDIVE_SECRETS_ROOT` (the A-gap).
- `tcg` → require `KDIVE_STACK_BASE_URL`, `KDIVE_OIDC_ISSUER`, `KDIVE_DATABASE_URL`,
  `KDIVE_S3_ENDPOINT_URL`, `KDIVE_S3_BUCKET`, `KDIVE_GUEST_IMAGE_PPC64LE` (file
  exists), `KDIVE_KERNEL_SRC` (path exists), and `qemu-system-ppc64` on PATH.

**Skip-vs-fail discipline** (mirrors A's contract): the preflight's job is to
distinguish "the operator asked for family X but the runner is not set up for it"
(→ **fail loud**, non-zero, name the missing var) from "no family requested" (not
this script's concern — a job always declares at least one family). It never
`pytest.skip`s; skipping is A's per-test gate for an *unrequested* capability. The
preflight asserts the *requested* families are runnable, so a mis-provisioned
runner fails the job instead of masquerading as green.

### Concurrency & permissions

- `permissions: contents: read` at the job level (least privilege; neither job
  writes the repo or packages).
- The existing top-level `concurrency` group (`cancel-in-progress: true`) already
  serializes same-ref runs; the self-hosted and hosted jobs each carry their own
  `timeout-minutes` so a wedged boot cannot hang a runner indefinitely.
- Actions pinned by SHA (the `zizmor` gate enforces this); no
  `pull_request_target`, no untrusted checkout with secrets.

## Failure modes and how they are handled

| Failure | Handling |
| --- | --- |
| Fork PR triggers a self-hosted run (RCE) | `if: github.event_name != 'pull_request'` on both jobs — no PR (fork or same-repo) dispatches to the runner; secrets ride `schedule`/`workflow_dispatch` only. The runbook orders the "require approval for outside collaborators" repo setting before the operator enables the runner service. |
| A declared family's env is absent → suite skips green | `preflight-env.sh <family>` fails the job with the exact missing var *before* pytest runs — never a mid-suite skip. |
| Provisioned family: `KDIVE_S3_*` env set but credential files absent | The preflight checks the material under `KDIVE_SECRETS_ROOT` (the gap A's resolver leaves), so a runner with the env but no credentials fails loud, not `AVAILABLE`. |
| Self-hosted job builds a throwaway venv → `guestfs` import fails at test time | The job reuses `/opt/kdive/.venv` via `KDIVE_PYTHON` (never `uv sync` in the workspace); documented as the B/D seam. |
| `workflow_dispatch` on a feature branch tests stale `/opt/kdive` `main` sources | The job updates `/opt/kdive` to the dispatched ref + re-syncs before running, so the tested sources match the dispatch while the libguestfs symlinks persist. |
| Warm-store refresh swaps `current/` mid-boot (TOCTOU) | The throwaway boot holds a shared `flock` on C's store lockfile for the domain's life; a refresh's exclusive lock waits. |
| TCG boot-to-panic exceeds the deadline | The per-proof reachability deadline is already generous (900 s, #1144); the job `timeout-minutes` bounds the whole suite (measured; see Testing) so a wedged emulator fails the job instead of burning hosted minutes indefinitely. |
| ppc64le emulator absent on the hosted runner → TCG proofs skip green | The `tcg` preflight requires `qemu-system-ppc64` on PATH and fails the job if the apt-install step did not provide it — the gate does not silently degrade to a skip. |
| Store output (`KDIVE_LIVE_VM_ROOTFS`) not mapped to the spine env (`KDIVE_GUEST_IMAGE_PPC64LE`) | The mapping is an explicit job step; the `tcg` preflight asserts `KDIVE_GUEST_IMAGE_PPC64LE` (file exists), so a missed mapping fails loud, not as a mid-suite skip. |
| Two TCG jobs race the same `/mnt` scratch | The top-level concurrency group cancels the in-progress same-ref run; `stage-tcg-images.sh`'s own `trap` cleanup + `require_free_space` guard the scratch. |

## Testing

The CI **behavior** (a live job actually booting a ppc64le guest) cannot run in
ordinary hosted PR CI — that is the whole reason the tiers exist. So D's tests
split into what ordinary CI proves and what the operator/local proof proves:

- **Ordinary CI (every PR) — the guardrails that gate the change itself:**
  - `actionlint` + `zizmor` (`just lint-workflows`) on the edited `ci.yml` —
    syntax, pinned-SHA, and security posture (`if:` guards, `permissions`,
    no `pull_request_target`). These fail the PR if the fork-PR guard or a pin is
    wrong.
  - `shellcheck` + `shfmt` (`just lint-shell`) on `preflight-env.sh`.
  - **`tests/scripts/test_live_vm_preflight.py`** — subprocess-source the preflight
    (the pattern C's `test_live_vm_stores.py` uses), asserting for each family:
    present env → exit 0; each required var missing → non-zero, message names the
    missing var; the provisioned family's missing-credential-material case fails
    loud (stub `KDIVE_SECRETS_ROOT` at an empty dir); an unknown family arg fails
    loud rather than passing vacuously; `tcg` with `qemu-system-ppc64` absent
    (stubbed empty PATH) fails loud.
  - **A workflow-shape guard** — a test (AST/YAML, no marker) asserting the two
    jobs carry `if: github.event_name != 'pull_request'` and never trigger on
    `pull_request`, so a future edit cannot silently re-expose the self-hosted
    runner to fork PRs. This is the security acceptance criterion pinned at the
    source, analogous to `test_live_vm_tcg_tier.py` pinning the marker set.
- **Local live proof (grounds the timeout, records the wall-time):** on this
  x86_64 host, ppc64le is foreign → TCG anyway, so the hosted-gate boot path is
  reproducible locally. Bring up the stack, apt/dnf the ppc64le emulator, stage the
  TCG set, and run `just test-live-tcg`; record the measured boot-to-panic
  wall-time and set `timeout-minutes` from it (with headroom). The measured number
  is recorded in ADR-0389 and this spec, the same CI-cannot-prove-it-live posture
  A/B/C shipped. The self-hosted job's *process-context* proof (a real nightly on
  the enabled runner) is the operator step, deferred to the runbook.
- Guardrails to run before commit: `just lint-workflows lint-shell test`
  (workflow + shell + the preflight/shape tests), and the full `just ci` before
  push (env-docs, docs guards, the whole PR gate).

## Rollout / rollback

- **Rollout is additive to CI:** the `live-vm` job is *rewritten* (from inert to
  real) and a new `live-vm-tcg` job is added; no other job's behavior changes. New
  files: `scripts/live-vm/preflight-env.sh` + its test, the workflow-shape guard,
  ADR-0389, this spec, the plan, the operator CI notes. Any new `KDIVE_*` the
  preflight introduces is added to `external_env.py` (env-docs guard); the vars it
  *reads* are already documented (A/B/C).
- **Enabling order (operator, runbook):** the self-hosted runner service stays
  stopped until D's `if:` guard is merged **and** the "require approval for outside
  collaborators" repo setting is applied; only then `github_runner_service_enabled:
  true`. Merging D does not by itself expose the runner — enabling the service does.
- **Rollback:** revert the `ci.yml` change (the job returns to inert
  `workflow_dispatch`-only) and leave the runner service disabled; the preflight
  script and tests are inert without the job. No migration, no data, nothing to
  undo beyond the workflow and the two script/test files.
