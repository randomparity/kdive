from kdive.kernel_config.parse import KernelConfig
from kdive.kernel_config.requirements import (
    CRASH_CAPTURE,
    CRASH_CAPTURE_RHEL_GUEST,
    FEATURE_REQUIREMENTS,
    SYSRQ,
    feature_manifest,
    feature_requirement,
)
from kdive.kernel_config.support import missing_symbols, unmet_advertised_clauses


def test_crash_capture_gate_excludes_kaslr_and_or_groups_kexec():
    feat = feature_requirement(CRASH_CAPTURE)
    gate_symbols = {s for clause in feat.gate_required for s in clause}
    assert "RANDOMIZE_BASE" not in gate_symbols  # KASLR advertised-only
    assert "RANDOMIZE_BASE" in {s for clause in feat.advertised for s in clause}
    assert frozenset({"KEXEC", "KEXEC_FILE"}) in feat.gate_required  # either load syscall
    assert feat.gated is True


def test_advertise_only_features_have_empty_gate_required():
    for fid in (
        "rootfs_mount",
        "crash_capture_rhel_guest",
        "ikconfig",
        "debuginfo",
        "kasan",
        "serial_console",
    ):
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


def test_debuginfo_summary_names_use_case_and_cost():
    # #1350: the advice must steer an agent away from DWARF5 for a console-log-only
    # investigation by naming *when* debuginfo is useful and *what* it costs. A bare
    # "resolve symbols" summary gave no basis to omit it, so an agent enabled DWARF5 on a
    # boot-time panic reproducer and inflated the module tree to ~2 GB.
    summary = feature_requirement("debuginfo").summary.lower()
    # use case: live introspection or offline vmcore analysis
    assert "drgn" in summary or "vmcore" in summary
    # cost: DWARF tables in every module, large module-tree growth
    assert ".ko" in summary
    assert "10-50x" in summary or "module tree" in summary
    # explicit omit-guidance for the wasteful case
    assert "omit" in summary


def test_unknown_feature_raises():
    import pytest

    with pytest.raises(KeyError):
        feature_requirement("does_not_exist")


def test_rootfs_mount_matches_the_real_direct_kernel_boot():
    # #1094: rootfs_mount used to advertise a squashfs+overlay boot path that does not exist
    # anywhere in the tree. Those stay out — the kdive-provisioned boot (ADR-0030) is a whole-disk
    # qcow2 mounted direct-kernel via root=/dev/vda (a virtio-blk device) with no initramfs.
    # #1626 refines the filesystem half only: a remote or agent-uploaded rootfs (ADR-0183/0440/
    # 0441) is commonly XFS, so the root-fs requirement is EXT4_FS-or-XFS_FS, not EXT4_FS alone.
    feat = feature_requirement("rootfs_mount")
    symbols = {s for clause in feat.advertised for s in clause}
    assert symbols == {"EXT4_FS", "XFS_FS", "VIRTIO_BLK"}
    for stale in ("SQUASHFS", "SQUASHFS_ZSTD", "OVERLAY_FS", "BLK_DEV_LOOP"):
        assert stale not in symbols
    assert "squashfs" not in feat.summary.lower()
    assert "overlay" not in feat.summary.lower()


def test_rootfs_mount_root_filesystem_is_an_or_group_not_two_and_clauses():
    # AND-of-OR: two _plain clauses would make every ext4-only local-libvirt kernel warn for a
    # missing XFS_FS (and vice versa). One OR-group keeps the advisory at "mounts nothing kdive
    # boots", which is the only claim kdive can make without a guest-family axis.
    feat = feature_requirement("rootfs_mount")
    assert frozenset({"EXT4_FS", "XFS_FS"}) in feat.advertised
    assert frozenset({"EXT4_FS"}) not in feat.advertised
    assert frozenset({"XFS_FS"}) not in feat.advertised


