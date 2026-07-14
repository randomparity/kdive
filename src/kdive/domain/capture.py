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
