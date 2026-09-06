"""Tests for provider-neutral external-boot artifact bounds."""

from typing import cast

import pytest

from kdive.providers.ports.external_boot import ArtifactSource, BundleSource, InitrdSource
from kdive.providers.shared.external_boot_bounds import source_byte_limit

_DIGEST = "sha256:" + "11" * 32


def test_source_byte_limit_uses_the_closed_initrd_size() -> None:
    source = InitrdSource(key="initrd", version="v1", sha256=_DIGEST, size_bytes=123)

    assert source_byte_limit(source) == 123


def test_source_byte_limit_rejects_an_unknown_source_type() -> None:
    with pytest.raises(TypeError, match="unsupported external-boot artifact source"):
        source_byte_limit(cast(ArtifactSource, object()))


def test_source_byte_limit_bounds_a_kernel_bundle_independently_of_claimed_size() -> None:
    source = BundleSource(
        key="bundle",
        version="v1",
        sha256=_DIGEST,
        vmlinuz_sha256=_DIGEST,
        member_count=1,
        uncompressed_bytes=1,
        vmlinuz_size_bytes=1,
        decoded_kernel_size_bytes=1,
        elf_metadata_bytes=1,
        gnu_build_id_size_bytes=4,
    )

    assert source_byte_limit(source) > source.uncompressed_bytes
