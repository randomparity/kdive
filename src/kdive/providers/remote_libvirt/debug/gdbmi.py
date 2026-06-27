"""Remote-libvirt gdb-MI attach seam over the shared engine (ADR-0079/0083).

The gdb subprocess still runs on the worker; the only difference from local is the host policy
(ACL-remote, not loopback). Debuginfo resolution + staging is the provider-neutral seam shared with
local (``shared.debug_common.debuginfo``): look the Run's ``debuginfo_ref`` up, fail loud on an
absent one (``no_debuginfo`` ``CONFIGURATION_ERROR``), and materialize the vmlinux into a private
``mkdtemp(0o700)`` dir reclaimed on any failure. The real DB read and the gdb spawn are
``live_vm``-real; the orchestration is unit-tested with injected seams.
"""

from __future__ import annotations

from pathlib import Path

from kdive.providers.ports.debug import GdbMiAttachment
from kdive.providers.shared.debug_common.debuginfo import stage_and_attach
from kdive.providers.shared.debug_common.gdbmi import GdbMiEngine as _GdbMiEngine
from kdive.providers.shared.debug_common.hostpolicy import allow_acl_remote


def remote_attach_seam(
    *, host: str, port: int, run_id: str, transcript_path: Path
) -> GdbMiAttachment:  # pragma: no cover - live_vm
    """Resolve+materialize the remote debuginfo, spawn gdb, connect RSP (ACL-remote policy).

    The vmlinux is staged into a private, owner-only directory (``mkdtemp`` mode ``0o700``) removed
    on any resolve/attach failure — never the old fixed ``/tmp/kdive-remote-debuginfo-{run_id}``
    path a local user could pre-create (symlink attack) or collide across Runs on.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` (``reason=no_debuginfo``) when the Run has no
            published debuginfo object; ``DEBUG_ATTACH_FAILURE`` for a gdb/RSP attach fault.
    """

    def attach(vmlinux_path: Path) -> GdbMiAttachment:
        return _GdbMiEngine(host_policy=allow_acl_remote).attach(
            host=host, port=port, vmlinux_path=vmlinux_path, transcript_path=transcript_path
        )

    return stage_and_attach(run_id=run_id, attach=attach)


__all__ = ["remote_attach_seam"]
