"""Fixture identity boundaries for the live VM stores (ADR-0687)."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from kdive.images.rootfs.catalog import resolve_rootfs_entry
from kdive.images.rootfs.specs import source_image_digest
from kdive.images.rootfs.staged_provenance import write_sidecar

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/live_vm_fixtures.py"
IMAGE = "rocky-kdive-ready-10"
FETCH = ROOT / "scripts/fetch-kernel-tree.sh"
BASH = shutil.which("bash") or "bash"


def _fixture_module():
    spec = importlib.util.spec_from_file_location("live_vm_fixtures", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _set(path: Path) -> None:
    path.mkdir(exist_ok=True)
    for name in ("rootfs.qcow2", "vmlinux", "vmlinux.debug"):
        (path / name).write_bytes(name.encode())
    (path / "rootfs.qcow2.config").write_bytes(b"CONFIG_KDUMP=y\n")
    write_sidecar(
        path / "rootfs.qcow2",
        provenance={
            "source_image_digest": source_image_digest(resolve_rootfs_entry(IMAGE).source),
            "package_versions": {"kernel": "6.1"},
        },
    )


def test_inputs_bind_catalog_and_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _fixture_module()
    monkeypatch.setattr(fixture, "builder_revision", lambda: "a" * 40)
    actual = fixture.inputs(IMAGE)
    assert actual["catalog"] == fixture.asdict(resolve_rootfs_entry(IMAGE))
    assert actual["builder_revision"] == "a" * 40
    assert fixture.inputs("rocky-kdive-ready-9")["catalog"] != actual["catalog"]


def test_dirty_or_unknown_builder_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _fixture_module()
    monkeypatch.setattr(
        fixture.subprocess,
        "run",
        lambda argv, **kw: SimpleNamespace(
            returncode=0, stdout=("a" * 40 if "rev-parse" in argv else "?? source.py\n")
        ),
    )
    with pytest.raises(ValueError, match="not clean"):
        fixture.builder_revision()
    monkeypatch.setattr(
        fixture.subprocess,
        "run",
        lambda argv, **kw: SimpleNamespace(returncode=0, stdout="unknown\n"),
    )
    with pytest.raises(ValueError, match="unknown builder revision"):
        fixture.builder_revision()


def test_unpinned_source_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _fixture_module()
    from kdive.images.rootfs import catalog
    from kdive.images.rootfs.catalog import VirtBuilderSource

    original = catalog.resolve_rootfs_entry
    monkeypatch.setattr(
        catalog,
        "resolve_rootfs_entry",
        lambda name: replace(original(IMAGE), source=VirtBuilderSource("example")),
    )
    with pytest.raises(ValueError, match="checksum-pinned"):
        fixture.inputs(IMAGE)


@pytest.mark.parametrize(
    "changed",
    [
        "rootfs.qcow2",
        "vmlinux",
        "vmlinux.debug",
        "rootfs.qcow2.provenance.json",
        "rootfs.qcow2.config",
    ],
)
def test_artifact_mutation_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    fixture = _fixture_module()
    monkeypatch.setattr(fixture, "builder_revision", lambda: "a" * 40)
    _set(tmp_path)
    fixture.write(tmp_path, IMAGE, "kernel-6.1", "abcd")
    assert fixture.verify(tmp_path, IMAGE, "kernel-6.1")
    (tmp_path / changed).write_bytes(b"changed")
    assert not fixture.verify(tmp_path, IMAGE, "kernel-6.1")


def test_manifest_and_provenance_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture_module()
    monkeypatch.setattr(fixture, "builder_revision", lambda: "a" * 40)
    _set(tmp_path)
    fixture.write(tmp_path, IMAGE, "kernel-6.1", "abcd")
    manifest = tmp_path / "MANIFEST"
    good = manifest.read_bytes()
    for bad in (b"old=value\n", b"{" + b"x" * 70000, b"{}"):
        manifest.write_bytes(bad)
        assert not fixture.verify(tmp_path, IMAGE, "kernel-6.1")
    manifest.write_bytes(good)
    data = json.loads(good)
    data["artifacts"]["rootfs.qcow2"] = "0" * 64
    manifest.write_text(json.dumps(data))
    assert not fixture.verify(tmp_path, IMAGE, "kernel-6.1")


def test_symlink_artifact_and_oversize_sidecar_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture_module()
    monkeypatch.setattr(fixture, "builder_revision", lambda: "a" * 40)
    _set(tmp_path)
    artifact = tmp_path / "vmlinux"
    artifact.rename(tmp_path / "real-vmlinux")
    artifact.symlink_to("real-vmlinux")
    with pytest.raises(ValueError, match="regular file"):
        fixture.write(tmp_path, IMAGE, "kernel-6.1", "abcd")
    artifact.unlink()
    artifact.write_bytes(b"kernel")
    (tmp_path / "rootfs.qcow2.provenance.json").write_bytes(b"x" * 70000)
    with pytest.raises(ValueError, match="provenance"):
        fixture.write(tmp_path, IMAGE, "kernel-6.1", "abcd")


def test_incomplete_provenance_and_config_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture_module()
    monkeypatch.setattr(fixture, "builder_revision", lambda: "a" * 40)
    _set(tmp_path)
    sidecar = tmp_path / "rootfs.qcow2.provenance.json"
    for provenance in ({}, {"source_image_digest": "wrong", "package_versions": {}}):
        write_sidecar(tmp_path / "rootfs.qcow2", provenance=provenance)
        with pytest.raises(ValueError, match="provenance"):
            fixture.write(tmp_path, IMAGE, "kernel-6.1", "abcd")
    _set(tmp_path)
    (tmp_path / "rootfs.qcow2.config").write_bytes(b"")
    with pytest.raises(ValueError, match="config"):
        fixture.write(tmp_path, IMAGE, "kernel-6.1", "abcd")
    _set(tmp_path)
    sidecar.unlink()
    with pytest.raises(ValueError, match="provenance"):
        fixture.write(tmp_path, IMAGE, "kernel-6.1", "abcd")


def test_wrong_checkout_interpreter_fails_before_catalog_or_build(tmp_path: Path) -> None:
    fake = tmp_path / "kdive"
    fake.mkdir()
    (fake / "__init__.py").write_text("")
    env = {**os.environ, "PYTHONPATH": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "inputs", ".", IMAGE],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode != 0
    assert "source-overlay" in result.stderr


def test_fetch_parses() -> None:
    result = subprocess.run([BASH, "-n", str(FETCH)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_fetch_declares_strict_mode() -> None:
    assert "set -euo pipefail" in FETCH.read_text()


def test_fetch_is_idempotent_on_existing_tree(tmp_path: Path) -> None:
    dest = tmp_path / "linux"
    (dest / ".git").mkdir(parents=True)
    result = subprocess.run(
        [BASH, str(FETCH), str(dest)],
        env={"PATH": ""},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "idempotent" in result.stderr
