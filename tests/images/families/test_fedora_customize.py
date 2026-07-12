"""Fedora/rhel-family rootfs customization contracts (ADR-0251)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kdive.images.families._fedora_customize import (
    KDIVE_CLOUD_CFG_PATH,
    NOCLOUD_SEED_DIR,
    SEED_MACHINE_ID,
    cloud_init_first_boot_args,
    drgn_version_marker_args,
    makedumpfile_version_marker_args,
    readiness_unit,
)
from kdive.images.families.base import CustomizeContext
from kdive.images.planes._build_common import (
    DRGN_MARKER_GUEST_PATH,
    MAKEDUMPFILE_MARKER_GUEST_PATH,
)


def test_makedumpfile_marker_args_writes_version_file() -> None:
    argv = makedumpfile_version_marker_args()
    joined = " ".join(argv)
    assert "--run-command" in argv
    assert MAKEDUMPFILE_MARKER_GUEST_PATH in joined
    assert "makedumpfile -v" in joined


def test_drgn_marker_args_writes_version_file() -> None:
    argv = drgn_version_marker_args()
    joined = " ".join(argv)
    assert "--run-command" in argv
    assert DRGN_MARKER_GUEST_PATH in joined
    assert "drgn --version" in joined


def _after_targets(unit: str) -> list[str]:
    return [
        target
        for line in unit.splitlines()
        if line.startswith("After=")
        for target in line.removeprefix("After=").split()
    ]


@pytest.mark.parametrize("kdump_unit", ["kdump.service", "kdump-tools.service"])
def test_readiness_unit_ordered_after_the_family_kdump_unit(kdump_unit: str) -> None:
    """The serial ``kdive-ready`` signal must not fire before kdump finishes arming (#817, #824).

    On a crash-capture image the family's kdump unit (``WantedBy=multi-user.target``) builds the
    capture initramfs and ``kexec -p``-loads it; the readiness unit is also
    ``WantedBy=multi-user.target``, so without an ordering edge the serial ``kdive-ready`` signal
    can race ahead of kdump arming. A ``force_crash`` on a System that reported ``ready`` before
    kdump armed then captures nothing (an empty ``/var/crash`` — not even a ``vmcore-incomplete``).
    Ordering the readiness unit ``After=<kdump-unit>`` makes ``ready`` mean "kdump finished its
    arming attempt". The unit name is family-parameterized (``rhel`` → ``kdump.service``, ``debian``
    → ``kdump-tools.service``) so the edge always names the real unit; ``After=`` against an absent
    unit is a no-op, so a non-kdump (build) image is unaffected (#824).
    """
    after_targets = _after_targets(readiness_unit(kdump_unit, "ttyS0"))
    assert kdump_unit in after_targets, (
        f"kdive-ready must be ordered After={kdump_unit} so the serial readiness signal cannot "
        "precede kdump arming (#817 race); a wrong/absent unit name silently reopens it"
    )
    assert "dev-ttyS0.device" in after_targets, "the serial device ordering is preserved"
    assert "network-online.target" in after_targets, (
        "kdive-ready must be ordered After=network-online.target so `ready` implies the cloud-init "
        "DHCP lease (ADR-0288); else authorize_ssh_key at ready races the lease (live-found)"
    )


def test_readiness_unit_targets_the_arch_console_device() -> None:
    # On pseries there is no ttyS0; the serial console is hvc0. The unit must order after
    # dev-hvc0.device and echo the marker to /dev/hvc0 or the marker never reaches the host log.
    unit = readiness_unit("kdump.service", "hvc0")
    assert "dev-hvc0.device" in _after_targets(unit)
    assert "> /dev/hvc0" in unit
    assert "ttyS0" not in unit


def _ci_ctx(tmp_path: Path, *, is_cloud_image: bool) -> CustomizeContext:
    return CustomizeContext(
        kind="debug",
        packages=("openssh-server",),
        readiness_unit_path=tmp_path / "u.service",
        is_cloud_image=is_cloud_image,
        cleanup=[],
        distro="fedora",
        version="44",
    )


def _uploads(argv: list[str]) -> dict[str, str]:
    # Map each `--upload LOCAL:REMOTE` to {REMOTE: text-of-LOCAL}.
    out: dict[str, str] = {}
    for flag, val in zip(argv, argv[1:], strict=False):
        if flag == "--upload":
            local, remote = val.split(":", 1)
            out[remote] = Path(local).read_text()
    return out


def test_cloud_init_helper_writes_authoritative_cfg(tmp_path: Path) -> None:
    argv = cloud_init_first_boot_args(_ci_ctx(tmp_path, is_cloud_image=True))
    cfg = _uploads(argv)[KDIVE_CLOUD_CFG_PATH]
    assert "datasource_list: [ NoCloud ]" in cfg
    assert "disable_root: false" in cfg
    assert "dhcp4: true" in cfg and 'match: { name: "e*" }' in cfg
    assert 'mode: "off"' in cfg  # quoted so YAML does not read it as boolean false
    # growpart stays off (no partition table, ADR-0030); resize_rootfs is on so cloud-init
    # grows the whole-disk ext4 to fill an overlay sized at provision (ADR-0312, #985).
    assert "resize_rootfs: true" in cfg


def test_cloud_init_helper_writes_nocloud_seed(tmp_path: Path) -> None:
    argv = cloud_init_first_boot_args(_ci_ctx(tmp_path, is_cloud_image=True))
    uploads = _uploads(argv)
    assert uploads[f"{NOCLOUD_SEED_DIR}/meta-data"].startswith("instance-id:")
    assert uploads[f"{NOCLOUD_SEED_DIR}/user-data"].startswith("#cloud-config")
    assert "--mkdir" in argv and NOCLOUD_SEED_DIR in argv


def test_cloud_init_helper_undisables_and_seeds_machine_id(tmp_path: Path) -> None:
    # ADR-0288: the helper undoes any cloud-init disable and seeds machine-id, but does NOT
    # `systemctl enable` named units — the vendor base ships them enabled and unit names vary
    # across cloud-init versions (24.x renamed cloud-init.service). Enumerating names is fragile.
    j = " ".join(cloud_init_first_boot_args(_ci_ctx(tmp_path, is_cloud_image=True)))
    assert "rm -f /etc/cloud/cloud-init.disabled" in j  # harmless if absent (debian path)
    assert f"/etc/machine-id:{SEED_MACHINE_ID}" in j  # seeded on every image now
    assert "systemctl enable cloud-init" not in j  # no fragile unit-name enumeration
    assert "systemctl unmask cloud-init" not in j


def test_cloud_init_helper_installs_cloud_init_only_on_non_cloud_base(tmp_path: Path) -> None:
    cloud = " ".join(cloud_init_first_boot_args(_ci_ctx(tmp_path, is_cloud_image=True)))
    scratch = " ".join(cloud_init_first_boot_args(_ci_ctx(tmp_path, is_cloud_image=False)))
    assert "--install cloud-init" not in cloud  # ships cloud-init already
    assert "--install cloud-init" in scratch  # virt-builder base needs it installed
