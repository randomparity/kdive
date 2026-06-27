"""Local-libvirt gdb-MI wiring: the provider attach seam over the shared engine (ADR-0034/0083).

The gdb-MI engine itself is provider-neutral (``kdive.providers.shared.debug_common.gdbmi``); the
debuginfo resolution + private staging orchestration is the provider-neutral seam shared with
remote (``shared.debug_common.debuginfo`` — ``stage_and_attach``). This module keeps only
local-libvirt's ``default_attach_seam``: it stages the Run's vmlinux via the shared seam and
attaches loopback-only (the engine's default host policy). Only the real DB read, object-store
fetch, and gdb spawn are ``live_vm``-real (mirroring the Retrieve/introspect plane split,
ADR-0210 §1).
"""

from __future__ import annotations

from pathlib import Path

from kdive.providers.ports import GdbMiAttachment
from kdive.providers.shared.debug_common.debuginfo import stage_and_attach
from kdive.providers.shared.debug_common.gdbmi import GdbMiEngine as _GdbMiEngine


def default_attach_seam(
    *, host: str, port: int, run_id: str, transcript_path: Path
) -> GdbMiAttachment:  # pragma: no cover - live_vm
    """The real ``live_vm`` local attach: resolve+materialize debuginfo, spawn gdb, connect RSP.

    The vmlinux is staged into a private, owner-only ``mkdtemp(0o700)`` dir (not a fixed/predictable
    name) and reclaimed on any resolve/attach failure (the shared ``stage_and_attach`` seam); on a
    successful attach the staged vmlinux outlives this call because the live gdb reads symbols from
    it for the session's lifetime.
    """

    def attach(vmlinux_path: Path) -> GdbMiAttachment:
        return _GdbMiEngine().attach(
            host=host, port=port, vmlinux_path=vmlinux_path, transcript_path=transcript_path
        )

    return stage_and_attach(run_id=run_id, attach=attach)


__all__ = ["default_attach_seam"]
