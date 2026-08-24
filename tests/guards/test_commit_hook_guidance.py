"""`AGENTS.md` names the pre-commit hooks that rewrite the tree; they have to still exist (#2062).

The guidance tells an agent to settle four specific hooks before `git commit`, because each of
them aborts the commit with the tree modified and costs a guaranteed second attempt. Naming the
hooks is what makes the advice actionable — and what makes it go stale silently when one is
renamed. That is not hypothetical here: `.pre-commit-config.yaml` already carries `ruff-check`,
which upstream ruff-pre-commit renamed from `ruff`.

This checks only that every id the prose names is still a hook in the config. Whether a hook
*mutates* is a property of the hook, not of the YAML, so it is not something a test can read;
the direction that can be checked is the one that goes wrong on a rename or a removal.
"""

from __future__ import annotations

import pathlib

import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_AGENTS = _ROOT / "AGENTS.md"
_CONFIG = _ROOT / ".pre-commit-config.yaml"

#: The hooks the pre-commit ordering paragraph tells an agent to settle first, spelled as the
#: ids `.pre-commit-config.yaml` declares — which is what makes the prose greppable against it.
_NAMED_HOOKS = ("ruff-check", "ruff-format", "end-of-file-fixer", "trailing-whitespace")

#: The sentence the paragraph opens with, used to locate it rather than search the whole file —
#: `just format` and `prek run` appear elsewhere in AGENTS.md for unrelated reasons.
_ANCHOR = "**Before `git commit` (agent guidance):**"

#: The bold lead-in that follows the guidance. Slicing to it rather than to the first blank
#: line means a routine reflow that splits the paragraph in two does not fail the guard on
#: intact guidance (#2068).
_END = "**Running the live tiers**"


def _paragraph() -> str:
    text = _AGENTS.read_text(encoding="utf-8")
    assert _ANCHOR in text, "the pre-commit ordering guidance is gone from AGENTS.md (#2062)"
    after = text.split(_ANCHOR, 1)[1]
    assert _END in after, (
        f"the guidance's closing anchor {_END!r} is gone from AGENTS.md, so this guard can no "
        "longer bound its slice and would silently check the rest of the file (#2068)"
    )
    return after.split(_END, 1)[0]


def _hook_ids() -> set[str]:
    config = yaml.safe_load(_CONFIG.read_text(encoding="utf-8"))
    return {hook["id"] for repo in config["repos"] for hook in repo["hooks"]}


def test_every_hook_the_guidance_names_still_exists() -> None:
    ids = _hook_ids()
    paragraph = _paragraph()
    for hook_id in _NAMED_HOOKS:
        assert hook_id in paragraph, f"the guidance no longer names {hook_id!r}"
        assert hook_id in ids, (
            f"AGENTS.md tells an agent to settle {hook_id!r} before committing, but "
            ".pre-commit-config.yaml has no such hook — the advice now names nothing"
        )


def test_the_guidance_still_prescribes_an_order() -> None:
    # A paragraph that named the hooks without saying what to run about them would satisfy the
    # test above while leaving the round trip exactly where it was.
    paragraph = _paragraph()
    assert "just format" in paragraph
    assert "prek run" in paragraph
    assert "git add" in paragraph
