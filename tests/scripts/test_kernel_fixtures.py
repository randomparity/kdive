"""Pinned fixture provenance must bind the retained bytes and selected inputs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import kernel_fixtures as fixture


def _outputs(root: Path) -> None:
    for name in fixture.REQUIRED["x86_64"]:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
    (root / "modules.order").write_text("drivers/block/loop.ko\n")
    (root / "drivers/block").mkdir(parents=True)
    (root / "drivers/block/loop.ko").write_bytes(b"module")
    (root / ".config").write_text(fixture.CONFIG.read_text())
    (root / "input.config").write_text(fixture.CONFIG.read_text())
    (root / "include/config/kernel.release").write_text("6.18.54\n")


@pytest.fixture
def built(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _outputs(tmp_path)
    monkeypatch.setattr(fixture, "elf_build_id", lambda _: "abcd1234")
    fixture.record(
        tmp_path,
        baseline="longterm",
        arch="x86_64",
        config_digest=fixture.digest(fixture.CONFIG),
        toolchain={"gcc": "test gcc", "packages": "gcc=1"},
        builder="a" * 40,
        source_epoch=1,
    )
    return tmp_path


def test_record_round_trip_and_identity(built: Path) -> None:
    manifest = fixture.verify(built, baseline="longterm", arch="x86_64")
    assert manifest["source"]["commit"] == fixture.baseline_selection("longterm")["commit"]
    assert manifest["build_id"] == "abcd1234"
    assert manifest["artifacts"]["drivers/block/loop.ko"] == fixture.digest(
        built / "drivers/block/loop.ko"
    )
    assert len(manifest["fixture_id"]) == 64
    assert manifest["artifacts"]["input.config"] == manifest["fragment_sha256"]


@pytest.mark.parametrize(
    "name",
    [".config", "vmlinux", "arch/x86/boot/bzImage", "drivers/block/loop.ko", "modules.order"],
)
def test_output_tampering_fails(built: Path, name: str) -> None:
    (built / name).write_bytes(b"changed")
    with pytest.raises(ValueError):
        fixture.verify(built, baseline="longterm", arch="x86_64")


def test_missing_manifest_and_wrong_selection_fail(built: Path) -> None:
    with pytest.raises(ValueError):
        fixture.verify(built, baseline="stable", arch="x86_64")
    with pytest.raises(ValueError):
        fixture.verify(built, baseline="longterm", arch="ppc64le")
    (built / "manifest.json").unlink()
    with pytest.raises(ValueError):
        fixture.verify(built, baseline="longterm", arch="x86_64")


@pytest.mark.parametrize("replacement", ["{}", "[]", "{"])
def test_malformed_manifest_fails(built: Path, replacement: str) -> None:
    (built / "manifest.json").write_text(replacement)
    with pytest.raises(ValueError):
        fixture.verify(built, baseline="longterm", arch="x86_64")


def test_symlink_artifact_fails(built: Path) -> None:
    (built / "vmlinux").unlink()
    (built / "vmlinux").symlink_to(built / ".config")
    with pytest.raises(ValueError):
        fixture.verify(built, baseline="longterm", arch="x86_64")


def test_manifest_cannot_escape_output(built: Path) -> None:
    path = built / "manifest.json"
    data = json.loads(path.read_text())
    data["artifacts"]["../outside"] = "f" * 64
    data.pop("fixture_id")
    data["fixture_id"] = fixture.identity(data)
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        fixture.verify(built, baseline="longterm", arch="x86_64")


@pytest.mark.parametrize("jobs,arch", [(0, "x86_64"), (-1, "x86_64"), (1, "bogus")])
def test_invalid_build_inputs_do_not_create_output(tmp_path: Path, jobs: int, arch: str) -> None:
    output = tmp_path / "out"
    with pytest.raises(ValueError):
        fixture.build(
            tmp_path / "source",
            output,
            baseline="longterm",
            arch=arch,
            config=fixture.CONFIG,
            jobs=jobs,
        )
    assert not output.exists()


def test_non_native_build_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fixture.platform, "machine", lambda: "aarch64")
    with pytest.raises(ValueError, match="native"):
        fixture.build(
            tmp_path / "source",
            tmp_path / "out",
            baseline="longterm",
            arch="x86_64",
            config=fixture.CONFIG,
            jobs=1,
        )


def test_existing_output_rejected_without_modification(built: Path, monkeypatch) -> None:
    monkeypatch.setattr(fixture.platform, "machine", lambda: "x86_64")
    before = (built / "manifest.json").read_bytes()
    with pytest.raises(ValueError, match="fresh"):
        fixture.build(
            built.parent / "source",
            built,
            baseline="longterm",
            arch="x86_64",
            config=fixture.CONFIG,
            jobs=1,
        )
    assert (built / "manifest.json").read_bytes() == before


def test_dropped_config_is_rejected(tmp_path: Path) -> None:
    config = tmp_path / ".config"
    config.write_text("CONFIG_MODULES=y\n")
    with pytest.raises(ValueError, match="CONFIG_"):
        fixture.check_config(config, fixture.CONFIG)


def test_fragment_replaces_assignments_without_duplicate_symbols(tmp_path: Path) -> None:
    effective = tmp_path / ".config"
    effective.write_text("CONFIG_MODULES=n\n# CONFIG_BPF_SYSCALL is not set\nCONFIG_OTHER=y\n")
    fixture.apply_config(effective, "CONFIG_MODULES=y\nCONFIG_BPF_SYSCALL=y\n")
    assert effective.read_text() == "CONFIG_OTHER=y\nCONFIG_MODULES=y\nCONFIG_BPF_SYSCALL=y\n"


@pytest.mark.parametrize("overlap", ["inside", "ancestor"])
def test_overlapping_output_rejected(tmp_path: Path, monkeypatch, overlap: str) -> None:
    monkeypatch.setattr(fixture.platform, "machine", lambda: "x86_64")
    source = tmp_path / "source"
    output = source / "out" if overlap == "inside" else tmp_path
    with pytest.raises(ValueError, match="separate"):
        fixture.build(
            source, output, baseline="longterm", arch="x86_64", config=fixture.CONFIG, jobs=1
        )


def test_build_sanitizes_environment_and_rechecks_source(tmp_path: Path, monkeypatch) -> None:
    import subprocess

    monkeypatch.setattr(fixture.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(fixture, "toolchain_identity", lambda: {"gcc": "test"})
    monkeypatch.setenv("CROSS_COMPILE", "wrong-")
    monkeypatch.setenv("KBUILD_OUTPUT", "wrong")
    output = tmp_path / "out"
    fetches = 0

    def command(argv, **kwargs):
        nonlocal fetches
        assert "CROSS_COMPILE" not in kwargs["env"]
        assert "KBUILD_OUTPUT" not in kwargs["env"]
        if argv[0] == "bash":
            fetches += 1
            if fetches == 2:
                raise subprocess.CalledProcessError(1, argv)
        elif argv[0] == "git":
            return "1" if "%ct" in argv[-2] else "a" * 40
        elif "defconfig" in argv:
            (output / ".config").write_text("")
        return ""

    monkeypatch.setattr(fixture, "command", command)
    with pytest.raises(subprocess.CalledProcessError):
        fixture.build(
            tmp_path / "source",
            output,
            baseline="longterm",
            arch="x86_64",
            config=fixture.CONFIG,
            jobs=1,
        )
    assert fetches == 2
    assert not (output / "manifest.json").exists()


def test_failed_compilation_never_writes_manifest(tmp_path: Path, monkeypatch) -> None:
    import subprocess

    monkeypatch.setattr(fixture.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(fixture, "toolchain_identity", lambda: {"gcc": "test"})
    source, output = tmp_path / "source", tmp_path / "out"
    source.mkdir()

    def command(argv, **kwargs):
        if argv[0] == "git":
            return "1"
        if argv[0] == "make":
            if "olddefconfig" in argv:
                return ""
            if "defconfig" in argv:
                (output / ".config").write_text("")
                return ""
            raise subprocess.CalledProcessError(2, argv)
        return ""

    monkeypatch.setattr(fixture, "command", command)
    with pytest.raises(subprocess.CalledProcessError):
        fixture.build(
            source, output, baseline="longterm", arch="x86_64", config=fixture.CONFIG, jobs=1
        )
    assert not (output / "manifest.json").exists()
