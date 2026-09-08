"""Guard: no workflow pushes a commit to the default branch (ADR-0633).

`changelog-sync.yml` regenerated `CHANGELOG.md` on every push to `main` and pushed the delta back
over a write deploy key. That moved the default branch a second time per merge, so every other
open pull request had to refresh its base and pay a full CI cycle before it could merge — three of
the four refreshes an eight-pull-request serial batch needed (#2337). ADR-0633 removed it and
moved the regeneration into the post-release bump pull request.

The cost was invisible in the workflow that caused it: it landed on every *other* open pull
request, which is why nothing noticed for as long as it did, and why the property is worth holding
directly rather than trusting review to catch a reintroduction.

This reads command text, so it is a proxy, not a proof. It cannot see:

- a push whose refspec is built from a variable (`git push origin "HEAD:$TARGET"`);
- a marketplace commit-and-push action, which contains no `git push` text at all — the shape a
  reacquisition would most cheaply take for as long as the `DeployKey` bypass on the protect-main
  ruleset survives (#2337);
- a command a YAML *folded* scalar (`run: >`) joins out of several lines, since the join happens
  in the runner, not here;
- an indirect invocation — `git -C <dir> push`, or a script the scan does not read: anything
  outside `.github/workflows/` and `.github/scripts/`, and any non-`.sh` file inside
  `.github/scripts/`, such as the `mermaid-check.mjs` that `docs-mermaid.yml` invokes;
- a bare `git push` in a block that has already run `git checkout main`. Flagging every
  operand-less push would redden `git push --tags` and the ordinary idiom for pushing to a
  pull-request head, so the trade is deliberate; see `_default_branch_pushes`.

It also over-reads in one direction, which is loud rather than silent: a `git push origin main`
quoted *inside* a string — an `echo` of this rule, a `grep` for it — is flagged, because nothing
here distinguishes a command from prose about one. The failure message says so.

Stdlib + pytest only, matching `tests/guards/test_workflow_action_pins.py`: this reads the tree,
not the project.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GITHUB = _REPO_ROOT / ".github"
_WORKFLOWS = _GITHUB / "workflows"
_SCRIPTS = _GITHUB / "scripts"

#: The repository's default branch, named once so the check and its failure message agree.
_DEFAULT_BRANCH = "main"

#: Any `git push`, wherever it sits on the line — the removed workflow's sat under `if !`.
_PUSH = re.compile(r"git\s+push\b(?P<args>[^\n]*)")

#: Where a push's own operands stop: a trailing comment, or the next command on the line.
#: Without this, `git push origin HEAD:$B  # never main` and `… && echo "not main"` both read
#: as pushes to the default branch.
_OPERAND_END = re.compile(r"\s#|[;&|]")

#: A `\`-continued command, as the shell joins it. Wrapping a long push over two lines is
#: ordinary formatting, not evasion, so it must not hide the refspec.
_CONTINUATION = re.compile(r"\\\n[ \t]*")

#: The `refs/heads/` a refspec may spell out; `HEAD:main` and `HEAD:refs/heads/main` are one ref.
_BRANCH_PREFIX = "refs/heads/"


def _scanned_files() -> list[Path]:
    """Workflow YAML, plus the shell scripts under `.github/scripts/` that workflows invoke.

    A `git push origin main` moved into a script (`records.yml` already calls
    `./.github/scripts/check-records.sh`) would satisfy a workflows-only scan while breaking the
    property this file is named for. Vendored `node_modules` is excluded — third-party content
    this repository does not author and does not run against its own remote.
    """
    scripts = [path for path in _SCRIPTS.rglob("*.sh") if "node_modules" not in path.parts]
    return sorted([*_WORKFLOWS.glob("*.yml"), *_WORKFLOWS.glob("*.yaml"), *scripts])


def _strip_comments(text: str) -> str:
    """Blank out whole-line `#` comments, so prose about a push is not read as one."""
    return "\n".join("" if line.lstrip().startswith("#") else line for line in text.splitlines())


def _operands(args: str) -> list[str]:
    """The push's own non-flag operands: everything before a comment or the next command."""
    end = _OPERAND_END.search(args)
    tokens = args[: end.start() if end else None].split()
    return [token for token in tokens if not token.startswith("-")]


