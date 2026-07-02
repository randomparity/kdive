"""introspect, postmortem, and control must have allowlisted, linked toolset guides (#995).

These three high-value toolsets had no allowlisted doc resource, so an agent driving stages
7-8 of the typical session had no reachable guide for the non-halting introspection path, the
crash-triage path, or how to deliberately induce a crash. This guard ties the deliverable to
the registrar (each guide is served) and to the agent index (each guide is linked), so a later
edit cannot drop a guide or its link with CI still green.

The per-namespace completeness guard (``test_toolset_doc_completeness``) additionally checks
each guide names exactly its live tools.
"""

from __future__ import annotations

from pathlib import Path

from kdive.mcp.resources.registrar import DOC_RESOURCES

_REPO_ROOT = Path(__file__).resolve().parents[3]
_NEW_TOOLSETS = ("introspect", "postmortem", "control")


def test_new_toolset_guides_are_allowlisted() -> None:
    served = {e.name for e in DOC_RESOURCES}
    for namespace in _NEW_TOOLSETS:
        assert f"toolset-{namespace}" in served, (
            f"{namespace} toolset guide is not registered as a doc resource (#995)"
        )


def test_new_toolset_guides_are_linked_from_agent_index() -> None:
    body = (_REPO_ROOT / "docs/guide/agent-index.md").read_text(encoding="utf-8")
    for namespace in _NEW_TOOLSETS:
        assert f"toolsets/{namespace}.md" in body, (
            f"agent-index does not link the {namespace} toolset guide (#995)"
        )
