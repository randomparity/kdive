"""Tests for provider-neutral external-boot artifact bounds."""

from typing import cast
from uuid import UUID

import pytest
from pydantic import ValidationError

from kdive.build_artifacts import validation
from kdive.providers.ports.external_boot import (
    ArtifactSource,
    BundleSource,
    DebuginfoSource,
    InitrdSource,
)
from kdive.providers.shared.external_boot_bounds import (
    materialization_reservation_bytes,
    source_byte_limit,
)
from tests.support.external_boot_plan import external_boot_plan

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


def _debuginfo(size_bytes: int) -> DebuginfoSource:
    return DebuginfoSource(key="vmlinux", version="v1", sha256=_DIGEST, size_bytes=size_bytes)


def test_source_byte_limit_uses_the_closed_debuginfo_size() -> None:
    assert source_byte_limit(_debuginfo(4096)) == 4096


def test_reservation_counts_the_debuginfo_size() -> None:
    plan = external_boot_plan(UUID(int=1), UUID(int=2))
    with_debuginfo = plan.model_copy(update={"debuginfo": _debuginfo(4096)})

    assert (
        materialization_reservation_bytes(with_debuginfo) - materialization_reservation_bytes(plan)
        == 4096
    )


def test_debuginfo_bound_matches_completion_bound() -> None:
    bound = validation._EXTERNAL_BOOT_DEBUGINFO_MAX_BYTES  # noqa: SLF001

    assert _debuginfo(bound).size_bytes == bound
    with pytest.raises(ValidationError):
        _debuginfo(bound + 1)
