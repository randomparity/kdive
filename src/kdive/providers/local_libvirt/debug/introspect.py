"""Local-libvirt offline vmcore drgn introspection (ADR-0033).

Live SSH-backed drgn introspection lives in ``live_introspect.py``.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import cast

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.retrieve import (
    IntrospectOutput,
    VmcoreIntrospector,
)
from kdive.providers.shared.debug_common.drgn_program import (
    open_vmcore_program,
    read_vmcoreinfo_build_id,
    run_introspection_helper,
)
from kdive.providers.shared.debug_common.introspect import (
    _REPORT_BYTE_CAP,
    _Program,
    assemble_report,
)
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.assembly import UNCONFIGURED_OBJECT_STORE
from kdive.store.objectstore import ObjectStore

# --- LocalLibvirtVmcoreIntrospect (the realized port) --------------------------------------

type _FetchObject = Callable[[str], bytes]
type _FetchVersionedObject = Callable[[str, str], bytes]
type _ReadBuildId = Callable[[bytes], str]
type _OpenProgram = Callable[[Path, Path], _Program]
type _RunHelper = Callable[[_Program, str], dict[str, object]]


class LocalLibvirtVmcoreIntrospect:
    """Realizes the offline ``VmcoreIntrospector`` port (ADR-0033)."""

    def __init__(
        self,
        *,
        fetch_object: _FetchObject,
        fetch_versioned_object: _FetchVersionedObject | None = None,
        read_vmcore_build_id: _ReadBuildId,
        secret_registry: SecretRegistry,
        open_program: _OpenProgram | None = None,
        run_helper: _RunHelper | None = None,
    ) -> None:
        self._fetch_object = fetch_object
        self._fetch_versioned_object = fetch_versioned_object
        self._read_vmcore_build_id = read_vmcore_build_id
        self._secret_registry = secret_registry
        self._open_program = open_program
        self._run_helper = run_helper
        self._report_byte_cap = _REPORT_BYTE_CAP

    @classmethod
    def from_env(
        cls, *, secret_registry: SecretRegistry, store: ObjectStore = UNCONFIGURED_OBJECT_STORE
    ) -> LocalLibvirtVmcoreIntrospect:
        """Build with real drgn seams.

        An absent package raises ``MISSING_DEPENDENCY`` on first use.
        """
        # ``open_vmcore_program`` returns ``DrgnProgramAdapter`` (its ``iter_*`` are typed
        # ``list[object]``); cast it to the seam alias whose ``_Program`` reads the same surface
        # with the narrower helper-facing element types. ``run_introspection_helper`` accepts
        # ``Any`` for ``program`` so it needs no cast.
        return cls(
            fetch_object=lambda ref: store.get_artifact(ref, None).data,
            fetch_versioned_object=lambda ref, version_id: (
                store.get_artifact(ref, None, version_id=version_id).data
            ),
            read_vmcore_build_id=read_vmcoreinfo_build_id,
            secret_registry=secret_registry,
            open_program=cast("_OpenProgram", open_vmcore_program),
            run_helper=run_introspection_helper,
        )

    def from_vmcore(
        self,
        *,
        vmcore_ref: str,
        debuginfo_ref: str,
        debuginfo_version_id: str | None = None,
        expected_build_id: str,
    ) -> IntrospectOutput:
        """Fetch and verify the core, fetch debuginfo, stage both, run helpers, and return the
        shared report assembler's redacted, byte-capped report.

        Raises:
            CategorizedError: ``MISSING_DEPENDENCY`` if the drgn seams were not configured
                (off-gate); ``CONFIGURATION_ERROR`` for a malformed ref reported by an injected
                fetch/build-id seam or a build-id provenance mismatch;
                ``STALE_HANDLE`` when a referenced object is missing;
                ``INFRASTRUCTURE_FAILURE`` for object-store IO failures; or
                ``DEBUG_ATTACH_FAILURE`` if drgn cannot open the core or load the vmlinux.
            RuntimeError: if versioned debuginfo is requested without a versioned fetch seam.
        """
        if self._open_program is None or self._run_helper is None:
            raise CategorizedError(
                "offline drgn introspection runs only under the live_vm gate",
                category=ErrorCategory.MISSING_DEPENDENCY,
            )
        vmcore_bytes = self._fetch_object(vmcore_ref)
        self._verify_provenance(vmcore_bytes, expected_build_id, vmcore_ref)
        if debuginfo_version_id is None:
            vmlinux_bytes = self._fetch_object(debuginfo_ref)
        elif self._fetch_versioned_object is not None:
            vmlinux_bytes = self._fetch_versioned_object(debuginfo_ref, debuginfo_version_id)
        else:
            raise RuntimeError("versioned drgn debuginfo fetch seam is not configured")
        with (
            tempfile.NamedTemporaryFile(suffix=".vmcore") as core_file,
            tempfile.NamedTemporaryFile(suffix=".vmlinux") as vmlinux_file,
        ):
            core_file.write(vmcore_bytes)
            core_file.flush()
            vmlinux_file.write(vmlinux_bytes)
            vmlinux_file.flush()
            program = self._open(self._open_program, Path(core_file.name), Path(vmlinux_file.name))
            tasks = self._run_helper(program, "tasks")
            modules = self._run_helper(program, "modules")
            sysinfo = self._run_helper(program, "sysinfo")
        return self._assemble(tasks, modules, sysinfo)

    def _verify_provenance(self, vmcore_bytes: bytes, expected: str, vmcore_ref: str) -> None:
        observed = self._read_vmcore_build_id(vmcore_bytes)
        if observed != expected:
            raise CategorizedError(
                "captured vmcore build-id does not match the Run's debuginfo build-id",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"vmcore_ref": vmcore_ref},
            )

    @staticmethod
    def _open(open_program: _OpenProgram, core: Path, vmlinux: Path) -> _Program:
        try:
            return open_program(core, vmlinux)
        except CategorizedError:
            raise
        except Exception as exc:  # noqa: BLE001 - any drgn open fault becomes a typed attach failure
            raise CategorizedError(
                "drgn could not open the vmcore against the supplied vmlinux",
                category=ErrorCategory.DEBUG_ATTACH_FAILURE,
            ) from exc

    def _assemble(
        self,
        tasks: dict[str, object],
        modules: dict[str, object],
        sysinfo: dict[str, object],
    ) -> IntrospectOutput:
        return assemble_report(
            tasks,
            modules,
            sysinfo,
            byte_cap=self._report_byte_cap,
            secret_registry=self._secret_registry,
        )


__all__ = [
    "IntrospectOutput",
    "LocalLibvirtVmcoreIntrospect",
    "VmcoreIntrospector",
]
