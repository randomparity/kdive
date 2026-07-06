"""Op-time resolution of the operator ``guest_egress`` opt-in (#1031, ADR-0313)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from kdive.providers.local_libvirt.config import local_guest_egress_for_resource

_BLOCK = """
schema_version = 2

[[local_libvirt]]
name = "{name}"
cost_class = "local"
host_uri = "qemu:///system"
{egress}
"""


def _write_systems_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, name: str, egress: str
) -> None:
    path = tmp_path / "systems.toml"
    path.write_text(_BLOCK.format(name=name, egress=egress))
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(path))


def test_egress_true_block_resolves_true(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_systems_toml(tmp_path, monkeypatch, name="loc", egress="guest_egress = true")
    assert local_guest_egress_for_resource("loc") is True


def test_egress_false_block_resolves_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_systems_toml(tmp_path, monkeypatch, name="loc", egress="guest_egress = false")
    assert local_guest_egress_for_resource("loc") is False


def test_egress_omitted_defaults_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_systems_toml(tmp_path, monkeypatch, name="loc", egress="")
    assert local_guest_egress_for_resource("loc") is False


def test_no_matching_block_defaults_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A name-mismatch (or no local_libvirt block for this resource) is legitimate absence, not an
    # error — the operator opt-in silently defaults off (ADR-0313: secure default).
    _write_systems_toml(tmp_path, monkeypatch, name="other", egress="guest_egress = true")
    assert local_guest_egress_for_resource("loc") is False


def test_absent_file_defaults_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(tmp_path / "does-not-exist.toml"))
    assert local_guest_egress_for_resource("loc") is False


def test_malformed_file_degrades_to_false_and_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The F1 blast-radius fix (ADR-0313 §1): the rebind seam runs for EVERY local op, and local ops
    # need no systems.toml at all, so a malformed file must NOT break an unrelated live op. It
    # degrades to the secure default (egress off) with a logged warning — never raised.
    path = tmp_path / "systems.toml"
    path.write_text("this is = = not valid toml [[[")
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(path))
    with caplog.at_level(logging.WARNING):
        assert local_guest_egress_for_resource("loc") is False
    assert any("systems.toml" in rec.getMessage() for rec in caplog.records)
