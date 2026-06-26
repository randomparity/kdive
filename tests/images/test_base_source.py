"""Tests for the dual base-source acquirer (ADR-0250)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.base_source import acquire_base
from kdive.images.rootfs_catalog import CloudImageSource, VirtBuilderSource


def _unused(*args: object, **kwargs: object) -> None:
    raise AssertionError("seam should not be invoked")


def test_cloud_image_sha256_match(tmp_path: Path) -> None:
    data = b"qcow2-bytes"
    src = CloudImageSource(url="https://x/y.qcow2", sha256=hashlib.sha256(data).hexdigest())

    def dl(url: str, dest: Path) -> None:
        dest.write_bytes(data)

    acquire_base(
        src,
        tmp_path / "scratch",
        releasever="44",
        arch="x86_64",
        virt_builder=_unused,
        downloader=dl,
    )


def test_cloud_image_sha256_mismatch_fails_closed(tmp_path: Path) -> None:
    src = CloudImageSource(url="https://x/y.qcow2", sha256="0" * 64)  # pragma: allowlist secret

    def dl(url: str, dest: Path) -> None:
        dest.write_bytes(b"other")

    with pytest.raises(CategorizedError) as e:
        acquire_base(
            src,
            tmp_path / "s",
            releasever="44",
            arch="x86_64",
            virt_builder=_unused,
            downloader=dl,
        )
    assert e.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert e.value.details["reason"] == "base_sha256_mismatch"


def test_unreachable_url_named(tmp_path: Path) -> None:
    src = CloudImageSource(
        url="https://x/missing.qcow2",
        sha256="0" * 64,  # pragma: allowlist secret
    )

    def dl(url: str, dest: Path) -> None:
        raise FileNotFoundError("404")

    with pytest.raises(CategorizedError) as e:
        acquire_base(
            src,
            tmp_path / "s",
            releasever="44",
            arch="x86_64",
            virt_builder=_unused,
            downloader=dl,
        )
    assert e.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert e.value.details["reason"] == "base_unreachable"
    assert "missing.qcow2" in str(e.value.details)


def test_virt_builder_source_invokes_template(tmp_path: Path) -> None:
    calls: dict[str, str] = {}

    def vb(*, template: str, output: Path) -> None:
        calls["t"] = template
        Path(output).write_bytes(b"x")

    acquire_base(
        VirtBuilderSource(template="fedora-43"),
        tmp_path / "s",
        releasever="43",
        arch="x86_64",
        virt_builder=vb,
        downloader=_unused,
    )
    assert calls["t"] == "fedora-43"
