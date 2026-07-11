"""Tests for local-libvirt staged artifact writes."""

from __future__ import annotations

from pathlib import Path

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.lifecycle.staged_write import write_staged_bytes


def test_write_staged_bytes_replaces_destination_without_leaving_part_file(tmp_path: Path) -> None:
    dest = tmp_path / "kernel"
    dest.write_bytes(b"old")

    write_staged_bytes(dest, b"new kernel")

    assert dest.read_bytes() == b"new kernel"
    assert not dest.with_name("kernel.part").exists()


def test_write_staged_bytes_removes_part_file_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "kernel"

    def fail_replace(self: Path, target: Path) -> None:
        del self, target
        raise OSError("replace failed")

    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(CategorizedError) as caught:
        write_staged_bytes(dest, b"new kernel")

    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert caught.value.details == {"op": "stage", "dest": str(dest)}
    assert not dest.exists()
    assert not dest.with_name("kernel.part").exists()
