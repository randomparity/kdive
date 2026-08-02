"""External S3 guidance must prove exact-version reads before KDIVE starts."""

from pathlib import Path

_ROOT = Path(__file__).parents[2]
_OPERATOR_DOCS = (
    "docs/operating/install.md",
    "docs/operating/runbooks/live-stack.md",
    "docs/operating/local-stack.md",
    "docs/operating/docker-compose.md",
    "deploy/helm/kdive/README.md",
)


def test_external_s3_guidance_declares_version_read_permission() -> None:
    for relative in _OPERATOR_DOCS:
        assert "s3:GetObjectVersion" in (_ROOT / relative).read_text(), relative


def test_install_preflight_proves_an_exact_version_read() -> None:
    text = (_ROOT / "docs/operating/install.md").read_text()
    assert "get-object --version-id" in text
    assert "Do not start KDIVE until this exact-version read succeeds" in text
