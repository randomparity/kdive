"""The served external-build doc must state the kernel-config contract (#993).

The upload validator constrains artifact *structure* (bzImage magic, gzip layout, a
``lib/modules`` member, build-id match) and never which Kconfig symbols were enabled — so the
kernel config is the agent's to choose. Stating this keeps an agent from assuming it cannot get,
say, a KASAN kernel on the build lane. This guard keeps the contract from being dropped by a
later edit.

The assertion reads the **served snapshot** an agent receives over MCP, so it also fails if
the packaged snapshot falls out of sync with the source doc.
"""

from __future__ import annotations

from pathlib import Path

from kdive.mcp.resources.registrar import DOC_RESOURCES

_CONTENT_DIR = Path(__file__).resolve().parents[3] / "src/kdive/mcp/resources/_content"


def _served(name: str) -> str:
    entry = next(e for e in DOC_RESOURCES if e.name == name)
    return (_CONTENT_DIR / entry.content_file).read_text(encoding="utf-8")


def test_external_build_doc_states_config_is_yours_to_choose() -> None:
    body = _served("external-build-upload")
    lowered = body.lower()
    assert "kasan" in lowered, (
        "served external-build doc gives no debug-config example (KASAN) (#993)"
    )
    assert "structure" in lowered, (
        "served external-build doc does not say the validator constrains structure only (#993)"
    )
    # The whole point: no Kconfig symbol is disallowed — the config is the agent's choice.
    assert "symbol" in lowered, (
        "served external-build doc does not say the validator never restricts config symbols (#993)"
    )