def test_rhel_guest_kdump_feature_carries_the_symbols_lost_with_the_build_fragment():
    # #1626: ADR-0213 put SQUASHFS/SQUASHFS_ZSTD/BLK_DEV_LOOP/OVERLAY_FS/KEXEC_FILE and ADR-0183
    # put XFS_FS into the ADR-0096 kdump build-config fragment. ADR-0316 deleted the fragment and
    # every symbol but KEXEC_FILE went with it, unnoticed, until #1610's Rocky 10 live run needed
    # five rebuilds to rediscover them. They now live here.
    feat = feature_requirement(CRASH_CAPTURE_RHEL_GUEST)
    symbols = {s for clause in feat.advertised for s in clause}
    assert symbols == {
        "XFS_FS",
        "SQUASHFS",
        "SQUASHFS_ZSTD",
        "EROFS_FS",
        "OVERLAY_FS",
        "BLK_DEV_LOOP",
        "KEXEC_FILE",
    }


def test_rhel_guest_kdump_summary_says_it_is_conditional_and_names_the_dependencies():
    # The issue asks that the set be described as filesystem- and initramfs-dependent rather than
    # implied universal, so a non-RHEL guest knows to skip it.
    summary = feature_requirement(CRASH_CAPTURE_RHEL_GUEST).summary.lower()
    assert "rhel" in summary
    assert "initramfs" in summary
    assert "dracut" in summary
    assert "kexec_file_load" in summary
    assert "non-rhel" in summary


def test_rhel_guest_kdump_names_every_missing_symbol_for_a_bare_defconfig_capture_kernel():
    # The bite: the kernel the #1610 run first uploaded — crash_capture-complete and gate-passing,
    # but with none of the RHEL-family extras — must now come back naming all seven at once,
    # instead of surfacing one per rebuild.
    cfg = KernelConfig(
        frozenset(
            {
                "KEXEC",
                "KEXEC_CORE",
                "CRASH_DUMP",
                "PROC_VMCORE",
                "VMCORE_INFO",
                "FW_CFG_SYSFS",
                "RELOCATABLE",
                "EXT4_FS",
                "VIRTIO_BLK",
            }
        )
    )
    unmet = unmet_advertised_clauses(cfg, feature_requirement(CRASH_CAPTURE_RHEL_GUEST))
    assert missing_symbols(unmet) == [
        "BLK_DEV_LOOP",
        "EROFS_FS",
        "KEXEC_FILE",
        "OVERLAY_FS",
        "SQUASHFS",
        "SQUASHFS_ZSTD",
        "XFS_FS",
    ]


def test_rhel_guest_kdump_is_silent_for_a_kernel_that_carries_the_whole_set():
    cfg = KernelConfig(
        frozenset(
            {
                "XFS_FS",
                "SQUASHFS",
                "SQUASHFS_ZSTD",
                "EROFS_FS",
                "OVERLAY_FS",
                "BLK_DEV_LOOP",
                "KEXEC_FILE",
            }
        )
    )
    assert unmet_advertised_clauses(cfg, feature_requirement(CRASH_CAPTURE_RHEL_GUEST)) == ()


def test_crash_capture_summary_disclaims_being_sufficient_on_a_rhel_guest():
    # The #1626 trap: crash_capture is complete, the gate passes, kexec_crash_size is non-zero,
    # and capture still produces nothing. The base feature must point at the conditional one.
    summary = feature_requirement(CRASH_CAPTURE).summary
    assert CRASH_CAPTURE_RHEL_GUEST in summary


def test_virtio_blk_is_filed_under_rootfs_mount_not_serial_console():
    # The root-disk driver requirement was previously misfiled under serial_console.
    rootfs = feature_requirement("rootfs_mount")
    serial = feature_requirement("serial_console")
    rootfs_symbols = {s for clause in rootfs.advertised for s in clause}
    serial_symbols = {s for clause in serial.advertised for s in clause}
    assert "VIRTIO_BLK" in rootfs_symbols
    assert "VIRTIO_BLK" not in serial_symbols
