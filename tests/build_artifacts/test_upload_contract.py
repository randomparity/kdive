"""The public external-build upload contract (#769, ADR-0234 §5).

These tests are the drift guard: they assert the advertised byte contract is derived from the
validator's own constants, and that the hand-encoded ``requirement`` values match what
``validate_external_artifacts`` actually enforces. If the validator's magic/layout/caps change and
the contract is not re-derived, these fail.
"""

from __future__ import annotations

import json

import pytest

from kdive.artifacts.storage import HeadResult
from kdive.artifacts.uploads import ManifestEntry
from kdive.build_artifacts import validation
from kdive.build_artifacts.validation import (
    EFFECTIVE_CONFIG_MAX_BYTES,
    EXTERNAL_BUILD_CONTRACTS,
    MagicPin,
    validate_external_artifacts,
)
from kdive.domain.errors import CategorizedError
from kdive.mcp.tools.catalog.artifacts.uploads import RUN_ARTIFACT_NAMES


class _UntouchedStore:
    """A store that fails loudly if used — proves a code path returns before any object read."""

    def head(self, key: str) -> HeadResult | None:
        raise AssertionError(f"store.head must not be called (key={key!r})")

    def get_range(self, key: str, *, start: int, length: int) -> bytes:
        raise AssertionError(
            f"store.get_range must not be called (key={key!r}, start={start}, length={length})"
        )


def test_contracts_cover_exactly_the_run_artifact_names() -> None:
    """Every accepted run upload name has a contract, and no contract names an unknown artifact."""
    assert set(EXTERNAL_BUILD_CONTRACTS) == set(RUN_ARTIFACT_NAMES)


def test_kernel_byte_contract_derives_from_validator_constants() -> None:
    kernel = EXTERNAL_BUILD_CONTRACTS["kernel"]
    # The combined-tar container is gzip — magic taken from the validator's own constant.
    assert kernel.format.magic == (MagicPin(offset=0, hex=validation._GZIP_MAGIC.hex()),)
    # Layout member paths are the validator's literal constants (<release> hint lives in `note`).
    paths = {member.path for member in kernel.layout}
    assert validation._KERNEL_BOOT_MEMBER in paths
    assert validation._MODULES_MEMBER_PREFIX in paths
    # boot/vmlinuz carries the bzImage HdrS magic at the validator's offset.
    boot = next(m for m in kernel.layout if m.path == validation._KERNEL_BOOT_MEMBER)
    assert boot.format is not None
    assert boot.format.magic == (
        MagicPin(offset=validation._BZIMAGE_MAGIC_OFFSET, hex=validation._BZIMAGE_MAGIC.hex()),
    )


def test_vmlinux_byte_contract_uses_elf_magic_and_notes_build_id() -> None:
    vmlinux = EXTERNAL_BUILD_CONTRACTS["vmlinux"]
    assert vmlinux.format.magic == (MagicPin(offset=0, hex=validation._ELF_MAGIC.hex()),)
    # The build_id dependency is surfaced (it is a runs.complete_build argument, not an artifact).
    assert "build_id" in " ".join(vmlinux.notes)


def test_effective_config_cap_is_the_shared_constant() -> None:
    cap = EXTERNAL_BUILD_CONTRACTS["effective_config"].format.max_bytes
    assert cap == EFFECTIVE_CONFIG_MAX_BYTES


def test_requirement_values_encode_adr_0234() -> None:
    assert EXTERNAL_BUILD_CONTRACTS["kernel"].requirement == "required"
    assert EXTERNAL_BUILD_CONTRACTS["vmlinux"].requirement == "optional"
    assert EXTERNAL_BUILD_CONTRACTS["initrd"].requirement == "optional"
    assert EXTERNAL_BUILD_CONTRACTS["effective_config"].requirement == "conditional"


def test_kernel_required_matches_validator_behavior() -> None:
    """A manifest with no kernel is rejected before any object read — proves `kernel` is required.

    The validator early-returns at its kernel check, so the store is never touched. The vmlinux
    build_id dependency and kernel-only-accepted are already proven by the local_libvirt validation
    suite, which owns the heavy combined-tar/ELF fixtures.
    """
    with pytest.raises(CategorizedError):
        validate_external_artifacts(
            _UntouchedStore(),
            manifest=[ManifestEntry(name="initrd", sha256="x", size_bytes=1)],
            keys={"initrd": "tenant/runs/x/initrd"},
            declared_build_id=None,
        )


def test_to_json_is_json_serializable() -> None:
    for contract in EXTERNAL_BUILD_CONTRACTS.values():
        # Must not raise: every nested value is a JSON primitive, not a dataclass.
        json.dumps(contract.to_json())
