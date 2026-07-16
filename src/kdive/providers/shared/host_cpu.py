"""Compose the advertised/persisted ``host_cpu`` dict from a parsed libvirt CPU block (ADR-0369).

Shared by local-libvirt discovery (the advertised ``host_cpu``) and the provisioner's
post-provision resolved-CPU read (``resolved_cpu``), so the two sites cannot drift on the shape or
the x86-64-vN level derivation. Kept out of :mod:`kdive.providers.shared.libvirt_xml`, which is
deliberately ``domain``-free (ADR-0368); this helper depends on ``domain.platform.cpu_baseline``.
"""

from __future__ import annotations

from kdive.domain.platform.cpu_baseline import baseline_level
from kdive.providers.shared.libvirt_xml import ParsedHostCpu
from kdive.serialization import JsonValue


def host_cpu_dict(parsed: ParsedHostCpu, fallback_arch: str) -> dict[str, JsonValue]:
    """Build the ``{model, vendor?, arch, baseline_level?}`` dict, adding the x86 level.

    ``arch`` falls back to ``fallback_arch`` when the parsed block carries none; ``vendor`` and the
    normalized ``baseline_level`` (x86-64 only, disable-guarded) are included only when present.
    """
    result: dict[str, JsonValue] = {"model": parsed.model, "arch": parsed.arch or fallback_arch}
    if parsed.vendor is not None:
        result["vendor"] = parsed.vendor
    level = baseline_level(parsed.model, parsed.disabled_features)
    if level is not None:
        result["baseline_level"] = level
    return result
