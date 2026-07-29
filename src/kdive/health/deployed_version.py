"""The deployed-build fact the aux listener reports on ``/readyz`` (ADR-0482 §1).

A running deployment otherwise says nothing about how far behind the working tree it is, and a
stale deployment is symptomatically identical to a defect (#1630, found proving #1610). This
module answers "which build is this process running, and since when" from the **existing**
single source — :func:`kdive.version.version_info` (ADR-0041/0370: baked ``_buildinfo`` first,
live git second, unknown last) — so the aux listener opens no fourth resolution path.

``started_at`` is what makes the *local* variant detectable. The live-stack app tier is three
plain Python processes with no hot reload, so a source edit does not reach a running process
until it is restarted; a source file whose mtime is later than ``started_at`` is one the running
process provably did not load.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from kdive.version import version_info

_ISO_8601_UTC = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True, slots=True)
class DeployedVersion:
    """The build a process is running, plus the instant it started.

    Args:
        version: The package version (``version_info().version``).
        commit: The build's commit, or ``None`` when it could not be resolved.
        is_release: Whether the build came from a clean, exactly-tagged tree.
        started_at: ISO-8601 UTC, second precision, ``Z``-suffixed.
    """

    version: str
    commit: str | None
    is_release: bool
    started_at: str

    def payload(self) -> dict[str, object]:
        """The ``version`` object serialized into the ``/readyz`` body."""
        return {
            "version": self.version,
            "commit": self.commit,
            "is_release": self.is_release,
            "started_at": self.started_at,
        }


def deployed_version(*, now: Callable[[], datetime] | None = None) -> DeployedVersion:
    """Resolve this process's build identity, stamped with the current instant.

    Args:
        now: Clock returning a timezone-aware ``datetime``; injected by tests. Defaults to
            :func:`datetime.now` in UTC.
    """
    instant = now() if now is not None else datetime.now(UTC)
    info = version_info()
    return DeployedVersion(
        version=info.version,
        commit=info.commit,
        is_release=info.is_release,
        started_at=instant.astimezone(UTC).strftime(_ISO_8601_UTC),
    )
