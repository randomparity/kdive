# Automatic local pre-push verification

Issue #2582; decision [ADR-0670](../../adr/0670-local-pre-push-gate.md).

## Problem and scope

`install-hooks` installs only pre-commit. ADR-0420 retains the full suite as the
pre-push gate, but invoking it is manual. CI already checks the lockfile and
workflows; `container-arch-check` currently runs only inside local `just ci`.

The approved outcome is automatic local verification with truthful guarantees.
Existing owners remain: the justfile defines check selection and installation;
prek runs hooks. No caller migration, duplicated command list, or new wrapper.

## Design

Set `default_stages: [pre-commit]` so the existing hooks keep their current
commit-time behavior. Add one local `pre-push-ci` hook with `entry: just ci`,
`language: system`, `stages: [pre-push]`, `always_run: true`, and
`pass_filenames: false`. The installer passes both `--hook-type pre-commit`
and `--hook-type pre-push`; its existing `prek run -a` remains commit-only.

An eligible push means one for which prek selects a non-deletion update with
new content. Its changed-file selection must not filter the full gate. A gate
failure blocks the Git push; successful checks allow Git to continue.
Deletion-only and already-reachable/no-op pushes may not invoke the hook.

The command checks the current local checkout, with prek's normal handling of
local modifications. It neither checks out each pushed SHA nor attests every
ref in a multi-ref push. A developer should push their clean checked-out branch
and avoid concurrent edits during verification. `--no-verify`, hook removal,
and `SKIP=pre-push-ci` bypass the local gate. Neither documentation nor status
output may present local hooks as remote enforcement.

`just ci` keeps its existing prerequisite, skip, architecture and live-tier
contracts. Full-suite selection does not imply every Docker-gated test ran on
a host without Docker. The expected existing cost is minutes, not seconds;
there is no test-result cache or second full run added to this design.

## Success

- C1: the installer creates both Git hook types, and installation runs only
  the existing commit stage.
- C2: an eligible ordinary push invokes `just ci` once without filenames,
  including the existing lock, workflow and container-architecture recipes.
- C3: existing hooks retain commit-stage selection; fast-loop recipes do not change.
- C4: functional tests prove installation, stage selection, failure blocking,
  no-match pushes, deletion-only handling and the local-checkout limitation.
- C5: ADR and AGENTS guidance explain cost, bypass, prerequisites and candidate limits.

## Global Constraints

Python 3.14, managed with uv. No new dependencies or version floors.
The justfile remains the source of command definitions. Use sibling worktrees.
Do not change test-lf/test-changed, live tiers, remote enforcement, or unrelated hooks.
Plans are transient and are not committed. ADR-0670 is assigned; no index edit.

## Failure model

- Actors and deployments: local developers and coding agents using Git, just
  and installed prek; temporary local Git repositories for functional proof.
- Invariants and assets: commit feedback remains bounded; gate errors stop an
  eligible non-bypassed push; the reported coverage matches the local checkout.
- Accepted failure classes: deliberate hook bypass, non-HEAD/multi-ref pushes,
  concurrent edits, and local working/index changes prevent SHA attestation;
  the approved contract is local-checkout verification. Missing tools or services
  can stop the gate under existing recipes; Docker absence retains existing skips.
- Covered elsewhere: protected branches and remote guarantees belong to CI/operator
  policy; live proofs belong to live-test owners; fast-loop selection to ADR-0420.

## Validation

Configuration structure is checked without needing prek. Functional tests use
real Git and installed prek in pytest temporary repositories, drive the actual
installer recipe, and replace only the expensive `just ci` boundary with an
observable stub. They verify exit propagation and the content visible to the
hook, including a pushed older commit while the checkout is newer. Missing
host Git/just/prek skips only the functional tests, visibly; local acceptance
requires those tests actually run. Existing commit-hook and justfile tests
cover the surrounding workflow. Run just lint/type and doc/record guards.
Run real `just ci` once for final parity; no excluded live tiers are introduced.
