"""The images toolset must have an allowlisted, linked selection guide (#996).

Nothing in the agent-facing doc set pointed an agent at `images.describe`'s `capability_signals`
before provisioning, so a multi-kernel or non-kdump image could burn an allocation. This guard
ties the deliverable to the registrar (the guide is served) and to the agent index (the
"define and provision" stage links it and steers to `images.describe`), so a later edit cannot
drop the guidance with CI still green.

The per-namespace completeness guard (``test_toolset_doc_completeness``) additionally checks the
guide names exactly the live ``images.*`` tools.
"""

from __future__ import annotations

from pathlib import Path

from kdive.mcp.resources.registrar import DOC_RESOURCES

_REPO_ROOT = Path(__file__).resolve().parents[3]


def test_images_guide_is_allowlisted() -> None:
    served = {e.name for e in DOC_RESOURCES}
    assert "toolset-images" in served, "images toolset guide is not registered (#996)"


def test_agent_index_steers_to_capability_check_before_provision() -> None:
    body = (_REPO_ROOT / "docs/guide/agent-index.md").read_text(encoding="utf-8")
    assert "toolsets/images.md" in body, "agent-index does not link the images guide (#996)"
    assert "images.describe" in body, (
        "agent-index does not steer to images.describe before provisioning (#996)"
    )
