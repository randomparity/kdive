"""Local-libvirt gdb-MI wiring: the provider attach seam over the shared engine (ADR-0034/0083).

The gdb-MI engine itself is provider-neutral (``kdive.providers.shared.debug_common.gdbmi``); this
module keeps only local-libvirt's ``default_attach_seam`` (loopback-only via the engine's default
host policy) and its debuginfo resolver. The resolver's orchestration (look up the Run's
``debuginfo_ref``, fail loud on an absent one, materialize the vmlinux) is provider-neutral and
unit-tested with injected seams (``DebuginfoResolver``); only the real DB read and object-store
fetch — and the gdb spawn — are ``live_vm``-real, mirroring the Retrieve/introspect plane split
(ADR-0210 §1).
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

import psycopg

import kdive.config as config
from kdive.config.core_settings import DATABASE_URL
from kdive.db.artifact_queries import debuginfo_ref_for_run_sync
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports import GdbMiAttachment
from kdive.providers.shared.debug_common.crash_postmortem import default_fetch_object
from kdive.providers.shared.debug_common.gdbmi import GdbMiEngine

type _ReadDebuginfoRef = Callable[[str], str | None]
type _FetchObject = Callable[[str], bytes]


class DebuginfoResolver:
    """Resolve + materialize a Run's debuginfo (vmlinux) for the gdb-MI attach seam.

    Mirrors the Retrieve/introspect lookup split: the Run's ``debuginfo_ref`` DB read and the
    object-store fetch are injected seams, so the orchestration (ref lookup, the ``no_debuginfo``
    error, the write) is unit-tested with fakes and only the IO seams are ``live_vm``-real.
    """

    def __init__(
        self, *, read_debuginfo_ref: _ReadDebuginfoRef, fetch_object: _FetchObject
    ) -> None:
        self._read_debuginfo_ref = read_debuginfo_ref
        self._fetch_object = fetch_object

    def resolve(self, run_id: str, dest: Path) -> Path:
        """Fetch the Run's debuginfo (vmlinux) bytes to ``dest`` and return ``dest``.

        Looks the ``debuginfo_ref`` up first; an absent one (no row, or a NULL ``debuginfo_ref``)
        is a legitimate, actionable error raised **before** any fetch — never a silent ``None`` the
        seam would then hand to gdb as a non-existent path, and never a ``MISSING_DEPENDENCY`` that
        would falsely imply a missing host tool. Writes to the ``dest`` it is handed; it derives no
        path from ``run_id``.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` (``reason=no_debuginfo``) when the Run has no
                published debuginfo object; any object-store ``CategorizedError`` raised by the
                fetch seam propagates unchanged.
        """
        ref = self._read_debuginfo_ref(run_id)
        if ref is None:
            raise CategorizedError(
                "the Run has no published debuginfo object; build the kernel before attaching gdb",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"run_id": run_id, "reason": "no_debuginfo"},
            )
        dest.write_bytes(self._fetch_object(ref))
        return dest


def _real_read_debuginfo_ref(run_id: str) -> str | None:  # pragma: no cover - live_vm
    # ``run_id`` is the caller's ``str(session.run_id)`` (a UUID the handler already produced); a
    # non-UUID here is a programming error, not an operational path, so ``UUID()`` is allowed to
    # raise. The conversion lives only in this live DB seam — the resolver never parses ``run_id``.
    with psycopg.connect(config.require(DATABASE_URL)) as conn:
        return debuginfo_ref_for_run_sync(conn, UUID(run_id))


def default_attach_seam(
    *, host: str, port: int, run_id: str, transcript_path: Path
) -> GdbMiAttachment:  # pragma: no cover - live_vm
    """The real ``live_vm`` local attach: resolve+materialize debuginfo, spawn gdb, connect RSP.

    The vmlinux is staged into a private, owner-only directory (``mkdtemp`` defaults to mode
    ``0o700``), not a fixed/predictable name, so a local user cannot pre-create the path (symlink
    attack) and concurrent attaches cannot collide. The directory is removed on **any** failure of
    the resolve or attach; on a successful attach the staged vmlinux outlives this call because the
    live gdb reads symbols from it for the session's lifetime (reclaiming it at session reap is a
    follow-up, not wired here to avoid a shared-dataclass edit).
    """
    staging_dir = Path(tempfile.mkdtemp(prefix="kdive-debuginfo-"))
    resolver = DebuginfoResolver(
        read_debuginfo_ref=_real_read_debuginfo_ref, fetch_object=default_fetch_object
    )
    try:
        vmlinux_path = resolver.resolve(run_id, staging_dir / "vmlinux")
        return GdbMiEngine().attach(
            host=host, port=port, vmlinux_path=vmlinux_path, transcript_path=transcript_path
        )
    except BaseException:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise


__all__ = ["DebuginfoResolver", "GdbMiEngine", "default_attach_seam"]
