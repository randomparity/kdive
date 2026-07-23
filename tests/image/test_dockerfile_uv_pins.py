"""Guard that every uv pin in the Dockerfile names one version (ADR-0359).

amd64/arm64 copy the uv binary out of ``ghcr.io/astral-sh/uv``; ppc64le has no such
upstream image and installs the same version as a wheel from PyPI instead. The two
carriers must agree, or a ppc64le image ships a different uv than the amd64/arm64 images
built from the same commit — a silent per-arch resolver difference.

Nothing enforced that before: dependabot's docker ecosystem rewrites ``FROM`` image
references but never a version pinned inside a ``RUN``, so every grouped bump moved the
astral image and left the ppc64le wheel behind. This test is the guardrail that turns that
drift into a build failure.

Stdlib + pytest only: ``tests/image/`` is collected with ``--noconftest`` in CI, without
the project installed.
"""

from __future__ import annotations

import re
from pathlib import Path

_DOCKERFILE = Path(__file__).resolve().parents[2] / "Dockerfile"

#: ``FROM ghcr.io/astral-sh/uv:0.11.29@sha256:...`` — the amd64/arm64 binary provider stages.
_IMAGE_PIN = re.compile(r"^FROM\s+ghcr\.io/astral-sh/uv:(?P<version>[^@\s]+)", re.MULTILINE)
#: ``RUN pip install --no-cache-dir uv==0.11.29`` — the ppc64le wheel fallback.
_WHEEL_PIN = re.compile(r"\buv==(?P<version>[^\s\"']+)")


def _dockerfile() -> str:
    return _DOCKERFILE.read_text(encoding="utf-8")


def test_uv_pins_are_discoverable() -> None:
    # A rename or refactor that stops matching would make the parity assertion below pass
    # vacuously, so pin the expected shape of the Dockerfile first.
    text = _dockerfile()
    assert _IMAGE_PIN.findall(text), f"no astral-sh/uv FROM pin found in {_DOCKERFILE}"
    assert _WHEEL_PIN.findall(text), f"no `uv==` wheel pin found in {_DOCKERFILE}"


def test_all_uv_pins_agree_across_arches() -> None:
    text = _dockerfile()
    versions = set(_IMAGE_PIN.findall(text)) | set(_WHEEL_PIN.findall(text))
    assert len(versions) == 1, (
        f"uv is pinned to more than one version in {_DOCKERFILE.name}: {sorted(versions)}. "
        "The astral-sh/uv image tags (amd64/arm64) and the `uv==` wheel (ppc64le) must name "
        "the same version — dependabot bumps the image pins but not the wheel."
    )
