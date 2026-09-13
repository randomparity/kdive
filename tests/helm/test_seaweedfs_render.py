from __future__ import annotations

import subprocess


def _render(*args: str) -> str:
    return subprocess.run(
        ["helm", "template", "kdive", "deploy/helm/kdive", *args],
        check=True,
        text=True,
        capture_output=True,
    ).stdout


def test_bundled_render_uses_pinned_seaweedfs() -> None:
    rendered = _render("--set", "bundledBackends=true", "--set", "demoAcknowledged=true")
    assert (
        "ghcr.io/randomparity/kdive-seaweedfs@sha256:6a4e9f013ecd9c1f86136ed3d3c7eb089c8eb13ff3eee83044ab2a2950f7ce6c"
        in rendered
    )
    assert "-seaweedfs:8333" in rendered


def test_external_s3_render_omits_bundled_seaweedfs() -> None:
    rendered = _render("--set", "bundledBackends=false")
    assert "-seaweedfs" not in rendered
