# 0571 — Every job in every workflow declares a job-level timeout-minutes

## Status

Accepted (2026-08-21)

## Context

ADR-0566 bounded the CI apt install with a hard timeout and a retry, and recorded one more
decision alongside them: **"Every job in a workflow that installs packages declares
`timeout-minutes`"** (0566, Decision). That scope held when the record was written — the guard
then read only the four package-installing workflows.

#1983 generalized the rule and PR #1992 implemented it: the guard now enumerates every workflow
file under `.github/workflows`, checks every job in each, and the test is renamed
`test_every_job_in_every_workflow_declares_a_timeout`. PR #2014 (#1994) hardened the same test
twice: a declared value must be a positive number — `timeout-minutes: 0` gets a job cancelled
the moment it starts, so it bounds nothing — and a job that exempts itself on a local reusable
workflow `uses:` callee must have that callee resolve to one of the enumerated workflow files,
so a typo'd or removed callee can no longer exempt a job against nothing.

The consequence of leaving ADR-0566 as it stands is not cosmetic. The guard's own docstring
cited ADR-0566 as the authority for the provenance half of the convention, and a reader who
followed that citation to check the rule's scope got the narrower, superseded answer: the record
says package-installing workflows, the tree enforces every workflow.

The same paragraph of ADR-0566 also miscounts its own suite — "Four of its tests are static" and
"The other six run the script" (:137, :144). The file held five and five before #1992 and holds
five and five now. The miscount cannot be corrected in place: any correction removes lines, and
`check_sections_append_only` in `.github/scripts/check-records.sh` rejects removed lines outside
`## Status`. It can only be superseded, which makes this record the route for that too.

A new record rather than an in-place amendment is deliberate, and the route was settled by the
issue author on #1995: the stale sentence is a decision statement, and appending "and now every
workflow" inside an Accepted record would change a decision's scope with no record of who
decided it or why.

## Decision

**Every job in every workflow declares job-level `timeout-minutes`, as a positive number below
the 360-minute GitHub Actions default.** The 360-minute default is a property of GitHub Actions,
not of any one step: a job that wedges on a registry push, an emulated build, or an apt mirror
alike burns the same six hours unless a smaller bound is declared. A value at or above the
default is refused as well as a non-positive one — at the default the job is exactly as
wedgeable as before the declaration, and on a hosted runner nothing above it is enforceable.

**The value is sized from the job's observed runtime plus headroom, with the observed figure
written in a comment beside it.** Only presence and a real bound are mechanised by the guard;
the provenance half — that the declared figure traces to an observation — is held by review.
Where a job runs the apt script, its sizing carries the script's ~11-minute worst case; a job
with no apt step does not carry that term.

**A job that calls a reusable workflow with `uses:` is exempt, and loudly so.** GitHub and
actionlint reject `timeout-minutes` on such a job, so requiring it here would deadlock the first
reusable-workflow caller against the repo's own lint gate. The exemption is not taken on trust:
the callee is resolved against the repository root, as GitHub resolves it, and must be one of
the workflow files the guard enumerates. Anything else reddens the guard — a local callee that
names no real file, and an out-of-repo `owner/repo/…` callee alike, which resolves to no
enumerated file and is refused outright rather than silently exempting the job's check
(PR #2014). Introducing a remote reusable-workflow caller therefore means extending the guard
first.

`tests/guards/test_apt_install_is_bounded.py` owns the wiring. **Five of its tests are static**:
no workflow calls `apt-get` directly, every package-installing workflow reaches the shared
script, the retry shape still matches `pull-test-images.sh`, the `justfile` owns the command
text, and the live budget override reaches the script. **The other five run the script**:
against a stub `apt-get` that hangs on `update`, one that exits non-zero, one that succeeds on
the first attempt, one that lets `update` pass and hangs on `install`, and one that rejects a
zero or malformed budget. This corrects ADR-0566's four/six count.

#1983 is the decision; PR #1992 implemented the generalization and renamed the test; PR #2014
added the positive-integer floor and the local-callee resolution.

## Consequences

The enforced rule and the record that states it now agree: a reader who follows the guard's
citation reaches a record describing every workflow, not four of them. ADR-0566's apt bounding
and retry decision stands unchanged; only its timeout-guard scope decision — the sentence at
:123 and the descriptive paragraph at :137–144 that repeats it, including the four/six
miscount — is superseded by this record, and ADR-0566 carries the supersession banner in its
Status section.

New workflows are covered without further record-keeping: the guard enumerates the workflow
files it checks, so a workflow added tomorrow is inside the rule with no edit to this record or
the guard. The job-count floor in the test goes slack as jobs are added by design; it is a
tripwire against a silently shrinking check, not a coverage assertion.

The reusable-workflow exemption is scoped, not absolute: the jobs inside a reusable workflow
this repo calls are checked when the guard reads the callee's own file. A caller of an
out-of-repo callee cannot exist on this branch today — the guard refuses any `uses:` value that
does not resolve to an enumerated workflow file — so admitting one is a deliberate future
change that must extend the guard with it, and decide then how those jobs' timeouts are
bounded.

## Considered & rejected

**An append-only amendment to ADR-0566**, the route PR #1972 used for ADR-0430 (#1942).
Rejected: the stale sentence is a decision statement, not a description. Widening an Accepted
record's Decision in place would change the scope of a decision with no record of who decided
it or why, and a later reader could not distinguish the amendment from the original judgement.
The descriptive drift at :137–144 is downstream of the same decision, so amending the
description while leaving the decision would split them; and the four/six miscount is
uncorrectable in place regardless, because correcting it removes lines that
`check_sections_append_only` rejects.

**Leaving ADR-0566 untouched and relying on the guard's failure messages**, which since #1992
already tell a tripped reader that the rule covers every workflow. Rejected as the whole fix: a
reader arriving via a red check is not misdirected, but a reader arriving at ADR-0566 directly —
or following the guard's citation to it — still gets the narrower rule. A citation chain must
terminate at a record that states the current scope.
