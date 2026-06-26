"""Fedora/rhel-family rootfs customization contracts (ADR-0251)."""

from __future__ import annotations

from kdive.images.families._fedora_customize import READINESS_UNIT


def test_readiness_unit_ordered_after_kdump_arming() -> None:
    """The serial ``kdive-ready`` signal must not fire before kdump finishes arming (#817).

    On a crash-capture image ``kdump.service`` (``WantedBy=multi-user.target``) builds the capture
    initramfs and ``kexec -p``-loads it; the readiness unit is also ``WantedBy=multi-user.target``,
    so without an ordering edge the serial ``kdive-ready`` signal can race ahead of kdump arming.
    A ``force_crash`` on a System that reported ``ready`` before kdump armed then captures nothing
    (an empty ``/var/crash`` — not even a ``vmcore-incomplete``). Ordering the readiness unit
    ``After=kdump.service`` makes ``ready`` mean "kdump finished its arming attempt". ``After=``
    against an absent unit is a no-op, so a non-kdump (build) image is unaffected.
    """
    after_targets = [
        target
        for line in READINESS_UNIT.splitlines()
        if line.startswith("After=")
        for target in line.removeprefix("After=").split()
    ]
    assert "kdump.service" in after_targets, (
        "kdive-ready must be ordered After=kdump.service so the serial readiness signal cannot "
        "precede kdump arming (#817 arm-vs-ready race)"
    )
