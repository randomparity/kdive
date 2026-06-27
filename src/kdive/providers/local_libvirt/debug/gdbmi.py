"""Local-libvirt gdb-MI wiring: the provider attach seam over the shared engine (ADR-0034/0083).

The gdb-MI engine itself is provider-neutral (``kdive.providers.shared.debug_common.gdbmi``); the
debuginfo resolution + private staging orchestration is the provider-neutral seam shared with
remote (``shared.debug_common.debuginfo`` — ``stage_and_attach``). This module keeps only
local-libvirt's configured ``default_attach_seam``: it stages the Run's vmlinux via the shared
seam factory and attaches loopback-only (the engine's default host policy). Only the real DB read,
object-store fetch, and gdb spawn are ``live_vm``-real (mirroring the Retrieve/introspect plane
split, ADR-0210 §1).
"""

from __future__ import annotations

from kdive.providers.shared.debug_common.debuginfo import gdb_attach_seam
from kdive.providers.shared.debug_common.gdbmi import GdbMiEngine as _GdbMiEngine

default_attach_seam = gdb_attach_seam(engine_factory=_GdbMiEngine)


__all__ = ["default_attach_seam"]
