# ADR 0389 — CI wiring for the live-test tiers

- **Status:** Proposed <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-07-19
- **Deciders:** Maintainer (randomparity), Claude Code

## Context

Epic #1289 (directional ADR-0386) built a `live_vm` harness (A, ADR-0386), a
self-hosted KVM runner (B, ADR-0387), and the guest-image stores (C, ADR-0388),
but left the CI job that runs them inert: `.github/workflows/ci.yml`'s `live-vm`
job is `workflow_dispatch`-only, sets no env, stages no image, and runs `pytest
-m live_vm` — which with no env skips every test and reports **green**. The
"green run that is no coverage" is the failure the epic exists to kill.

Sub-issue D (#1293) wires two real gates and the fail-loud preflight that keeps
them honest. Three things the epic spec and the sibling ADRs deliberately
deferred to D must be decided here:

1. **The hosted TCG gate's trigger.** ADR-0386 left "PR-gate vs nightly … chosen
   on the measured wall-time / flake rate" explicitly open. A ppc64le
   boot-to-panic under TCG is minutes-scale and variable (the spine's own
   reachability deadline is 900 s per proof, #1144), and the job first stands up
   the full compose stack.
2. **How the self-hosted nightly stands up the provisioned-System family.**
   ADR-0388 (C) names this "the open C/D detail … live stack + S3 on the box, or
   an externally provisioned System." B's runner has libvirt and the warm rootfs;
   the provisioned-System family (kdump/install) needs a real System +
   `KDIVE_S3_*`.
3. **The fork-PR security posture.** B (ADR-0387) installed the runner service
   **stopped** because self-hosted + fork PR is arbitrary code execution, and
   deferred the workflow `if:` guard that closes that window to D.

## Decision

Move the two live gates into a **new `.github/workflows/live.yml`** (removing the
inert `live-vm` job from `ci.yml` — replace, not deprecate) and add a family-keyed
fail-loud env preflight (`scripts/live-vm/preflight-env.sh`) in the
`scripts/live-vm/lib.sh` `die`/`require_*` idiom. A separate workflow is required
because GitHub triggers are workflow-level: a `schedule` on `ci.yml`'s shared `on:`
would fire every existing job nightly, and `live.yml` carrying **no `pull_request`
trigger at all** makes fork-PR dispatch structurally impossible. Four decisions:

1. **Hosted TCG is nightly + `workflow_dispatch` + `push`-to-`main`, not a PR
   gate.** The `tcg` job runs on `ubuntu-latest` (no `/dev/kvm`; TCG), stands up the
   compose backends + host processes + S3, stages the ppc64le set via C's
   `stage-tcg-images.sh`, and runs the **three core `live_vm_tcg` spine proofs**
   (`ssh_reachable`, `kdump_captures`, `fadump_captures`) under an **explicit
   `timeout-minutes` = ⌈measured boot-to-panic wall-time × 1.5⌉ floored at 30 min**
   (the concrete headroom rule; the measured number is recorded below). The fourth
   marked proof (`uploaded_kernel_bundle_boots`, #1146) needs a separately-built
   ppc64le bundle no repo script produces, so it is an operator opt-in
   (`KDIVE_PPC64LE_BUNDLE`), explicitly scoped — not a silent green skip. A
   minutes-scale, variable, full-stack-bring-up job on every PR is the "slower
   theater" the epic's risk section warns against; nightly + on-`main` gives
   default-branch regression coverage and `workflow_dispatch` proves it green on the
   introducing PR, without taxing every contributor. Promotion to a PR gate later is
   a one-line trigger addition with no job-body change, reversible once real
   wall-time/flake data justify it.

2. **The self-hosted nightly stands up the provisioned-System family on the box.**
   `scripts/live-stack/up.sh --skip-obs` brings up the compose backends (the on-box
   MinIO is the object store, authenticated with the `minioadmin` default
   `env.sh` supplies — not a repo secret; external S3 is out of scope for the
   nightly) + host processes on the runner (which already has libvirt), then a new
   `scripts/live-vm/mint-system.sh` funds/onboards a project (the omitted-otherwise
   prerequisite — `up.sh` cannot allocate against an unfunded project), mints a
   token, allocates, provisions from the warm rootfs, polls to `ready`, and prints
   the System id captured into `KDIVE_LIVE_VM_SYSTEM_ID`. Self-contained: a pre-job
   reaper (not trap cleanup) is what guarantees nothing external survives between
   nightly runs. Because each Actions `run:` is a fresh shell, the stand-up →
   mint → preflight → suite steps run as **one `run:` block** so the sourced/minted
   env survives to the suite (not lost between steps). It
   uses the **persistent `/opt/kdive` venv as the interpreter** (`KDIVE_PYTHON`)
   and overlays the tested sources via `PYTHONPATH=$GITHUB_WORKSPACE/src` — it does
   **not** mutate `/opt/kdive`'s checkout (no git update, no re-sync), because the
   libguestfs `.so` symlinks the worker's `guestfs` import needs live only in that
   venv (B's explicit D constraint) and a re-sync or a feature-branch update would
   drop them or bleed a stale ref into the next nightly. Both families run (`-m
   "live_vm and not live_vm_tcg"`): throwaway from the warm rootfs, provisioned
   against the in-job System. **Host-dep parity:** `up.sh` needs `docker` + the
   compose plugin, which B's roles do not provision, so D declares them as a
   `live_vm_host` dependency in the same change (the runner is cattle).

3. **A family-keyed fail-loud preflight, not per-test skips, gates job readiness.**
   `preflight-env.sh <family…>` asserts the *requested* families' env is present
   before pytest runs and **fails the job** (non-zero, names the missing var) when
   it is not. For the provisioned family the teeth over A is **`KDIVE_LIVE_VM_SYSTEM_ID`
   non-empty**: A's resolver returns `AVAILABLE` on `KDIVE_S3_ENDPOINT_URL` +
   `KDIVE_S3_BUCKET` alone, so a family declared but with no System minted (a
   `mint-system.sh` failure) skips green — the preflight fails loud. (The `AWS_*`
   creds are the on-box `minioadmin` default on this path, so a credential-absence
   check would be vacuous; it has teeth only on the `tcg` job, which exports the
   presigned-upload creds explicitly and does not source `env.sh`.) A per-test
   `pytest.skip` is correct for an *unrequested* capability (A's gates); the
   preflight is correct for a *declared* family a job intends to run, so a
   mis-provisioned runner fails loud instead of skipping to green.

4. **No fork-PR exposure — three layers.** (a) *Structural:* `live.yml` carries
   **no `pull_request` trigger**, so no PR event — fork or same-repo — can dispatch
   either job. (b) *Code:* the self-hosted job's `if:` is a **positive allowlist**,
   `github.event_name == 'schedule' || github.event_name == 'workflow_dispatch'` —
   deliberately *not* `!= 'pull_request'`, which would also admit `push` and fire
   the RCE-sensitive, minutes-scale self-hosted boot on every commit to `main`. (c)
   *Platform:* the repo "Require approval for all outside collaborators" setting, a
   runbook-ordered operator step applied *before* `github_runner_service_enabled:
   true`. The S3 credentials ride trusted events only. This closes B's deferred RCE
   window; merging D does not expose the runner — enabling the service does, and
   only after all three controls are in place.

**Measured wall-time (grounds Decision 1's timeout):** not yet recorded. The
shipped `timeout-minutes` is the **30-minute floor** of the `⌈measured × 1.5⌉,
floor 30` rule. A local measurement was attempted on the x86_64 KVM dev host
(the ppc64le fixture and a kernel tree are present), but a clean full-stack
bring-up for the emulated boot could not be completed in-session without
extensive stack orchestration tangential to this change; grounding the number is
therefore **deferred to the first operator nightly on the enabled runner** (the
same CI-cannot-always-prove-it-live posture A/B/C shipped, where the live proof is
an operator step). The ADR stays **Proposed** until that first run records the
number; the 30-minute floor is a safe default until then (the per-proof
reachability deadline is 900 s, so three core proofs fit within it comfortably).

## Consequences

Easier:

- The product's boot→crash→introspect boundary finally has an automated gate: the
  hosted TCG tier nightly + on-`main`, the self-hosted native tier nightly.
- A mis-provisioned runner fails loud with the exact missing var, instead of a
  green run that proves nothing — the epic's founding failure mode is closed at
  the job boundary, not left to per-test skips.
- Enabling the self-hosted runner is now safe: the `if:` guard exists, so an
  operator can apply the repo setting and flip `github_runner_service_enabled`.
- Promotion of the hosted TCG tier to a PR gate is a trigger-only change if the
  measured wall-time and flake rate later justify it.

Harder / new obligations:

- The self-hosted job reuses `/opt/kdive`'s venv as the *interpreter* and overlays
  the tested sources via `PYTHONPATH=$GITHUB_WORKSPACE/src` — never mutating
  `/opt/kdive`. The one bound: the overlay assumes the tested ref adds no PyPI
  dependency absent from that venv; the preflight asserts `kdive` imports under the
  overlay, so a dependency drift fails loud at the job boundary rather than
  mis-testing.
- D declares `docker` + the compose plugin as a `live_vm_host` Ansible dependency
  (the parity obligation for the on-box stack bring-up), so a reprovisioned cattle
  runner is stack-capable — an edit to B's role that ships with D.
- The provisioned family needs a new `scripts/live-vm/mint-system.sh` (fund/onboard
  → token → allocate → provision → poll-ready → print id); minting the System is a
  real deliverable, not folded into "allocate → provision", and funding is a stated
  prerequisite, not an assumption.
- The reused `scripts/live-stack/{lib.sh,onboard.sh}` hardcode the workspace
  `.venv` / `uv run` and ignore `KDIVE_PYTHON`, so as-is they would run the
  provisioning worker (which imports `guestfs`) under a non-libguestfs interpreter.
  D gives them a **backward-compatible `KDIVE_PYTHON` override** (default unchanged
  when unset) so the on-box stack runs under the libguestfs venv; the worker runs
  non-root (`KDIVE_WORKER_AS_ROOT=0`, the runner user in the `libvirt` group) to
  keep `sudo` from stripping the inherited `KDIVE_PYTHON`/`PYTHONPATH`.
- The hosted TCG job maps C's `KDIVE_LIVE_VM_ROOTFS` output onto the spine's
  `KDIVE_GUEST_IMAGE_PPC64LE` and supplies `KDIVE_KERNEL_SRC` from the
  fetch-kernel-tree fixture — an explicit job step, asserted by the preflight, so a
  missed mapping fails loud rather than skipping.
- The timeout is a *measured* number that must be revisited if the emulator, the
  proof set, or the guest RAM changes; the ADR records the first measurement, the
  same operator-live-proof posture the sibling ADRs shipped.
- `live.yml`'s two jobs use **distinct per-job** concurrency groups with
  `cancel-in-progress: false` (a running self-hosted boot is never killed
  mid-flight, and a long native run does not cross-block the hosted push-to-`main`
  gate), paired with a pre-job reaper that **destroy-then-`undefine
  --remove-all-storage`s** orphaned `kdive-*` domains (so shut-off definitions and
  their disks do not leak on the non-ephemeral runner) and tears down a stale
  stack. The reaper's `^kdive-` match is host-wide, so the runner topology assumes
  one runner per libvirt host (runbook-documented).
- The self-hosted job runs pytest **directly under `KDIVE_PYTHON`** (not
  `just test-live`), so the reused `/opt/kdive` interpreter — with `drgn` +
  `guestfs` — is the one that runs the suite, and no `just` need be provisioned on
  the runner.
- Two live jobs mean things ordinary hosted PR CI cannot prove; the change's own
  guardrails are `actionlint`/`zizmor` (pins + posture), `shellcheck` on the
  preflight, the preflight behavioral test, and a workflow-shape guard that pins the
  no-`pull_request`-trigger + positive-allowlist + `cancel-in-progress: false`
  posture at the source.

No database migration; CI/test infrastructure only.

## Alternatives considered

- **Hosted TCG as a required PR gate.** Rejected (Decision 1): a minutes-scale,
  variable, full-stack + TCG-emulated-boot job on every PR taxes every contributor
  and invites the flaky-theater rot the epic warns against, for regression
  coverage that nightly + on-`main` already gives. Kept reversible: promotion is a
  one-line trigger change.
- **Externally provisioned System by id for the self-hosted family.** Rejected
  (Decision 2): supplying a long-lived System id as a runner secret is lighter per
  run but depends on external state staying healthy between nightly runs — more rot
  risk, and a stale System fails green-ish (the System resolves but is dead). The
  on-box stand-up is self-contained and reproducible from the warm rootfs each
  night.
- **Throwaway family only for now; defer provisioned-System.** Rejected: the epic's
  roadmap criterion is that the self-hosted KVM nightly *must* run the
  provisioned-System family natively (kdump/install on real silicon) — that native
  depth is why the KVM box exists and emulated TCG does not substitute for it.
  Wiring only the throwaway family would ship the box's reason-to-exist as a TODO.
- **A throwaway `uv sync` in `$GITHUB_WORKSPACE` for the self-hosted job.**
  Rejected: it gets `drgn` but not the libguestfs symlinks, so the worker's
  `guestfs` import fails at test time — the exact trap B's spec flagged. Reusing
  `/opt/kdive`'s venv as the interpreter is the host contract B built.
- **Mutating `/opt/kdive` to the dispatched ref (git-update + re-sync per job).**
  Rejected (Decision 2): it makes the persistent checkout shared mutable state — a
  feature-branch dispatch bleeds a stale ref into the next `schedule` run, two
  different-ref dispatches interleave (the concurrency group keys on `github.ref`,
  so they are not serialized), and a re-`uv sync` can drop the hand-placed
  libguestfs symlinks. The `PYTHONPATH` overlay reuses the venv without mutating it,
  so none of these arise.
- **Two new jobs in `ci.yml` with a shared `schedule` and per-job `if:` guards.**
  Rejected (Decision 4 / rollout): GitHub triggers are workflow-level, so a
  `schedule` on `ci.yml` fires every existing job nightly unless each is guarded —
  a wide, error-prone edit whose "no other job changes" claim is false. A separate
  `live.yml` with its own `on:` isolates the triggers and, carrying no
  `pull_request`, makes fork-PR dispatch structurally impossible.
- **The shared `ci.yml` `cancel-in-progress: true` for the self-hosted job.**
  Rejected: cancelling a self-hosted job mid-boot orphans a libvirt domain, the
  provisioned System, the compose stack, and a held `flock` (a non-ephemeral runner
  runs no trap on SIGKILL). `live.yml` uses `cancel-in-progress: false` + a pre-job
  reaper instead.
- **Per-test skips instead of a fail-loud preflight.** Rejected (Decision 3): A's
  per-test gates correctly skip an *unrequested* capability, but a nightly that
  declares a family and then skips it green is the founding failure. The preflight
  fails a *declared* family's missing env at the job boundary — including the
  credential material A's resolver does not check.
- **Rely on the `runs-on` label alone for fork-PR safety.** Rejected (Decision 4):
  the label routes a job to the runner but does not gate which *events* dispatch to
  it; only the `if:` guard (code) plus the repo approval setting (platform) close
  the fork-PR RCE window. Defense in depth, both required.
- **Fold the CI wiring back into B or C.** Rejected: the epic sequences D after C
  so the image inputs and the host exist before a job is wired onto them; the same
  boundary ADR-0387/0388 drew. D owns `.github/workflows` and the preflight; A/B/C
  own the harness, host, and stores.
