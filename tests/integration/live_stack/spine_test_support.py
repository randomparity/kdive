"""Synthetic kernel build inputs shared by ordinary live-stack helper tests."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.integration.live_stack import spine

_BOOT_CONFIG = b"CONFIG_VIRTIO_PCI=y\nCONFIG_VIRTIO_BLK=y\nCONFIG_EXT4_FS=y\n"


_INITRD_CONFIG = _BOOT_CONFIG + b"CONFIG_BLK_DEV_INITRD=y\nCONFIG_RD_GZIP=y\n"


def _kernel_tree(tmp_path: Path, config: bytes = _BOOT_CONFIG) -> Path:
    tree = tmp_path / "tree"
    (tree / "include/config").mkdir(parents=True)
    (tree / ".config").write_bytes(config)
    (tree / "include/config/kernel.release").write_text("7.1.0-spine\n")
    return tree


def _prebuilt_power_bundle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    bundle = tmp_path / "power-bundle"
    bundle.mkdir()
    with tarfile.open(bundle / "kernel.tar.gz", "w:gz") as archive:
        boot = tarfile.TarInfo("boot/vmlinuz")
        boot.size = len(b"\x7fELFpower-boot")
        archive.addfile(boot, io.BytesIO(b"\x7fELFpower-boot"))
        for name in ("lib/modules/", "lib/modules/7.1.0-spine/"):
            modules = tarfile.TarInfo(name)
            modules.type = tarfile.DIRTYPE
            archive.addfile(modules)
    (bundle / "initrd.img").write_bytes(b"\x1f\x8bnative-initrd")
    monkeypatch.setenv("KDIVE_PPC64LE_BUNDLE", str(bundle))

    def readelf(args: list[str], **_kwargs: object) -> SimpleNamespace:
        assert args[:2] == ["readelf", "-n"]
        build_id = "ab1234" if Path(args[-1]).read_bytes() == b"\x7fELFpower-boot" else "bad123"
        return SimpleNamespace(stdout=f"Build ID: {build_id}\n")

    monkeypatch.setattr(spine.subprocess, "run", readelf)
    return bundle
