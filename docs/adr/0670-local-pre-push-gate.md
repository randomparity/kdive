# 0670 — Automatic local pre-push gate

## Status

Accepted (2026-09-20)

## Context

Issue #2582 identifies a manual-only pre-push gate. Existing pre-commit hooks
lint and format; none runs the full suite. CI already validates uv.lock and
runs actionlint and zizmor, but container-arch-check is local-ci-only.

## Decision

Install both pre-commit and pre-push through prek. Keep existing hooks at the
pre-commit stage and add one pre-push-only local hook that runs `just ci`
without filenames, even when no changed file matches. The justfile owns the
check list; there is no separate push-specific copy.

This checks the current local checkout under prek's ordinary working-tree
semantics. It is not an attestation of the pushed SHA or every ref in a push.
Use a clean checked-out branch and avoid concurrent edits for useful evidence.
Deletion-only/no-op pushes may run no checks. Hooks remain bypassable with
`--no-verify`, `SKIP=pre-push-ci`, removal, or an unconfigured clone; remote
enforcement remains an operator/CI concern.

## Consequences

Eligible pushes run the full existing local gate, including container-arch-check,
at its existing minutes-long cost. Commit-time work and fast-loop recipes stay
unchanged. Existing installations must rerun `just install-hooks`. Tool/service
requirements and test skips are those of `just ci`, including Docker-dependent
tests that skip when Docker is unavailable. A green local gate is not a live-tier
proof. A remembered manual full run does not suppress the automatic run.

## Considered & rejected

- **Only cheap checks or test-changed.** verified: ADR-0420 retains the full suite
  as the pre-push gate; its name-based selection does not cover transitive callers.
- **Keep manual-only verification.** judgment: fails the requested automatic gate.
- **Isolate and attest each pushed commit.** judgment: adds checkout, environment
  and multi-ref orchestration beyond the approved local-checkout contract.
- **Remote enforcement instead.** judgment: does not close the requested local
  push-time gap and belongs to separately owned repository policy.
