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

import io
import shutil
import struct
import tarfile
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

import psycopg

import kdive.config as config
from kdive.config.core_settings import DATABASE_URL
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.debug import AttachSeam, GdbMiAttachment
from kdive.providers.shared.debug_common.crash_postmortem import default_fetch_object
from kdive.services.artifacts.read_model import debuginfo_ref_for_run_sync, kernel_ref_for_run_sync

type _ReadDebuginfoRef = Callable[[str], str | None]
type _FetchObject = Callable[[str], bytes]
type _Attach = Callable[[Path], GdbMiAttachment]
type _GdbMiEngineFactory = Callable[[], _GdbMiAttachEngine]
type _ReadKernelRef = Callable[[str], str | None]
type _ReadModuleIdentity = Callable[[Path], tuple[str | None, str | None]]
type ModuleDebuginfoResolverSeam = Callable[[str, str], "ModuleDebuginfo"]


class _GdbMiAttachEngine(Protocol):
    def attach(
        self, *, host: str, port: int, vmlinux_path: Path, transcript_path: Path, run_id: str
    ) -> GdbMiAttachment: ...


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


@dataclass(frozen=True, slots=True)
class ModuleDebuginfo:
    """A staged module ``.ko`` path plus the artifact-side identity for a load (ADR-0278)."""

    path: Path
    srcversion: str | None
    build_id: str | None


class ModuleDebuginfoResolver:
    """Resolve a Run's module ``.ko`` (path + identity) from the combined ``kernel_ref`` tar.

    Lazy and run-id-keyed: the tar is fetched/extracted once per ``run_id`` on the first
    ``resolve`` and cached, so a debug session that never loads module symbols pays nothing. The
    DB read, object fetch, and `.ko` ELF identity parse are injected seams (only the real ones are
    ``live_vm``), mirroring :class:`DebuginfoResolver`.
    """

    def __init__(
        self,
        *,
        read_kernel_ref: _ReadKernelRef,
        fetch_object: _FetchObject,
        read_identity: _ReadModuleIdentity,
    ) -> None:
        self._read_kernel_ref = read_kernel_ref
        self._fetch_object = fetch_object
        self._read_identity = read_identity
        self._staged: dict[str, Path] = {}

    def resolve(self, run_id: str, module: str) -> ModuleDebuginfo:
        """Stage the modules tree (cached) and return the ``<module>.ko`` path + identity.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` (``reason=no_module_debuginfo``) when the Run
                has no ``kernel_ref`` or the tree holds no matching ``.ko``.
        """
        root = self._stage(run_id, module)
        ko = self._locate_ko(root, module)
        if ko is None:
            raise self._missing(run_id, module)
        srcversion, build_id = self._read_identity(ko)
        return ModuleDebuginfo(path=ko, srcversion=srcversion, build_id=build_id)

    def _stage(self, run_id: str, module: str) -> Path:
        cached = self._staged.get(run_id)
        if cached is not None:
            return cached
        ref = self._read_kernel_ref(run_id)
        if ref is None:
            raise self._missing(run_id, module)
        root = Path(tempfile.mkdtemp(prefix="kdive-modules-"))
        with tarfile.open(fileobj=io.BytesIO(self._fetch_object(ref)), mode="r:*") as tar:
            members = [m for m in tar.getmembers() if _is_modules_member(m.name)]
            tar.extractall(root, members=members, filter="data")
        self._staged[run_id] = root
        return root

    def _locate_ko(self, root: Path, module: str) -> Path | None:
        names = {f"{module}.ko", f"{module.replace('_', '-')}.ko"}
        for path in root.rglob("*.ko"):
            if path.name in names:
                return path
        return None

    def _missing(self, run_id: str, module: str) -> CategorizedError:
        return CategorizedError(
            "no matching module debuginfo (.ko) for the Run; build the kernel with "
            "CONFIG_DEBUG_INFO=y and ensure the module is built and published",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"run_id": run_id, "module": module, "reason": "no_module_debuginfo"},
        )


def _is_modules_member(name: str) -> bool:
    return name.lstrip("./").startswith("lib/modules/")


def real_read_kernel_ref(run_id: str) -> str | None:  # pragma: no cover - live_vm
    with psycopg.connect(config.require(DATABASE_URL)) as conn:
        return kernel_ref_for_run_sync(conn, UUID(run_id))


