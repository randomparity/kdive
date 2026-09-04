---
title: A Proposed ADR cited from src/ or tests/ fails adr-status-check, so the Accepted flip must land in the same commit
date: 2026-09-04
tags: [adr, ci-guard, commit-ordering, tdd, mid-pr-red]
components: [scripts/guards/check_adr_status.py, docs/adr/README.md, justfile]
---

## Problem

`just ci` goes red in the middle of a PR that is otherwise progressing normally, on:

```
ADR NNNN: status is Proposed but it is cited in src/ or tests/ — the citation asserts the
decision is implemented
```

The failure appears at the first commit that cites a new ADR and persists for every commit after
it, until the commit that flips the ADR's `Status`. On a change developed test-first this is easy
to walk into: the natural ordering is *write the ADR as Proposed → implement → cite it → ratify at
the end*, and every commit between "cite it" and "ratify" is red.

The trap is sharper than it looks, for two reasons.

**`tests/` counts, not just `src/`.** The obvious reading of "shipped" is production code, so an
author who cites the ADR from a test first — which TDD makes the default — trips a guard they
believe applies only to `src/`.

**It is invisible until CI or a bare `just ci`.** Nothing in the pre-commit hook set runs this
guard, so local commits report clean.

## Root cause

`scripts/guards/check_adr_status.py` enforces two invariants; the second is this one:

> **No shipped-but-Proposed drift.** No ADR whose status keyword is `Proposed` is cited in
> production source (`src/`) or in the test suite (`tests/`). A citation there means the decision
> is implemented — including guard-type ADRs whose enforcement ships purely as tests, never as
> `src/` code — so the ADR should have been advanced to Accepted (or superseded).

The `tests/` half is deliberate and documented in that docstring: some ADRs are enforced *only* by
tests, so restricting the check to `src/` would let those ship as Proposed forever.

The rule the guard implements comes from `docs/adr/README.md`:

> Open it as **Proposed**. An ADR moves to **Accepted** when the pull request that implements its
> decision merges — the implementing PR *is* the ratification. Update the ADR's `Status` in that
> same PR, so status never drifts from reality.

The guard is stricter than "same PR" in practice, because CI runs per commit: the flip and the
first citation must be in the **same commit**, or every commit in between is red.

## Solution

**Order the commits so the `Status` flip and the first citation from either tree are one commit.**
Make it step zero of the implementation task, not a tidy-up at the end.

Verify the guard's behaviour directly rather than trusting the reading — it takes two runs:

```
# with the ADR still Proposed and cited from src/ or tests/
just adr-status-check ; echo "exit=$?"      # exit=1

# after flipping Status to Accepted in the same tree
just adr-status-check ; echo "exit=$?"      # exit=0
```

Run it bare. The guard is stdlib-only and fast, so there is no reason to batch it behind a full
`just ci`.

**For a decision staged across several PRs**, `docs/adr/README.md` gives the escape hatch: cite the
**tracking issue number** in `src/` and `tests/` for the intermediate PRs instead of the ADR
number. The PR that flips `Status` to Accepted is the one that adds the ADR's citations across both
trees, in that same change.

## Prevention

- Make "flip `Status` to Accepted in the same commit as the first citation" an explicit first step
  of any task that introduces an ADR, and state it in the PR body so a reviewer can check the
  ordering rather than rediscovering it.
- Run `just adr-status-check` bare immediately before committing the first citation. It is the
  cheapest guard in the suite and the one most likely to be tripped by ordering rather than by
  content.
- When a task's plan says "ratify the ADR at the end", treat that as a defect in the plan. There is
  no valid intermediate state where the ADR is cited and still Proposed.
