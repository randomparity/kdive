"""Real drgn-backed seams for the worker-side vmcore introspection ports (ADR-0033/0083).

The introspection ports keep drgn behind injected ``open_program``/``run_helper`` seams so
unit tests never import it. These are the production implementations: drgn is imported
lazily inside the seam, so composition still builds on hosts without it and the port
surfaces the documented ``MISSING_DEPENDENCY`` instead of an ``ImportError``.

``read_vmcoreinfo_build_id`` reads the crashed kernel's GNU build-id from the VMCOREINFO
note (its ``BUILD-ID=`` line, present since v5.13) rather than from ELF section notes —
a kdump core carries VMCOREINFO but not the kernel image's own ``.notes`` section.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.shared.debug_common.introspect import (
    helper_modules,
    helper_sysinfo,
    helper_tasks,
)

# VMCOREINFO sits in a PT_NOTE near the start of the core; bound the scan so a
# pathological core cannot make provenance verification quadratic.
_VMCOREINFO_SCAN_BYTES = 64 * 1024 * 1024
_BUILD_ID_LINE = re.compile(rb"BUILD-ID=([0-9a-f]{40})")


def read_vmcoreinfo_build_id(data: bytes) -> str:
    """The crashed kernel's GNU build-id from the core's VMCOREINFO ``BUILD-ID=`` line.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when no build-id line is present —
            an ELF-format kdump core always carries VMCOREINFO, so its absence means
            the capture path produced something this platform cannot verify.
    """
    match = _BUILD_ID_LINE.search(data[:_VMCOREINFO_SCAN_BYTES])
    if match is None:
        raise CategorizedError(
            "vmcore carries no VMCOREINFO BUILD-ID line; cannot verify provenance",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return match.group(1).decode("ascii")


def _require_drgn() -> Any:
    try:
        import drgn  # noqa: PLC0415  # ty: ignore[unresolved-import]  # operator-provided
    except ImportError as exc:
        raise CategorizedError(
            "drgn is not installed on this worker host; offline introspection needs it",
            category=ErrorCategory.MISSING_DEPENDENCY,
        ) from exc
    return drgn


class _DrgnTask:
    def __init__(self, prog: Any, task: Any) -> None:
        self._prog = prog
        self._task = task

    def pid(self) -> int:
        return int(self._task.pid)

    def tgid(self) -> int:
        return int(self._task.tgid)

    def comm(self) -> str:
        return self._task.comm.string_().decode("utf-8", "replace")

    def state(self) -> str:
        from drgn.helpers.linux import sched  # noqa: PLC0415  # ty: ignore[unresolved-import]

        return sched.task_state_to_char(self._task)

    def kernel_stack(self) -> list[str]:
        trace = self._prog.stack_trace(self._task)
        return [str(frame) for frame in trace]


class _DrgnModule:
    def __init__(self, module: Any) -> None:
        self._module = module

    def name(self) -> str:
        return self._module.name.string_().decode("utf-8", "replace")

    def size(self) -> int:
        try:
            return int(self._module.mem[0].size)
        except Exception:  # noqa: BLE001 - layout varies by kernel; size is advisory
            return 0

    def refcount(self) -> int:
        return int(self._module.refcnt.counter)

    def used_by(self) -> list[str]:
        return []

    def state(self) -> str:
        return str(int(self._module.state))


class DrgnProgramAdapter:
    """Adapt a ``drgn.Program`` to the introspection helpers' ``_Program`` protocol."""

    def __init__(self, prog: Any) -> None:
        self._prog = prog

    def iter_tasks(self) -> list[object]:
        from drgn.helpers.linux import pid  # noqa: PLC0415  # ty: ignore[unresolved-import]

        return [_DrgnTask(self._prog, task) for task in pid.for_each_task(self._prog)]

    def iter_modules(self) -> list[object]:
        from drgn.helpers.linux import module  # noqa: PLC0415  # ty: ignore[unresolved-import]

        return [_DrgnModule(mod) for mod in module.for_each_module(self._prog)]

    def uts(self) -> dict[str, str]:
        name = self._prog["init_uts_ns"].name
        return {
            "release": name.release.string_().decode("utf-8", "replace"),
            "version": name.version.string_().decode("utf-8", "replace"),
            "machine": name.machine.string_().decode("utf-8", "replace"),
            "nodename": name.nodename.string_().decode("utf-8", "replace"),
        }

    def boot_cmdline(self) -> str:
        return self._prog["saved_command_line"].string_().decode("utf-8", "replace")

    def cpus_online(self) -> int:
        from drgn.helpers.linux import cpumask  # noqa: PLC0415  # ty: ignore[unresolved-import]

        return sum(1 for _ in cpumask.for_each_online_cpu(self._prog))

    def mem_total_pages(self) -> int:
        try:
            return int(self._prog["_totalram_pages"].counter)
        except Exception:  # noqa: BLE001 - symbol name varies by version; advisory counter
            return 0


