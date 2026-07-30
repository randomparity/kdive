# 0505 — Workflow action pins are uniform, SHA-pinned, and truthfully labelled

## Status

Accepted (2026-07-29)

## Context

Every `uses:` in `.github/workflows/` is pinned to a 40-hex commit SHA with the human-readable
version in a trailing comment — `uses: actions/checkout@3d3c42e5… # v7.0.1`. The SHA is what
GitHub resolves; the comment is what a reader (and a reviewer) actually looks at. Nothing kept
the two in step.

A grouped dependabot bump (#1699) moved 15 of the 16 `actions/checkout` call sites to v7.0.1 and
rewrote the sixteenth — `records.yml` — to v7.0.0's SHA while leaving its comment reading
`# v6.0.3`. The pin therefore named a major version it was not, and hid a v6 → v7 jump behind a
stale annotation. Every check on that PR was green: `actionlint` does not read version comments,
and `zizmor` audits permissions and injection, not comment accuracy. The defect was invisible to
CI by construction.

The enabling cause is worth recording. That site was pinned to `9f698171…`, which is not a commit
at all — it is the **annotated tag object** for v6.0.3, dereferencing to commit `df4cb1c0…`.
Dependabot identifies a pin's current version by reverse-looking-up the SHA against the action's
tags; a tag-object SHA does not appear in that mapping, so it rewrote the SHA and left the comment
untouched.

The obvious guard — resolve each SHA against the GitHub API and compare it to the comment — is not
available to us: CI gates here are hermetic and offline (`tests/image/` even runs with
`--noconftest`, without the project installed), and a gate that needs a network round-trip per pin
is both flaky and rate-limited. We need an invariant that is checkable from the tree alone.

## Decision

We will enforce three invariants over `.github/workflows/` from the tree alone, as a test guard:

1. Every `uses:` naming a remote action is pinned to a 40-hex commit SHA — never a tag or branch.
2. Every such pin carries a trailing `# <version>` comment.
3. All call sites of the same action name the **same** SHA and the **same** version comment.

Invariant 3 is what makes the other two load-bearing without network access. We cannot verify from
the tree that `3d3c42e5…` *is* v7.0.1, but we can verify that the repository never disagrees with
itself about it — and a bump that rewrites some sites and not others, or rewrites a SHA without its
comment, necessarily creates exactly that disagreement. #1699 fails invariant 3 on the first run.

## Consequences

A partial or mislabelled grouped bump now fails `just ci` instead of merging green. The failure
message names the action and every distinct pin, so the fix is mechanical.

The cost is that pinning one workflow to an older version of an action becomes a deliberate act:
the guard has no allowlist, so a genuine need (say a self-hosted runner stuck on an older Node)
means editing the guard in the same PR, with the reason in the diff. We consider that the right
default — today every action is uniform tree-wide, and the one site that was not uniform was not
uniform on purpose. It was the bug.

This guard cannot detect a SHA and comment that are *both* wrong in the same direction at every
site simultaneously, because it checks self-consistency rather than upstream truth. Verifying a
pin against upstream stays a review-time and dependabot-time concern.

## Considered & rejected

**Resolve each SHA against the GitHub API.** The only check that proves a comment true. Rejected:
it makes a core gate depend on network reachability and an authenticated, rate-limited API, and it
would fail closed on a GitHub outage. It also cannot run in the `--noconftest`, no-project
context the sibling pin guard uses.

**Require the comment to match `vMAJOR.MINOR.PATCH`.** Would have flagged nothing here — `# v6.0.3`
is well-formed; it was merely false. It would also break `extractions/setup-just # v4`, a
legitimate major-only pin.

**Ban annotated-tag-object pins specifically.** Addresses the enabling cause, and is checkable
offline only by length/shape — which a tag object shares with a commit (both 40-hex). Not
distinguishable from the tree, so it cannot be a hermetic guard.

**Let `zizmor`/`actionlint` cover it.** Neither reads version comments; `zizmor`'s
`unpinned-uses` audit checks that a SHA pin exists, not that its label is honest. Both stay in the
chain for what they do cover.
