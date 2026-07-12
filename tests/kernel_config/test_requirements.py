from kdive.kernel_config.requirements import (
    CRASH_CAPTURE,
    FEATURE_REQUIREMENTS,
    SYSRQ,
    feature_manifest,
    feature_requirement,
)


def test_crash_capture_gate_excludes_kaslr_and_or_groups_kexec():
    feat = feature_requirement(CRASH_CAPTURE)
    gate_symbols = {s for clause in feat.gate_required for s in clause}
    assert "RANDOMIZE_BASE" not in gate_symbols  # KASLR advertised-only
    assert "RANDOMIZE_BASE" in {s for clause in feat.advertised for s in clause}
    assert frozenset({"KEXEC", "KEXEC_FILE"}) in feat.gate_required  # either load syscall
    assert feat.gated is True


def test_advertise_only_features_have_empty_gate_required():
    for fid in ("rootfs_mount", "ikconfig", "debuginfo", "kasan", "serial_console"):
        feat = feature_requirement(fid)
        assert feat.gate_required == ()
        assert feat.gated is False


def test_sysrq_is_advertised_and_gate_required_magic_sysrq():
    feat = feature_requirement(SYSRQ)
    assert feat.gate_required == (frozenset({"MAGIC_SYSRQ"}),)


def test_manifest_covers_every_feature_and_exposes_advertised_not_gate_required():
    import json

    manifest = feature_manifest()
    assert {m["feature"] for m in manifest} == {f.feature for f in FEATURE_REQUIREMENTS}
    entry = next(m for m in manifest if m["feature"] == CRASH_CAPTURE)
    assert entry["gated"] is True
    assert entry["summary"]
    assert isinstance(entry["requirements"], list)
    # advertised superset carries KASLR (advertise-only); the gate-set exclusion is asserted above
    assert "RANDOMIZE_BASE" in json.dumps(entry["requirements"])
    assert "gate_required" not in entry  # internal, not advertised


def test_unknown_feature_raises():
    import pytest

    with pytest.raises(KeyError):
        feature_requirement("does_not_exist")


def test_rootfs_mount_matches_the_real_direct_kernel_ext4_boot():
    # #1094: rootfs_mount used to advertise a squashfs+overlay boot path that does not exist
    # anywhere in the tree. The real boot (ADR-0030) is a whole-disk ext4 qcow2 mounted
    # direct-kernel via root=/dev/vda (a virtio-blk device) with no initramfs, so the manifest
    # must ask for EXT4_FS + VIRTIO_BLK and nothing squashfs/overlay-related.
    feat = feature_requirement("rootfs_mount")
    symbols = {s for clause in feat.advertised for s in clause}
    assert symbols == {"EXT4_FS", "VIRTIO_BLK"}
    for stale in ("SQUASHFS", "SQUASHFS_ZSTD", "OVERLAY_FS", "BLK_DEV_LOOP", "XFS_FS"):
        assert stale not in symbols
    assert "squashfs" not in feat.summary.lower()
    assert "overlay" not in feat.summary.lower()


def test_virtio_blk_is_filed_under_rootfs_mount_not_serial_console():
    # The root-disk driver requirement was previously misfiled under serial_console.
    rootfs = feature_requirement("rootfs_mount")
    serial = feature_requirement("serial_console")
    rootfs_symbols = {s for clause in rootfs.advertised for s in clause}
    serial_symbols = {s for clause in serial.advertised for s in clause}
    assert "VIRTIO_BLK" in rootfs_symbols
    assert "VIRTIO_BLK" not in serial_symbols
