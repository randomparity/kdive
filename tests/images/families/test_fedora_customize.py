"""Fedora/rhel-family rootfs customization contracts (ADR-0251)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kdive.images.families._fedora_customize import (
    CLOUD_INIT_UNITS,
    KDIVE_CLOUD_CFG_PATH,
    NOCLOUD_SEED_DIR,
    SEED_MACHINE_ID,
    cloud_init_first_boot_args,
    makedumpfile_version_marker_args,
    readiness_unit,
)
from kdive.images.families.base import CustomizeContext
from kdive.images.planes._build_common import MAKEDUMPFILE_MARKER_GUEST_PATH


def test_makedumpfile_marker_args_writes_version_file() -> None:
    argv = makedumpfile_version_marker_args()
    joined = " ".join(argv)
    assert "--run-command" in argv
    assert MAKEDUMPFILE_MARKER_GUEST_PATH in joined
    assert "makedumpfile -v" in joined


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
    after_targets = _after_targets(readiness_unit(kdump_unit))
    assert kdump_unit in after_targets, (
        f"kdive-ready must be ordered After={kdump_unit} so the serial readiness signal cannot "
        "precede kdump arming (#817 race); a wrong/absent unit name silently reopens it"
    )
    assert "dev-ttyS0.device" in after_targets, "the serial device ordering is preserved"


def _ci_ctx(tmp_path: Path, *, is_cloud_image: bool) -> CustomizeContext:
    return CustomizeContext(
        kind="debug",
        packages=("openssh-server",),
        authorized_key=tmp_path / "key.pub",
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
    assert "resize_rootfs: false" in cfg


def test_cloud_init_helper_writes_nocloud_seed(tmp_path: Path) -> None:
    argv = cloud_init_first_boot_args(_ci_ctx(tmp_path, is_cloud_image=True))
    uploads = _uploads(argv)
    assert uploads[f"{NOCLOUD_SEED_DIR}/meta-data"].startswith("instance-id:")
    assert uploads[f"{NOCLOUD_SEED_DIR}/user-data"].startswith("#cloud-config")
    assert "--mkdir" in argv and NOCLOUD_SEED_DIR in argv


def test_cloud_init_helper_enables_full_pipeline_and_seeds_machine_id(tmp_path: Path) -> None:
    j = " ".join(cloud_init_first_boot_args(_ci_ctx(tmp_path, is_cloud_image=True)))
    for unit in (
        "cloud-init-local.service",
        "cloud-init.service",
        "cloud-config.service",
        "cloud-final.service",
    ):
        assert unit in j
    assert f"systemctl unmask {CLOUD_INIT_UNITS}" in j
    assert f"systemctl enable {CLOUD_INIT_UNITS}" in j
    assert "rm -f /etc/cloud/cloud-init.disabled" in j  # harmless if absent (debian path)
    assert f"/etc/machine-id:{SEED_MACHINE_ID}" in j  # seeded on every image now


def test_cloud_init_helper_installs_cloud_init_only_on_non_cloud_base(tmp_path: Path) -> None:
    cloud = " ".join(cloud_init_first_boot_args(_ci_ctx(tmp_path, is_cloud_image=True)))
    scratch = " ".join(cloud_init_first_boot_args(_ci_ctx(tmp_path, is_cloud_image=False)))
    assert "--install cloud-init" not in cloud  # ships cloud-init already
    assert "--install cloud-init" in scratch  # virt-builder base needs it installed