def _destination(operand: str) -> str:
    """The branch an operand would write: the refspec's destination half, normalised.

    `main`, `HEAD:main`, `:main`, `main:main`, `+main` and `HEAD:refs/heads/main` all name the
    same branch. `HEAD:feat/main` does not — it is somebody's feature branch, and matching `main`
    as a bare word inside it reddened a push that never touches the default branch.
    """
    ref = operand.strip("'\"").rsplit(":", 1)[-1].lstrip("+")
    return ref.removeprefix(_BRANCH_PREFIX)


def _default_branch_pushes(text: str) -> list[str]:
    """Return each `git push` in *text* whose refspec names the default branch, as matched.

    A `git push` carrying no ref operand is deliberately *not* flagged: whether it reaches the
    default branch depends on the workflow's trigger and on any preceding checkout, neither of
    which this function reads, and flagging it would redden `git push --tags` and the ordinary
    idiom for pushing to a pull-request head. The module docstring lists that gap alongside the
    other shapes a text proxy cannot see.
    """
    scanned = _CONTINUATION.sub(" ", _strip_comments(text))
    return [
        match.group(0).strip()
        for match in _PUSH.finditer(scanned)
        if any(_destination(token) == _DEFAULT_BRANCH for token in _operands(match.group("args")))
    ]


def test_workflow_files_are_discoverable() -> None:
    # A rename or a moved directory would make the assertion below pass over nothing. Both halves
    # are checked: scripts alone would keep `_scanned_files()` non-empty with every workflow gone.
    assert list(_WORKFLOWS.glob("*.yml")), f"no workflow files found under {_WORKFLOWS}"
    assert list(_SCRIPTS.rglob("*.sh")), f"no shell scripts found under {_SCRIPTS}"


def test_the_detector_recognises_a_default_branch_push() -> None:
    # The three shapes `changelog-sync.yml` used before ADR-0633 removed it, plus the same push
    # wrapped over two lines. This runs against literal strings, so it stays meaningful once that
    # workflow is gone.
    caught = _default_branch_pushes(
        'if ! git push "$remote" HEAD:main; then\n'
        '            git push "$remote" HEAD:main\n'
        "          git push origin main\n"
        "          git push \\\n            origin main\n"
    )
    assert len(caught) == 4, f"the detector stopped recognising a push to main: {caught}"
    # A fully spelled-out refspec names the same branch, so it is caught too.
    assert _default_branch_pushes("git push origin HEAD:refs/heads/main\n")
    # A tag push, a branch whose name merely contains "main", a branch whose last path component
    # is "main", a bare tag push, prose about pushing, a trailing comment, and a quoted string
    # are all outside what this claims.
    assert not _default_branch_pushes(
        'git push origin "v{{VERSION}}"\n'
        "  git push origin HEAD:refs/heads/domain-work\n"
        "  git push origin HEAD:feat/main\n"
        "  git push origin main-line\n"
        "  git push --tags\n"
        "  # never git push origin main from a workflow\n"
        '  git push origin HEAD:"$BRANCH"  # never main\n'
        '  git push origin "$BRANCH" && echo "not main"\n'
    )


def test_no_workflow_pushes_to_the_default_branch() -> None:
    offenders: dict[str, list[str]] = {}
    for path in _scanned_files():
        found = _default_branch_pushes(path.read_text(encoding="utf-8"))
        if found:
            # relpath, not Path.relative_to: a symlinked scan root must name the offender,
            # not raise ValueError over where the file turned out to live.
            offenders[os.path.relpath(path, _REPO_ROOT)] = found
    assert not offenders, (
        f"a workflow — or a script one invokes — pushes to {_DEFAULT_BRANCH!r}. Such a push moves "
        "the default branch outside a pull request, which forces every open pull request through "
        "a base refresh and a full CI cycle (ADR-0633, #2337). Route the change through a "
        "reviewed pull request. If it "
        "targets a pull-request head rather than the default branch, give it an explicit refspec "
        "(`git push origin HEAD:$BRANCH`) so this guard can tell them apart. Two shapes this "
        "cannot resolve from the text, both of which need a human rather than a rewrite: a push "
        f"to a *different* repository whose default branch is also {_DEFAULT_BRANCH!r}, and a "
        "match that is prose rather than a command (an `echo` or `grep` quoting the push) — say "
        "which in review, and narrow the scan here if it is the second. "
        f"Offenders: {offenders}"
    )
