"""The build orchestrator's sub-phase vocabulary (ADR-0191 G1).

Bounds the ``build_phase`` metric label: a low-cardinality enum naming the distinct
timed stages of a kernel build (provision a build host, sync source, configure, compile,
install modules, extract artifacts). Never a per-object identifier.
"""

from __future__ import annotations

from enum import StrEnum


class BuildPhase(StrEnum):
    """A timed sub-phase of the build pipeline (ADR-0191 G1)."""

    PROVISION = "provision"
    SOURCE_SYNC = "source_sync"
    CONFIGURE = "configure"
    COMPILE = "compile"
    MODULES = "modules"
    ARTIFACT = "artifact"
