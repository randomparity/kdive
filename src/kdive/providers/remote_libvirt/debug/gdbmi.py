"""Remote-libvirt gdb-MI attach seam over the shared engine (ADR-0079/0083).

The gdb subprocess still runs on the worker; the only difference from local is the host policy
(ACL-remote, not loopback). Debuginfo resolution + staging is configured through the
provider-neutral seam factory shared with local (``shared.debug_common.gdbmi.debuginfo``): look
the Run's ``debuginfo_ref`` up, fail loud on an absent one (``no_debuginfo``
``CONFIGURATION_ERROR``), and materialize the vmlinux into a private ``mkdtemp(0o700)`` dir
reclaimed on any failure. The real DB read and the gdb spawn are ``live_vm``-real; the
orchestration is unit-tested with injected seams.
"""

from __future__ import annotations

from kdive.providers.shared.debug_common.gdbmi.debuginfo import gdb_attach_seam
from kdive.providers.shared.debug_common.gdbmi.engine import GdbMiEngine as _GdbMiEngine
from kdive.providers.shared.debug_common.gdbmi.hostpolicy import allow_acl_remote

remote_attach_seam = gdb_attach_seam(
    engine_factory=lambda: _GdbMiEngine(host_policy=allow_acl_remote)
)


__all__ = ["remote_attach_seam"]
