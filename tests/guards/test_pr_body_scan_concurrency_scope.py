"""Guard: `pr-body-scan`'s concurrency group cannot span both its trigger types (#2507).

The workflow is a required status check that fires on `pull_request` (opened, edited, reopened,
synchronize, ready_for_review) and on `issue_comment` (created, edited), and it used one shared
`concurrency.group` keyed only by PR/issue number with `cancel-in-progress: true`. Any comment
posted on a pull request — including a required `WORK:*` annotation, which by construction cites
the pushed head and so lands *after* the body edit it accompanies — cancelled an in-flight
`pull_request`-triggered scan of that same PR, and vice versa. The cancelled run left the required
check with no successful run for the head SHA, and the pull request sat unmergeable until someone
re-ran it by hand.

The fix scopes the group by `github.event_name` in addition to the PR/issue number, so a
`pull_request` run and an `issue_comment` run for the same PR occupy separate groups and can no
longer cancel each other. Redundant runs of the *same* trigger type (two rapid pushes, two rapid
comments) still land in the same group and keep today's dedup-by-cancellation.

This cannot evaluate a GitHub Actions expression, so it renders the group template with the two
known substitutions the trigger types produce (`event_name`, `github.event.pull_request.number ||
github.event.issue.number`) and checks the *rendered* groups rather than the source string alone,
which would pass on any token order that happens to mention `event_name` without actually
separating the two trigger types.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "pr-body-scan.yml"

#: The two expressions the group template may reference; substituted per simulated event below.
_EVENT_NAME = re.compile(r"\$\{\{\s*github\.event_name\s*\}\}")
_NUMBER = re.compile(
    r"\$\{\{\s*github\.event\.pull_request\.number\s*\|\|\s*github\.event\.issue\.number\s*\}\}"
)


def _group_template() -> str:
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    return workflow["concurrency"]["group"]


def _render(template: str, *, event_name: str, number: int) -> str:
    """Render the group template as GitHub would for one simulated event.

    `||` picks the first non-null operand; a `pull_request` event has no `github.event.issue`, and
    an `issue_comment` event has no `github.event.pull_request`, so in both cases the same `number`
    is what the real expression resolves to for one given pull request.
    """
    rendered = _NUMBER.sub(str(number), template)
    return _EVENT_NAME.sub(event_name, rendered)


def test_workflow_is_discoverable() -> None:
    assert _WORKFLOW.is_file(), f"expected workflow file at {_WORKFLOW}"


def test_concurrency_group_separates_pull_request_and_issue_comment_runs() -> None:
    template = _group_template()
    pr_group = _render(template, event_name="pull_request", number=2507)
    comment_group = _render(template, event_name="issue_comment", number=2507)
    assert pr_group != comment_group, (
        "pr-body-scan's concurrency group is identical for a pull_request-triggered run and an "
        "issue_comment-triggered run on the same PR, so posting a comment cancels an in-flight "
        "body-edit scan (or vice versa) and can leave the required check with no successful run "
        f"for the head SHA (#2507). group template: {template!r}"
    )


def test_concurrency_group_still_deduplicates_same_trigger_runs() -> None:
    # The fix must not lose today's redundant-run cancellation within one trigger type: two rapid
    # pushes, or two rapid comments, on the same PR still share a group.
    template = _group_template()
    first = _render(template, event_name="pull_request", number=2507)
    second = _render(template, event_name="pull_request", number=2507)
    assert first == second, (
        "two pull_request-triggered runs on the same PR no longer share a concurrency group; "
        "redundant runs of the same trigger type should still cancel each other"
    )


def test_group_template_has_no_expression_beyond_the_two_known_substitutions() -> None:
    # _render only ever substitutes `github.event_name` and the PR/issue-number `||` expression;
    # anything else is passed through as identical literal text, which would make the previous
    # test pass even if the group grew a per-run-unique field (`github.run_id`,
    # `github.run_attempt`, ...) that GitHub itself would vary between two "same trigger type"
    # runs and that would silently stop them from ever landing in the same group — the opposite
    # of what that test claims to guard. Asserting no `${{` marker survives both substitutions
    # catches that class of regression directly, independent of the leftover expression itself.
    template = _group_template()
    rendered = _render(template, event_name="pull_request", number=2507)
    assert "${{" not in rendered, (
        "the concurrency group template contains a GitHub Actions expression beyond the two this "
        "test renders (github.event_name and the pull_request/issue number). A leftover expression "
        "here is untested by the dedup assertion above and, if it varies per run (github.run_id, "
        "github.run_attempt), would silently defeat same-trigger-type deduplication. "
        f"rendered group: {rendered!r}"
    )


def test_cancel_in_progress_is_unchanged() -> None:
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    assert workflow["concurrency"]["cancel-in-progress"] is True
