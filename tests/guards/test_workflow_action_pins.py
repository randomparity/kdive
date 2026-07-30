"""Guard: every workflow action pin is a SHA, labelled, and identical everywhere (ADR-0505).

`uses:` lines pin a 40-hex commit SHA and carry the human-readable version in a trailing comment.
GitHub resolves the SHA; reviewers read the comment. Nothing kept the two in step, so a grouped
dependabot bump (#1699) rewrote `records.yml`'s `actions/checkout` to v7.0.0's SHA while leaving
its comment reading `# v6.0.3` — a hidden v6 → v7 major jump, with every check green.

`actionlint` does not read comments and `zizmor` audits permissions and injection, so neither can
see a lying label. The check that would settle it — resolve each SHA against the GitHub API — needs
network and auth, which a hermetic gate cannot have.

So this guards **self-consistency** instead of upstream truth: the tree may not disagree with
itself about which SHA and which version an action is pinned to. That is enough, because a bump
that rewrites some call sites and not others — or a SHA without its comment — necessarily creates
that disagreement. It cannot catch a pin whose SHA and comment are both wrong the same way at
every site at once; upstream verification stays a review-time concern (ADR-0505).

Scope: `.github/workflows/` only. There are no composite actions under `.github/actions/`, and
`node_modules` vendored workflows are outside the glob by construction.

Stdlib + pytest only, matching `tests/image/test_dockerfile_uv_pins.py`: this reads the tree, not
the project.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

_WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"

#: ``uses: actions/checkout@3d3c42e5… # v7.0.1`` — a fully pinned, labelled reference.
_PINNED = re.compile(
    r"^\s*(?:-\s+)?uses:\s+(?P<action>[^@\s]+)@(?P<sha>[0-9a-f]{40})\s+#\s*(?P<version>\S+)",
    re.MULTILINE,
)
#: Any ``uses:`` at all. A pin that stops matching ``_PINNED`` must not pass unnoticed.
_ANY_USES = re.compile(r"^\s*(?:-\s+)?uses:\s+(?P<ref>\S+)", re.MULTILINE)


def _workflow_files() -> list[Path]:
    return sorted([*_WORKFLOWS.glob("*.yml"), *_WORKFLOWS.glob("*.yaml")])


def test_workflows_and_uses_lines_are_discoverable() -> None:
    # A rename, a reformat, or a regex that stops matching would make every assertion below pass
    # over nothing, so pin the expected shape of the tree first.
    files = _workflow_files()
    assert files, f"no workflow files found under {_WORKFLOWS}"
    without_uses = [p.name for p in files if not _ANY_USES.search(p.read_text(encoding="utf-8"))]
    assert len(without_uses) < len(files), (
        f"no `uses:` line matched in any of {len(files)} workflow files — the regex or the "
        f"workflow layout changed and this guard is now vacuous (files: {without_uses})"
    )


def test_every_uses_is_sha_pinned_with_a_version_comment() -> None:
    unpinned: list[str] = []
    for path in _workflow_files():
        text = path.read_text(encoding="utf-8")
        # Compare by match offset, not text: both patterns anchor at the same line start, but
        # `_ANY_USES` stops at the SHA while `_PINNED` runs through the version comment, so their
        # matched strings never compare equal.
        pinned = {match.start() for match in _PINNED.finditer(text)}
        for match in _ANY_USES.finditer(text):
            if match.start() not in pinned:
                line = text[: match.start()].count("\n") + 1
                unpinned.append(f"{path.name}:{line}: {match.group('ref')}")
    assert not unpinned, (
        "every workflow `uses:` must pin a 40-hex commit SHA and carry a trailing version "
        f"comment (`uses: owner/action@<sha> # v1.2.3`). Offenders: {unpinned}"
    )


def test_each_action_is_pinned_to_one_sha_and_one_version() -> None:
    pins: dict[str, set[tuple[str, str]]] = defaultdict(set)
    sites: dict[str, list[str]] = defaultdict(list)
    for path in _workflow_files():
        text = path.read_text(encoding="utf-8")
        for match in _PINNED.finditer(text):
            action, sha, version = match.group("action", "sha", "version")
            pins[action].add((sha, version))
            line = text[: match.start()].count("\n") + 1
            sites[action].append(f"{path.name}:{line} {sha[:8]} {version}")
    assert pins, "no pinned `uses:` found — see the discoverability guard above"
    divergent = {action: sorted(sites[action]) for action, seen in pins.items() if len(seen) > 1}
    assert not divergent, (
        "an action is pinned to more than one SHA/version across .github/workflows/, so the tree "
        "disagrees with itself about which version it runs. A grouped bump that rewrote only some "
        "call sites — or rewrote a SHA without its comment — lands here (ADR-0505). "
        f"Divergent: {divergent}"
    )
