"""Provider-neutral gdb-MI debuginfo resolution + staging for the attach seam (ADR-0034/0083).

The gdb-MI attach seam needs the Run's vmlinux on the worker before it can load symbols. The
orchestration — look the Run's ``debuginfo_ref`` up, fail loud on an absent one, materialize the
bytes, stage them into a private per-attach directory, and reclaim that directory on any failure —
is identical for every provider (only the host policy on the engine differs). It lives here so the
local and remote seams share one copy of the security-sensitive staging logic rather than diverging.

The IO seams (the DB read, the object-store fetch, the gdb spawn) are injected, so the
orchestration is unit-tested with fakes and only the real seams are ``live_vm``-real, mirroring the
Retrieve/introspect plane split (ADR-0210 §1).
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

type _ReadDebuginfoRef = Callable[[str], str | None]
type _FetchObject = Callable[[str], bytes]
type _Attach = Callable[[Path], GdbMiAttachment]


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


def real_read_debuginfo_ref(run_id: str) -> str | None:  # pragma: no cover - live_vm
    # ``run_id`` is the caller's ``str(session.run_id)`` (a UUID the handler already produced); a
    # non-UUID here is a programming error, not an operational path, so ``UUID()`` is allowed to
    # raise. The conversion lives only in this live DB seam — the resolver never parses ``run_id``.
    with psycopg.connect(config.require(DATABASE_URL)) as conn:
        return debuginfo_ref_for_run_sync(conn, UUID(run_id))


def stage_and_attach(
    *, run_id: str, attach: _Attach, resolver: DebuginfoResolver | None = None
) -> GdbMiAttachment:
    """Resolve+materialize the Run's debuginfo into a private dir, then ``attach`` against it.

    The vmlinux is staged into a private, owner-only directory (``mkdtemp`` defaults to mode
    ``0o700``), not a fixed/predictable name, so a local user cannot pre-create the path (symlink
    attack) and concurrent attaches cannot collide. The directory is removed on **any** failure of
    the resolve or attach; on a successful attach the staged vmlinux outlives this call because the
    live gdb reads symbols from it for the session's lifetime (reclaiming it at session reap is a
    follow-up, not wired here to avoid a shared-dataclass edit).

    ``attach`` is the provider's seam that spawns gdb and connects the RSP against the staged
    vmlinux path with its own host-policy'd engine (loopback for local, ACL-remote for remote).
    """
    if resolver is None:
        resolver = DebuginfoResolver(
            read_debuginfo_ref=real_read_debuginfo_ref, fetch_object=default_fetch_object
        )
    staging_dir = Path(tempfile.mkdtemp(prefix="kdive-debuginfo-"))
    try:
        vmlinux_path = resolver.resolve(run_id, staging_dir / "vmlinux")
        return attach(vmlinux_path)
    except BaseException:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise


__all__ = ["DebuginfoResolver", "real_read_debuginfo_ref", "stage_and_attach"]
