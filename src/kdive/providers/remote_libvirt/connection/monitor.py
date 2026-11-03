"""The remote QEMU monitor passthrough (#3294).

``libvirt.virDomain`` has no ``qemuMonitorCommand`` method: the passthrough is the
``libvirt_qemu`` module function, which takes the domain as its first argument. Every remote
QMP/HMP caller goes through :func:`qemu_monitor_command`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

import libvirt
import libvirt_qemu

type Monitor = Callable[[object, str, int], str]


def qemu_monitor_command(domain: object, cmd: str, flags: int) -> str:
    """Send ``cmd`` to ``domain``'s QEMU monitor and return QEMU's reply text."""
    return libvirt_qemu.qemuMonitorCommand(cast(libvirt.virDomain, domain), cmd, flags)