def real_module_debuginfo_resolver() -> ModuleDebuginfoResolverSeam:  # pragma: no cover - live_vm
    """The production module-debuginfo resolver seam wired to the live DB/object-store/ELF seams.

    Returns the bound ``resolve`` so the engine seam stays a plain ``(run_id, module)`` callable;
    the closed-over resolver instance keeps its per-run staging cache alive across calls.
    """
    return ModuleDebuginfoResolver(
        read_kernel_ref=real_read_kernel_ref,
        fetch_object=default_fetch_object,
        read_identity=read_module_identity,
    ).resolve


def read_module_identity(ko: Path) -> tuple[str | None, str | None]:  # pragma: no cover - live_vm
    """Read a ``.ko``'s ``srcversion`` (`.modinfo`) and build-id (`.note.gnu.build-id`)."""
    return parse_module_identity(ko.read_bytes())


def parse_module_identity(data: bytes) -> tuple[str | None, str | None]:
    """Parse ``(srcversion, build_id)`` from a ``.ko`` ELF64 image; ``(None, None)`` if absent.

    Pure (no IO) so the section/note parsing is unit-tested directly; the file read lives in the
    ``live_vm`` :func:`read_module_identity` wrapper.
    """
    sections = _elf_sections(data)
    srcversion = _modinfo_value(_section_bytes(data, sections.get(b".modinfo")), b"srcversion")
    build_id = _build_id_hex(_section_bytes(data, sections.get(b".note.gnu.build-id")))
    return srcversion, build_id


def _elf_sections(data: bytes) -> dict[bytes, tuple[int, int]]:
    """Map ELF64 section name -> (offset, size); empty on a non-ELF64/little-endian image."""
    if len(data) < 64 or data[:4] != b"\x7fELF" or data[4] != 2:
        return {}
    # ELF64: e_shoff (Q) at 0x28; e_shentsize/e_shnum/e_shstrndx (H) at 0x3a/0x3c/0x3e — 10 pad.
    sh_off, sh_entsize, sh_num, sh_strndx = struct.unpack_from("<Q10xHHH", data, 0x28)
    headers = [struct.unpack_from("<IIQQQQ", data, sh_off + i * sh_entsize) for i in range(sh_num)]
    if sh_strndx >= len(headers):
        return {}
    str_off, str_size = headers[sh_strndx][4], headers[sh_strndx][5]
    strtab = data[str_off : str_off + str_size]
    sections: dict[bytes, tuple[int, int]] = {}
    for sh_name, _type, _flags, _addr, sh_offset, sh_size in headers:
        end = strtab.find(b"\x00", sh_name)
        sections[strtab[sh_name:end]] = (sh_offset, sh_size)
    return sections


def _section_bytes(data: bytes, span: tuple[int, int] | None) -> bytes:
    if span is None:
        return b""
    offset, size = span
    return data[offset : offset + size]


def _modinfo_value(blob: bytes, key: bytes) -> str | None:
    prefix = key + b"="
    for entry in blob.split(b"\x00"):
        if entry.startswith(prefix):
            return entry[len(prefix) :].decode("utf-8", "replace")
    return None


def _build_id_hex(note: bytes) -> str | None:
    if len(note) < 12:
        return None
    namesz, descsz, _ntype = struct.unpack_from("<III", note, 0)
    desc_start = 12 + ((namesz + 3) & ~3)
    desc = note[desc_start : desc_start + descsz]
    return desc.hex() if desc else None


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


def gdb_attach_seam(*, engine_factory: _GdbMiEngineFactory) -> AttachSeam:
    """Build a provider attach seam around shared debuginfo staging and a gdb-MI engine.

    Local and remote providers differ only in how the engine is constructed: local uses the
    loopback host policy default, remote supplies its ACL policy. The debuginfo lookup, private
    staging directory, and failure cleanup stay here with the tested staging logic.
    """

    def attach_seam(*, host: str, port: int, run_id: str, transcript_path: Path) -> GdbMiAttachment:
        def attach(vmlinux_path: Path) -> GdbMiAttachment:
            return engine_factory().attach(
                host=host,
                port=port,
                vmlinux_path=vmlinux_path,
                transcript_path=transcript_path,
                run_id=run_id,
            )

        return stage_and_attach(run_id=run_id, attach=attach)

    return attach_seam


__all__ = [
    "DebuginfoResolver",
    "ModuleDebuginfo",
    "ModuleDebuginfoResolver",
    "gdb_attach_seam",
    "parse_module_identity",
    "read_module_identity",
    "real_module_debuginfo_resolver",
    "real_read_debuginfo_ref",
    "stage_and_attach",
]
