# ADR 0389 — CI wiring for the live-test tiers

- **Status:** Accepted <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
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

Rewrite the inert `live-vm` job and add a `live-vm-tcg` job; add a family-keyed
fail-loud env preflight (`scripts/live-vm/preflight-env.sh`) in the
`scripts/live-vm/lib.sh` `die`/`require_*` idiom. Four decisions:

1. **Hosted TCG is nightly + `workflow_dispatch` + `push`-to-`main`, not a PR
   gate.** The job runs on `ubuntu-latest` (no `/dev/kvm`; TCG), stands up the
   compose backends + host processes + S3, stages the ppc64le set via C's
   `stage-tcg-images.sh`, and runs the four `live_vm_tcg` spine proofs under an
   **explicit `timeout-minutes` derived from a measured boot-to-panic wall-time**
   (recorded below). A minutes-scale, variable, full-stack-bring-up job on every
   PR is the "slower theater" the epic's risk section warns against; nightly +
   on-`main` gives default-branch regression coverage and `workflow_dispatch`
   proves it green on the introducing PR, without taxing every contributor.
   Promotion to a PR gate later is a one-line trigger change with no job-body
   change, so the choice is reversible once real wall-time/flake data justify it.

2. **The self-hosted nightly stands up the provisioned-System family on the box.**
   The job brings up the compose backends + host processes on the runner (which
   already has libvirt), provisions one System from the warm rootfs, and exports
   `KDIVE_LIVE_VM_SYSTEM_ID`; `KDIVE_S3_*` + credential material ride repo/org
   secrets into `KDIVE_SECRETS_ROOT`. Self-contained: nothing external must stay
   healthy between nightly runs. It reuses the **persistent `/opt/kdive` venv**
   via `KDIVE_PYTHON` (never a throwaway `uv sync` in `$GITHUB_WORKSPACE` — the
   libguestfs `.so` symlinks the worker's `guestfs` import needs live only in that
   venv, B's explicit D constraint), and runs **both** families (`-m "live_vm and
   not live_vm_tcg"`): throwaway from the warm rootfs, provisioned against the
   in-job System.

3. **A family-keyed fail-loud preflight, not per-test skips, gates job readiness.**
   `preflight-env.sh <family…>` asserts the *requested* families' env is present
   before pytest runs and **fails the job** (non-zero, names the missing var) when
   it is not — including the S3 credential *material* under `KDIVE_SECRETS_ROOT`
   for the provisioned family, the gap A's resolver leaves (A checks only the
   `KDIVE_S3_*` env, per its docstring). A per-test `pytest.skip` is correct for an
   *unrequested* capability (A's gates); the preflight is correct for a *declared*
   family a job intends to run, so a mis-provisioned runner fails loud instead of
   skipping to green.

4. **No fork-PR exposure: `if: github.event_name != 'pull_request'` on both jobs.**
   A self-hosted runner cannot distinguish a trusted PR from a fork's at the
   `runs-on` layer, so the guard excludes *all* PRs (fork and same-repo) and relies
   on `schedule`/`workflow_dispatch`/`push` for coverage; `KDIVE_S3_*` secrets ride
   those trusted events only. This is the code control that closes B's deferred RCE
   window; the paired platform control — the repo "Require approval for all outside
   collaborators" setting — is a runbook-ordered operator step applied *before*
   `github_runner_service_enabled: true`. Merging D does not expose the runner;
   enabling the service does, and only after both controls are in place.

**Measured wall-time (grounds Decision 1's timeout):** _to be recorded from the
local TCG proof before this ADR is marked Accepted in the PR — the
CI-cannot-prove-it-live posture A/B/C shipped._

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

- The self-hosted job carries two subtle host contracts: it must reuse
  `/opt/kdive`'s venv (not build a throwaway one) and, on a `workflow_dispatch` of
  a non-`main` ref, update that checkout to the dispatched ref before running — a
  documented, bounded step so branch dispatch and the venv-reuse constraint both
  hold.
- The hosted TCG job maps C's `KDIVE_LIVE_VM_ROOTFS` output onto the spine's
  `KDIVE_GUEST_IMAGE_PPC64LE` and supplies `KDIVE_KERNEL_SRC` from the
  fetch-kernel-tree fixture — an explicit job step, asserted by the preflight, so a
  missed mapping fails loud rather than skipping.
- The timeout is a *measured* number that must be revisited if the emulator, the
  proof set, or the guest RAM changes; the ADR records the first measurement, the
  same operator-live-proof posture the sibling ADRs shipped.
- Two live jobs mean two things ordinary hosted PR CI cannot prove; the change's
  own guardrails are `actionlint`/`zizmor` (the fork-PR guard + pins), `shellcheck`
  on the preflight, the preflight behavioral test, and a workflow-shape guard that
  pins the `if:` exclusion at the source.

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
  `/opt/kdive` via `KDIVE_PYTHON` is the host contract B built.
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
