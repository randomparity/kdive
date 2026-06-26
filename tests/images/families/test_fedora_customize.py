"""Fedora/rhel-family rootfs customization contracts (ADR-0251)."""

from __future__ import annotations

import pytest

from kdive.images.families._fedora_customize import readiness_unit


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
