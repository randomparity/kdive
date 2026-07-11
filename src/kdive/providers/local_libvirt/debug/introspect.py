"""Offline drgn introspection of a captured vmcore on the host (ADR-0033).

`LocalLibvirtVmcoreIntrospect` realizes the `VmcoreIntrospector` port, mirroring
`LocalLibvirtRetrieve`'s `CrashPostmortem`: fetching the raw core + `vmlinux` from the
object store, verifying the core's build-id against the Run's recorded build-id
(provenance), opening drgn against the staged core, and running three fixed helpers
(tasks, modules, sysinfo). `from_env` wires the real shared drgn seams (ADR-0210 §2); the
orchestration, provenance, dispatch, byte-cap, and redaction stay unit-tested with a fake
`_Program`, and the drgn open itself runs only under the `live_vm` gate. The assembled report
is `Redactor`-scrubbed **inside the port** — the port is the single redaction boundary, so any
later persistence is of already-redacted text. The real drgn package is an operator-provided
live-host prerequisite, not a normal service dependency: the open seam imports it lazily, so a
host without drgn surfaces a `MISSING_DEPENDENCY` from the open seam, not an ``ImportError``.
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

# --- LocalLibvirtVmcoreIntrospect (the realized port) --------------------------------------

type _FetchObject = Callable[[str], bytes]
type _ReadBuildId = Callable[[bytes], str]
type _OpenProgram = Callable[[Path, Path], _Program]
type _RunHelper = Callable[[_Program, str], dict[str, object]]


class LocalLibvirtVmcoreIntrospect:
    """The realized offline-introspection port (ADR-0033).

    Stages the raw core + ``vmlinux`` from the object store, verifies the core's build-id
    against the Run's recorded build-id (provenance), opens drgn against the staged core
    (``live_vm`` seam), runs the three helpers, redacts and byte-caps the assembled report,
    and returns it — the port is the single redaction boundary.

    ``from_env`` wires the real ``open_program``/``run_helper`` seams; on a host without drgn the
    open seam raises ``MISSING_DEPENDENCY`` (it imports drgn lazily). A test may still pass ``None``
    seams to exercise the off-gate guard, which raises ``MISSING_DEPENDENCY`` before touching the
    store, mirroring ``LocalLibvirtRetrieve.run``'s seam guard.
    """

    def __init__(
        self,
        *,
        fetch_object: _FetchObject,
        read_vmcore_build_id: _ReadBuildId,
        secret_registry: SecretRegistry,
        open_program: _OpenProgram | None = None,
        run_helper: _RunHelper | None = None,
    ) -> None:
        self._fetch_object = fetch_object
        self._read_vmcore_build_id = read_vmcore_build_id
        self._secret_registry = secret_registry
        self._open_program = open_program
        self._run_helper = run_helper
        self._report_byte_cap = _REPORT_BYTE_CAP

    @classmethod
    def from_env(cls, *, secret_registry: SecretRegistry) -> LocalLibvirtVmcoreIntrospect:
        """Build from env with the real drgn seams (lazy: drgn imports on first use).

        drgn stays an operator-provided live-host prerequisite — the open seam imports it inside
        the call, so composition builds on hosts without it and ``from_vmcore`` raises the
        documented ``MISSING_DEPENDENCY`` from the open seam (not an up-front ``None`` guard).
        """
        # ``open_vmcore_program`` returns ``DrgnProgramAdapter`` (its ``iter_*`` are typed
        # ``list[object]``); cast it to the seam alias whose ``_Program`` reads the same surface
        # with the narrower helper-facing element types. ``run_introspection_helper`` accepts
        # ``Any`` for ``program`` so it needs no cast.
        return cls(
            fetch_object=_real_fetch_object,
            read_vmcore_build_id=read_vmcoreinfo_build_id,
            secret_registry=secret_registry,
            open_program=cast("_OpenProgram", open_vmcore_program),
            run_helper=run_introspection_helper,
        )

    def from_vmcore(
        self, *, vmcore_ref: str, debuginfo_ref: str, expected_build_id: str
    ) -> IntrospectOutput:
        """Open the core, run the helpers, and return a redacted, size-bounded report.

        Raises:
            CategorizedError: ``MISSING_DEPENDENCY`` if the drgn seams were not configured
                (off-gate); ``CONFIGURATION_ERROR`` for a malformed ref rejected by an
                injected fetch/build-id seam or a build-id provenance mismatch;
                ``STALE_HANDLE`` when a referenced object is missing;
                ``INFRASTRUCTURE_FAILURE`` for object-store IO failures; or
                ``DEBUG_ATTACH_FAILURE`` if drgn cannot open the core or load the vmlinux.
        """
        if self._open_program is None or self._run_helper is None:
            raise CategorizedError(
                "offline drgn introspection runs only under the live_vm gate",
                category=ErrorCategory.MISSING_DEPENDENCY,
            )
        vmcore_bytes = self._fetch_object(vmcore_ref)
        self._verify_provenance(vmcore_bytes, expected_build_id, vmcore_ref)
        vmlinux_bytes = self._fetch_object(debuginfo_ref)
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


def _real_fetch_object(ref: str) -> bytes:  # pragma: no cover - live_vm
    from kdive.store.objectstore import object_store_from_env

    # The ref is a key the system itself produced; there is no client etag handle, so the
    # read is unconditional (ADR-0054). An empty etag would 412 here, not skip the check.
    return object_store_from_env().get_artifact(ref, None).data


__all__ = [
    "IntrospectOutput",
    "LocalLibvirtVmcoreIntrospect",
    "VmcoreIntrospector",
]