# libkdumpfile (through 0.5.6) maps ppc64 Linux with the pre-4.x hash layout: all of
# 0xc000000000000000-0xcfffffffffffffff is the linear map, vmalloc is at 0xd000..., vmemmap at
# 0xf000.... Book3S-64 kernels put vmalloc, kernel I/O and vmemmap above __vmalloc_start
# (0xc008...), so every kdump-compressed read there fails "No way to translate" (#2763). drgn
# gives a later segment priority, so the non-linear tail of the 0xc region is routed through
# drgn's own page-table walk. Remove once a libkdumpfile release carries the layout fix.
_PPC64_LINEAR_REGION_END = 0xD000000000000000


def _page_split_reader(
    translate: Callable[[int], int],
    read_physical: Callable[[int, int], bytes],
    page_size: int,
) -> Callable[[int, int, int, bool], bytes]:
    """A drgn segment read function that translates and reads one page at a time."""

    def read(address: int, count: int, offset: int, physical: bool) -> bytes:
        out = bytearray()
        while len(out) < count:
            chunk = min(count - len(out), page_size - address % page_size)
            out += read_physical(translate(address), chunk)
            address += chunk
        return bytes(out)

    return read


def _route_ppc64_vmalloc(drgn: Any, prog: Any, follow_phys: Callable[[Any, int], int]) -> None:
    """Serve ppc64 vmalloc/I/O/vmemmap reads from page tables instead of libkdumpfile (#2763)."""
    if prog.platform.arch != drgn.Architecture.PPC64:
        return
    try:
        start = prog["__vmalloc_start"].value_()
    except KeyError:  # pre-Book3S-64-layout kernel: libkdumpfile's map is right for it
        return
    init_mm = prog["init_mm"].address_of_()
    reader = _page_split_reader(
        lambda address: follow_phys(init_mm, address),
        lambda address, count: prog.read(address, count, True),
        prog["PAGE_SIZE"].value_(),
    )
    prog.add_memory_segment(start, _PPC64_LINEAR_REGION_END - start, reader)


def open_vmcore_program(core: Path, vmlinux: Path) -> DrgnProgramAdapter:
    """Open a drgn program over a staged vmcore + vmlinux pair (the ``open_program`` seam)."""
    drgn = _require_drgn()
    from drgn.helpers.linux.mm import (  # noqa: PLC0415  # ty: ignore[unresolved-import]
        follow_phys,
    )

    prog = drgn.Program()
    prog.set_core_dump(core)
    prog.load_debug_info([vmlinux])
    _route_ppc64_vmalloc(drgn, prog, follow_phys)
    return DrgnProgramAdapter(prog)


def run_introspection_helper(program: Any, name: str) -> dict[str, object]:
    """Dispatch one fixed helper by name (the ``run_helper`` seam)."""
    helpers = {"tasks": helper_tasks, "modules": helper_modules, "sysinfo": helper_sysinfo}
    try:
        helper = helpers[name]
    except KeyError:
        raise CategorizedError(
            f"unknown introspection helper: {name}",
            category=ErrorCategory.CONFIGURATION_ERROR,
        ) from None
    return helper(program)
