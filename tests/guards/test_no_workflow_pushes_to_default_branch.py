"""Guard: no workflow pushes a commit to the default branch (ADR-0633).

`changelog-sync.yml` regenerated `CHANGELOG.md` on every push to `main` and pushed the delta back
over a write deploy key. That moved the default branch a second time per merge, so every other
open pull request had to refresh its base and pay a full CI cycle before it could merge — three of
the four refreshes an eight-pull-request serial batch needed (#2337). ADR-0633 removed it and
moved the regeneration into the post-release bump pull request.

The cost was invisible in the workflow that caused it: it landed on every *other* open pull
request, which is why nothing noticed for as long as it did, and why the property is worth holding
directly rather than trusting review to catch a reintroduction.

This reads command text, so it is a proxy, not a proof. A push whose refspec is built from a
variable is invisible to it, and so is a marketplace commit-and-push action, which contains no
`git push` text at all — that is the shape a reacquisition would most cheaply take for as long as
the `DeployKey` bypass on the protect-main ruleset survives (#2337).

Stdlib + pytest only, matching `tests/guards/test_workflow_action_pins.py`: this reads the tree,
not the project.
"""

from __future__ import annotations

import re
from pathlib import Path

_WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"

#: The repository's default branch, named once so both patterns below stay in step.
_DEFAULT_BRANCH = "main"

#: Any `git push`, wherever it sits on the line — the removed workflow's sat under `if !`.
_PUSH = re.compile(r"git\s+push\b(?P<args>[^\n]*)")

#: `main` as a whole ref component: `main`, `HEAD:main`, `origin/main`, `:main`, `main;`.
#: Not `domain`, `maintenance`, or `main-line`.
_DEFAULT_REF = re.compile(rf"(?<![\w-]){re.escape(_DEFAULT_BRANCH)}(?![\w-])")


def _workflow_files() -> list[Path]:
    return sorted([*_WORKFLOWS.glob("*.yml"), *_WORKFLOWS.glob("*.yaml")])


def _strip_comments(text: str) -> str:
    """Blank out whole-line `#` comments, so prose about a push is not read as one."""
    return "\n".join("" if line.lstrip().startswith("#") else line for line in text.splitlines())


def _default_branch_pushes(text: str) -> list[str]:
    """Return each `git push` in *text* whose refspec names the default branch, as matched.

    A `git push` carrying no ref operand is deliberately *not* flagged: whether it reaches the
    default branch depends on the workflow's trigger, which this function does not read, and
    flagging it would redden `git push --tags` and the ordinary idiom for pushing to a
    pull-request head.
    """
    offenders: list[str] = []
    for match in _PUSH.finditer(_strip_comments(text)):
        operands = [token for token in match.group("args").split() if not token.startswith("-")]
        if any(_DEFAULT_REF.search(token) for token in operands):
            offenders.append(match.group(0).strip())
    return offenders


def test_workflow_files_are_discoverable() -> None:
    # A rename or a moved directory would make the assertion below pass over nothing.
    assert _workflow_files(), f"no workflow files found under {_WORKFLOWS}"


def test_the_detector_recognises_a_default_branch_push() -> None:
    # The three shapes `changelog-sync.yml` used before ADR-0633 removed it. This runs against
    # literal strings, so it stays meaningful once that workflow is gone.
    caught = _default_branch_pushes(
        'if ! git push "$remote" HEAD:main; then\n'
        '            git push "$remote" HEAD:main\n'
        "          git push origin main\n"
    )
    assert len(caught) == 3, f"the detector stopped recognising a push to main: {caught}"
    # A tag push, a feature branch whose name merely contains "main", a bare tag push, and
    # prose about pushing are all outside what this guard claims.
    assert not _default_branch_pushes(
        'git push origin "v{{VERSION}}"\n'
        "  git push origin HEAD:refs/heads/domain-work\n"
        "  git push --tags\n"
        "  # never git push origin main from a workflow\n"
    )


def test_no_workflow_pushes_to_the_default_branch() -> None:
    offenders: dict[str, list[str]] = {}
    for path in _workflow_files():
        found = _default_branch_pushes(path.read_text(encoding="utf-8"))
        if found:
            offenders[path.name] = found
    assert not offenders, (
        f"a workflow pushes to {_DEFAULT_BRANCH!r}. Such a push moves the default branch outside a "
        "pull request, which forces every other open pull request through a base refresh and a "
        "full CI cycle (ADR-0633, #2337). Route the change through a reviewed pull request. If it "
        "targets a pull-request head rather than the default branch, give it an explicit refspec "
        "(`git push origin HEAD:$BRANCH`) so this guard can tell them apart. "
        f"Offenders: {offenders}"
    )
