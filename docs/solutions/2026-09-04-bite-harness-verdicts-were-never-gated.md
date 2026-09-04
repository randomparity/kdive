---
title: Fault-injection harnesses reported bite/no-bite verdicts that no exit code ever gated
date: 2026-09-04
tags: [lying-parser, test-tooling, fault-injection, exit-codes, pytest, false-evidence]
components: [out-of-tree agent bite-injection harnesses, scripts/mutate.py, justfile]
---

## Problem

Three agents each built a fault-injection ("bite proof") harness to verify that a new test
actually fails when the code it covers is broken: commit the fix, inject a fault, observe a red,
revert, confirm the file is byte-identical. Between them they reported roughly ninety passing
bite results.

Every printed report looked right. None of them was gated on anything a machine checked.

The defect surfaced only when each author was asked one question:

> What does your harness exit when every fault behaves exactly as expected?

The answers were `1`, `1`, and `0` — where the correct answer is `0`, `0`, and `0`. Two harnesses
could never report success by exit code; one could never report failure. In all three cases the
author had been reading printed text and treating it as a gate result.

Four distinct mechanisms were found, in three independently written tools:

**1. The pipeline ate the exit status.** The verdict line was

```sh
pytest ... | grep -E ... | head -10
```

A shell pipeline returns the exit status of the *last* command, so the status was `head`'s — always
`0`. The script then ended in `echo` statements, so even that was discarded. **No verdict was ever
computed.** This is the `set -o pipefail` trap, inside the tool built to enforce evidence.

**2. The control's expected outcome counted as a failure.** The harness ran a deliberate no-op
"control" fault and asserted it reported `NO BITE`. That correct result was tallied in the same
`failures` counter as a real problem, so the harness exited `1` on a completely successful run.

**3. Exit `1` was overloaded.** One harness distinguished three outcomes in printed text — bite,
no-bite, and "collection error, this proves nothing" — but mapped the last two onto the same exit
code. The classification existed only in the output a human read.

**4. The classifier's fallback branch emitted a valid-looking verdict.** Two harnesses
pattern-matched pytest output with the shape:

```python
verdict = "BITES" if (returncode != 0 and clean and not collected_error) else "NO BITE"
```

Anything the pattern did not recognise became `NO BITE`. Both had *different, non-overlapping*
blind spots:

- One required seven spaces of indentation in the `E   ...` line; pytest emitted three.
- One enumerated assertion shapes only — `AssertionError`, `assert`, `DID NOT RAISE`,
  `did not match` — so a failure *raised by the code under test* matched nothing. Its own most
  important bite failed with
  `E   ValueError: cleanup tombstone directory contains unexpected payload` and was classified
  `NO BITE`.
- The other missed `E   Failed: ...`, which is how both `pytest.fail()` and an unraised
  `pytest.raises` render.

Both hit their fallback **live**, on a real bite, and both were caught only because a human found
`NO BITE` implausible on that particular test and re-read the raw output.

## Root cause

Two layers.

**The specific one:** a parser whose unrecognised-input branch produces a *valid verdict* does not
fail — it lies. `NO BITE` and "I could not understand this output" are different facts, and
collapsing them converts a parse failure into recorded evidence that a test does not bite. A
no-op control cannot catch this: the control only proves the harness does not fire *spuriously*,
which is the opposite direction from a false negative.

**The general one, which is the transferable part:**

> The matcher was checked against the inputs it happened to see, rather than against the contract
> that generates them.

Each classifier was written by running it against whatever pytest output the author had in front
of them at the time, and it handled exactly those shapes. None was derived from pytest's documented
outcome model. No enumeration of observed shapes converges — that is why two independent authors
produced two non-overlapping narrowings of the same grammar.

The same class then appeared one layer out, in an orchestrator's merge gate. Its handshake
selector was `startswith("MERGE-READY")`, checked against the *idea* of a handshake rather than the
workflow text that defines one — which specifies the handshake as a line **inside** a
`WORK:TRAJECTORY` comment, and that comment opens with an HTML marker. The selector therefore had
**zero true positives**: it could only ever match a malformed handshake, and reported "not found"
for correctly formed ones. A gate that returns "not found" for well-formed input is the same defect
as a harness that returns `NO BITE` for an injection that never happened. Both fail by looking like
they worked.

## Solution

All three harnesses converged independently on the same contract. Implement it directly rather than
rediscovering it:

- **Derive the verdict from pytest's exit code first, content second.** Exit code is the outcome;
  output text only refines *which kind* of failure it was.
- **Unrecognised output is an ERROR that withholds the verdict — never a fallback verdict.**
  Distinct codes: `0` bite, `1` no-bite, `2`/`3` harness error. Treat pytest's exit `5`
  ("no tests collected") as an error, not a no-bite.
- **An injection anchor that does not match is an error, never a verdict.** Assert
  `count(old_text) == 1` before editing and abort with the anchor text in the message. Anchors go
  stale constantly — a reorder changes indentation, `ruff format` rewrites `except (A, B):` to
  `except A, B:` under PEP 758, a test gets renamed. Every one of those must surface as
  `FAULT SETUP FAILED`, not as `NO BITE`.
- **Report `BITE(assert)` and `BITE(raise)` distinctly.** A failure raised from the code under test
  is a legitimate bite; conflating it with an assertion hides which shapes the classifier handles.
- **Take the expected verdict as an argument** (`EXPECT=bite|no-bite`) and map "matched expectation"
  to `0`, so a fault run and a control run gate identically and no counter needs a special case.
- **Exit non-zero if the restore is not byte-identical.** Verify with `sha256sum` against a
  filesystem copy taken before injection — never `git checkout --`, which silently discards
  unrelated work.
- **Derive paths from `git rev-parse`,** never hard-code a worktree path. Agent worktrees get
  relocated, and a hard-coded path makes every anchor miss at once.
- **Run the harness bare.** No `| tail`, no `| head`, no `>/dev/null`, no `|| true`. To capture
  output *and* keep the status, use `set -o pipefail` (note: on zsh `${PIPESTATUS[0]}` is empty —
  the array is `pipestatus` and is 1-indexed).

## Prevention

**Self-test the harness in four directions before trusting a single result from it.** Three
directions are the obvious ones; the third is the one everybody skips and the only one that proves
the gate catches a *wrong* verdict:

| self-test | expected exit |
| --- | --- |
| known bite, expected `bite` | `0` |
| control (comment-only edit), expected `no-bite` | `0` |
| **known bite, expected `no-bite`** | **non-zero** |
| anchor absent | error code, and no verdict printed |

A gate that only ever confirms what you expected cannot fail, which makes it indistinguishable
from a gate that does nothing.

**The generalisable rule, which applies to any output-matching code, not just test harnesses:**

> Read the contract that generates the input, write one case per shape it permits, and make
> anything else an error. A parser that falls back to a valid verdict on unrecognised input does
> not fail; it lies.

For pytest specifically, the contract is its documented exit codes (`0` passed, `1` tests failed,
`2` interrupted, `3` internal error, `4` usage error, `5` no tests collected) — not the text of the
`E   ...` lines, which is formatting and will change.

No lint rule catches this class. The cheap detector is the question that found all four instances
here, and it costs one command: **run the tool on a clean tree with nothing wrong and check what it
exits.**
