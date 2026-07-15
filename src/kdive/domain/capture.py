"""The provider-agnostic crash-capture method vocabulary (ADR-0049 Decision 1)."""

from __future__ import annotations

from enum import StrEnum


class CaptureMethod(StrEnum):
    CONSOLE = "console"
    HOST_DUMP = "host_dump"
    GDBSTUB = "gdbstub"
    KDUMP = "kdump"
    # Firmware-assisted dump (POWER pseries, ADR-0349): a memory-preserving reboot the platform
    # firmware drives, reusing the kdump userspace and retrieve path. Only ever resolved for a
    # ppc64le System that also carries a crashkernel reservation.
    FADUMP = "fadump"


# The guest-kernel crash-capture family: methods that reserve boot memory via ``crashkernel=`` and
# produce a ``/proc/vmcore`` through the shared kdump userspace and retrieve path. fadump is the
# pseries firmware-assisted variant of kdump (ADR-0349), so both share the install prerequisites,
# the ``crashkernel=`` cmdline token, the ADR-0318 kernel-config gate, and the overlay harvest —
# every "is this the kdump family?" test reads this one definition.
KDUMP_FAMILY = frozenset({CaptureMethod.KDUMP, CaptureMethod.FADUMP})
