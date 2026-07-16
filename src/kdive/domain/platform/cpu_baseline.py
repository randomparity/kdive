"""Normalize a libvirt/QEMU x86-64 CPU model name to an ``x86-64-vN`` baseline level (ADR-0368).

The level is a nominal, name-derived upper bound: it maps the model name to its spec level, then
omits the level when a feature that *defines* that level is explicitly disabled in the host-model
block. It does not reconstruct the full feature set (that alternative was rejected in ADR-0368),
so a base-model-implied feature the host silently lacks is not caught — callers must treat a
present level as nominal, not a guaranteed floor.
"""

from __future__ import annotations

from collections.abc import Collection

# Curated libvirt/QEMU model name -> x86-64 micro-arch level. Extend as new models ship; an
# unmapped model degrades to "raw model, no level" (never a wrong level).
X86_64_MODEL_LEVELS: dict[str, str] = {
    # v2
    "Nehalem": "x86-64-v2",
    "Nehalem-IBRS": "x86-64-v2",
    "Westmere": "x86-64-v2",
    "Westmere-IBRS": "x86-64-v2",
    "SandyBridge": "x86-64-v2",
    "SandyBridge-IBRS": "x86-64-v2",
    "IvyBridge": "x86-64-v2",
    "IvyBridge-IBRS": "x86-64-v2",
    # Opteron_G3 (AMD K10/Barcelona) is deliberately omitted: it lacks SSSE3 and SSE4.1/4.2, so it
    # does NOT meet x86-64-v2, and the disable-guard cannot catch it (those features are absent, not
    # <feature policy='disable'>). Mapping it would advertise a wrong level. G4/G5 (Bulldozer/
    # Piledriver) do carry the v2 feature set.
    "Opteron_G4": "x86-64-v2",
    "Opteron_G5": "x86-64-v2",
    "EPYC": "x86-64-v2",
    # v3
    "Haswell": "x86-64-v3",
    "Haswell-IBRS": "x86-64-v3",
    "Haswell-noTSX": "x86-64-v3",
    "Haswell-noTSX-IBRS": "x86-64-v3",
    "Broadwell": "x86-64-v3",
    "Broadwell-IBRS": "x86-64-v3",
    "Broadwell-noTSX": "x86-64-v3",
    "Broadwell-noTSX-IBRS": "x86-64-v3",
    "Skylake-Client": "x86-64-v3",
    "Skylake-Client-IBRS": "x86-64-v3",
    "Skylake-Client-noTSX-IBRS": "x86-64-v3",
    "Skylake-Server": "x86-64-v3",
    "Skylake-Server-IBRS": "x86-64-v3",
    "Skylake-Server-noTSX-IBRS": "x86-64-v3",
    "EPYC-Rome": "x86-64-v3",
    "EPYC-Milan": "x86-64-v3",
    # v4
    "Cascadelake-Server": "x86-64-v4",
    "Cascadelake-Server-noTSX": "x86-64-v4",
    "Icelake-Server": "x86-64-v4",
    "Icelake-Server-noTSX": "x86-64-v4",
    "SapphireRapids": "x86-64-v4",
    "EPYC-Genoa": "x86-64-v4",
}

# The features that define each level; if the host-model block disables one for its mapped level,
# the guest is below that level and the level is omitted. Only the level-boundary markers, not the
# full set (ADR-0368: a targeted disable-guard, not full feature expansion).
#
# The tokens are libvirt/QEMU cpu-map feature names, matched verbatim against the
# `<feature policy='disable' name=...>` spelling. These names are stable in the libvirt cpu-map
# (dotted `sse4.2`, bare `avx2`/`bmi2`/`avx`/`avx512f`/`popcnt`) and were verified against a live
# host (a SapphireRapids host reported `avx512f` and disabled `rtm`/`hle`/`taa-no` with these exact
# spellings). `test_host_cpu_disable_guard_omits_level_end_to_end` pins `avx2` through the full
# parse -> guard path so a rename fails a test rather than silently advertising a wrong level.
LEVEL_DEFINING_FEATURES: dict[str, frozenset[str]] = {
    "x86-64-v2": frozenset({"sse4.2", "popcnt"}),
    "x86-64-v3": frozenset({"avx2", "bmi2", "avx"}),
    "x86-64-v4": frozenset({"avx512f"}),
}


def baseline_level(model: str, disabled_features: Collection[str]) -> str | None:
    """Return the ``x86-64-vN`` level for ``model``, or ``None`` if unmapped or under-delivered.

    Maps ``model`` via :data:`X86_64_MODEL_LEVELS`; returns ``None`` when the model is not in the
    table, or when any feature defining the mapped level is in ``disabled_features`` (the
    host-model disable-guard). The level is nominal — see the module docstring.
    """
    level = X86_64_MODEL_LEVELS.get(model)
    if level is None:
        return None
    disabled = set(disabled_features)
    if LEVEL_DEFINING_FEATURES.get(level, frozenset()) & disabled:
        return None
    return level
