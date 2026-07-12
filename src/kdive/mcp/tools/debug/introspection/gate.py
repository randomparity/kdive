"""Runtime drgn-live symbol-resolution probe (ADR-0329, ADR-0335).

The static config check (:func:`kdive.kernel_config.gate.debuginfo_warning`) proves BTF is
*advertised* by the uploaded ``.config``, never that the running guest's drgn can load it. This
module adds the runtime signal: a fixed one-line drgn lookup over the existing ``run_script`` seam
that finds a session blind even when the config looked healthy. Both the live-introspection
handlers and the ``debug.start_session`` attach seam call :func:`augment_with_runtime_probe` to fill
the exact gap the static check cannot cover, so the probe's gating and fail-open semantics have one
source of truth.
"""

from __future__ import annotations

import asyncio

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.kernel_config.gate import debuginfo_unloadable_warning
from kdive.prereqs.system_bootstrap_key import materialized_private_key
from kdive.providers.ports.retrieve import LiveIntrospector
from kdive.serialization import JsonValue

# A drgn-live runtime resolution probe: a bare lookup of a stable kernel global. On a kernel whose
# in-guest drgn cannot load debuginfo/BTF the lookup raises, the guest wrapper exits non-zero, and
# run_script surfaces DEBUG_ATTACH_FAILURE — the signal that the session is blind even when the
# uploaded .config advertised BTF (the static config check cannot see this).
RESOLUTION_PROBE_SCRIPT = "prog['init_task']\n"
_PROBE_TIMEOUT_SEC = 10.0


async def augment_with_runtime_probe(
    static_warning: dict[str, JsonValue] | None,
    *,
    introspector: LiveIntrospector,
    transport_handle: str,
    private_key: str,
    has_uploaded_vmlinux: bool,
) -> dict[str, JsonValue] | None:
    """Add the runtime resolution signal to the static config warning (ADR-0329, ADR-0335).

    The static config check is authoritative when it already warns (no BTF advertised, no vmlinux)
    or when a host vmlinux was uploaded (drgn resolves from it). Only when it is silent for a
    vmlinux-less Run — BTF advertised, or no config uploaded — can the session still be blind at
    runtime; probe it and return ``debuginfo_unloadable`` when resolution provably failed. The
    extra round-trip is confined to exactly that gap. Shared by the ``introspect.*`` seams and the
    ``debug.start_session`` attach seam so both compute the same warning.
    """
    if static_warning is not None or has_uploaded_vmlinux:
        return static_warning
    resolved = await probe_symbol_resolution(
        introspector, transport_handle=transport_handle, private_key=private_key
    )
    if resolved is False:
        return debuginfo_unloadable_warning()
    return static_warning


async def probe_symbol_resolution(
    introspector: LiveIntrospector,
    *,
    transport_handle: str,
    private_key: str,
) -> bool | None:
    """Probe whether in-guest drgn can resolve a stable kernel symbol at runtime (ADR-0329).

    Runs a fixed one-line resolution probe over the existing ``run_script`` seam. Returns ``True``
    when the probe resolves (drgn exited zero), ``False`` when drgn attached but could not resolve
    the symbol (``DEBUG_ATTACH_FAILURE`` — the blind-session signal), and ``None`` when the probe
    is indeterminate (a transport or other fault the real op will surface anyway). Fail-open: an
    indeterminate probe adds no warning and never blocks the attach or introspection.
    """
    try:
        with materialized_private_key(private_key) as key_path:
            await asyncio.to_thread(
                introspector.run_script,
                transport_handle=transport_handle,
                script=RESOLUTION_PROBE_SCRIPT,
                timeout_sec=_PROBE_TIMEOUT_SEC,
                key_path=str(key_path),
            )
    except CategorizedError as exc:
        if exc.category is ErrorCategory.DEBUG_ATTACH_FAILURE:
            return False
        return None
    return True
