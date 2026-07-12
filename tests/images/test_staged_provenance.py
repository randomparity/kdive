"""Tests for the staged-provenance sidecar contract (#977, ADR-0296)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from kdive.images.rootfs.staged_provenance import (
    SIDECAR_SCHEMA,
    config_sibling_path,
    read_config_sibling,
    read_sidecar,
    sidecar_path,
    write_config_sibling,
    write_sidecar,
)

_PROVENANCE = {
    "plane": "local-libvirt",
    "boot_kernel_count": 1,
    "makedumpfile_version": "1.7.7",
    "packages": ["kexec-tools", "crash"],
    "package_versions": {"kexec-tools": "2.0.28"},
}


def test_sidecar_path_appends_suffix_without_dropping_qcow2() -> None:
    qcow2 = Path("/var/lib/kdive/rootfs/local/fedora-kdive-ready-43.qcow2")
    assert sidecar_path(qcow2) == Path(
        "/var/lib/kdive/rootfs/local/fedora-kdive-ready-43.qcow2.provenance.json"
    )


def test_round_trip_returns_exact_provenance(tmp_path: Path) -> None:
    qcow2 = tmp_path / "image.qcow2"
    write_sidecar(qcow2, provenance=_PROVENANCE)
    assert read_sidecar(qcow2) == _PROVENANCE


def test_written_document_is_schema_wrapped(tmp_path: Path) -> None:
    qcow2 = tmp_path / "image.qcow2"
    write_sidecar(qcow2, provenance=_PROVENANCE)
    doc = json.loads(sidecar_path(qcow2).read_text(encoding="utf-8"))
    assert doc == {"schema": SIDECAR_SCHEMA, "provenance": _PROVENANCE}


def test_write_overwrites_a_pre_existing_sidecar(tmp_path: Path) -> None:
    qcow2 = tmp_path / "image.qcow2"
    write_sidecar(qcow2, provenance={"boot_kernel_count": 3})
    write_sidecar(qcow2, provenance=_PROVENANCE)
    assert read_sidecar(qcow2) == _PROVENANCE


def test_unknown_extra_provenance_key_survives(tmp_path: Path) -> None:
    """A future operand flows through unchanged (byte cap, not a type allowlist)."""
    qcow2 = tmp_path / "image.qcow2"
    provenance = {**_PROVENANCE, "future_operand": {"nested": [1, 2, 3]}}
    write_sidecar(qcow2, provenance=provenance)
    assert read_sidecar(qcow2) == provenance


def test_absent_sidecar_returns_none_without_warning(tmp_path: Path, caplog: object) -> None:
    import pytest  # noqa: PLC0415

    assert isinstance(caplog, pytest.LogCaptureFixture)
    with caplog.at_level(logging.WARNING):
        assert read_sidecar(tmp_path / "missing.qcow2") is None
    assert caplog.records == []


def _write_raw(qcow2: Path, text: str) -> None:
    sidecar_path(qcow2).write_text(text, encoding="utf-8")


def test_malformed_json_returns_none_and_warns(tmp_path: Path, caplog: object) -> None:
    import pytest  # noqa: PLC0415

    assert isinstance(caplog, pytest.LogCaptureFixture)
    qcow2 = tmp_path / "image.qcow2"
    _write_raw(qcow2, "{not json")
    with caplog.at_level(logging.WARNING):
        assert read_sidecar(qcow2) is None
    assert caplog.records != []


def test_non_object_document_returns_none(tmp_path: Path) -> None:
    qcow2 = tmp_path / "image.qcow2"
    _write_raw(qcow2, "[1, 2, 3]")
    assert read_sidecar(qcow2) is None


def test_wrong_schema_returns_none(tmp_path: Path) -> None:
    qcow2 = tmp_path / "image.qcow2"
    _write_raw(qcow2, json.dumps({"schema": "other.v9", "provenance": _PROVENANCE}))
    assert read_sidecar(qcow2) is None


def test_missing_schema_returns_none(tmp_path: Path) -> None:
    qcow2 = tmp_path / "image.qcow2"
    _write_raw(qcow2, json.dumps({"provenance": _PROVENANCE}))
    assert read_sidecar(qcow2) is None


def test_non_object_provenance_returns_none(tmp_path: Path) -> None:
    qcow2 = tmp_path / "image.qcow2"
    _write_raw(qcow2, json.dumps({"schema": SIDECAR_SCHEMA, "provenance": [1, 2]}))
    assert read_sidecar(qcow2) is None


def test_over_cap_sidecar_rejected_without_full_read(tmp_path: Path) -> None:
    """A sidecar larger than the cap degrades to None via a bounded read."""
    qcow2 = tmp_path / "image.qcow2"
    padding = "x" * (128 * 1024)
    _write_raw(qcow2, json.dumps({"schema": SIDECAR_SCHEMA, "provenance": {"pad": padding}}))
    assert read_sidecar(qcow2) is None


def test_config_sibling_path_appends_suffix_without_dropping_qcow2() -> None:
    qcow2 = Path("/var/lib/kdive/rootfs/local/fedora-kdive-ready-43.qcow2")
    assert config_sibling_path(qcow2) == Path(
        "/var/lib/kdive/rootfs/local/fedora-kdive-ready-43.qcow2.config"
    )


def test_config_sibling_round_trip(tmp_path: Path) -> None:
    """Written config bytes read back verbatim."""
    qcow2 = tmp_path / "image.qcow2"
    config = b"CONFIG_KASAN=y\nCONFIG_DEBUG_INFO_BTF=y\n"
    write_config_sibling(qcow2, config=config)
    assert read_config_sibling(qcow2) == config


def test_read_config_sibling_absent_returns_none(tmp_path: Path) -> None:
    assert read_config_sibling(tmp_path / "image.qcow2") is None


def test_read_config_sibling_over_cap_returns_none(tmp_path: Path, caplog: object) -> None:
    """A sibling larger than the cap degrades to None via a bounded read."""
    import pytest  # noqa: PLC0415

    from kdive.images.rootfs.staged_provenance import _CONFIG_MAX_BYTES

    assert isinstance(caplog, pytest.LogCaptureFixture)
    qcow2 = tmp_path / "image.qcow2"
    config_sibling_path(qcow2).write_bytes(b"x" * (_CONFIG_MAX_BYTES + 1))
    with caplog.at_level(logging.WARNING):
        assert read_config_sibling(qcow2) is None
    assert "exceeds" in caplog.text


def test_read_config_sibling_unreadable_returns_none(tmp_path: Path, caplog: object) -> None:
    """A directory at the sibling path (unreadable as a file) degrades to None, not a raise."""
    import pytest  # noqa: PLC0415

    assert isinstance(caplog, pytest.LogCaptureFixture)
    qcow2 = tmp_path / "image.qcow2"
    config_sibling_path(qcow2).mkdir()
    with caplog.at_level(logging.WARNING):
        assert read_config_sibling(qcow2) is None
