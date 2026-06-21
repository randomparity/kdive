# Local-libvirt Tier 3 kdump capture — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `CaptureMethod.KDUMP` a working capture method on the local-libvirt provider by harvesting the guest-written `/var/crash/<ts>/vmcore` host-side from the System's own qcow2 overlay.

**Architecture:** A local QEMU domain runs on the worker host, so its kdump core lands on a host-owned qcow2 overlay. The live seam force-stops the domain (`destroy`, idempotent — `vmcore.fetch` admits only on `CRASHED`), read-only-mounts the overlay via libguestfs, selects the newest `/var/crash/*/vmcore`, and reads its bytes. Build-id and redacted dmesg come from drgn helpers shared with remote host_dump. Selection, size-cap, dmesg-degrade, and redaction are pure and unit-tested; the libguestfs/`domain.destroy()`/drgn calls are the only `live_vm`-gated edge.

**Tech Stack:** Python 3.14, `uv`, pytest, libvirt-python, drgn, libguestfs (`guestfs` Python binding — host prerequisite for local kdump only).

**Spec:** `docs/design/local-libvirt-kdump-capture.md` · **ADR:** `docs/adr/0203-local-libvirt-kdump-overlay-harvest.md`

## Global Constraints

- Ruff line length 100; lint set `E,F,I,UP,B,SIM`; `ty` strict whole-tree.
- ≤100 lines/function, cyclomatic complexity ≤8, ≤5 positional params, absolute imports only.
- Every provider error is a `CategorizedError` with the most specific `ErrorCategory`; never invent strings.
- All guest output (dmesg) passes the `Redactor` before persistence (CLAUDE.md redaction invariant).
- Hardware/drgn/libguestfs calls are `# pragma: no cover - live_vm`; logic is pure and unit-tested with fakes.
- Guardrails before every commit: `just lint`, `just type`, and the focused test for the task. Full `just ci` before the first push.
- ADR-0203 cites no `src/` yet (status Proposed). The commit that first cites it in `src/` must flip ADR-0203 to **Accepted** in both the ADR file and the `docs/adr/README.md` row (adr-status-check gate, no exemption).
- Conventional-commit subjects ≤72 chars, ending with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

### Task 1: Extract shared drgn core-file helpers into `debug_common`

Move the drgn core-file readers, the dmesg sentinel, and the 5 GiB ceiling out of the remote
host_dump module into the shared `debug_common` package so both providers use one copy
(ADR-0203 consequence: a fix to either benefits both). Pure refactor — the existing remote
tests are the safety net; no behavior change.

**Files:**
- Create: `src/kdive/providers/shared/debug_common/core_file.py`
- Modify: `src/kdive/providers/remote_libvirt/retrieve/host_dump_capture.py` (delete the moved defs, import from shared)
- Modify: `src/kdive/providers/remote_libvirt/retrieve/common.py:35` (re-import `MAX_CORE_BYTES` from shared so `kdump_capture.py` / `facade.py` keep importing it from `..common`)
- Modify: `tests/providers/remote_libvirt/retrieve/test_retrieve_host_dump.py:36-43` (import `DMESG_UNAVAILABLE` and any moved helper from the shared module)

**Interfaces:**
- Produces (for Tasks 2-4):
  - `MAX_CORE_BYTES: int` (= `5 * 1024**3`)
  - `DMESG_UNAVAILABLE: bytes`
  - `open_core_program(core: Path) -> Any` (drgn, pragma)
  - `read_core_build_id_from_file(core: Path) -> str` (drgn, pragma)
  - `read_core_dmesg_from_file(core: Path) -> bytes` (drgn, pragma)

- [ ] **Step 1: Run the remote retrieve tests to confirm the baseline is green**

Run: `uv run python -m pytest tests/providers/remote_libvirt/retrieve/ -q`
Expected: PASS (this is the refactor safety net).

- [ ] **Step 2: Create the shared module by moving the symbols verbatim**

Create `src/kdive/providers/shared/debug_common/core_file.py` containing exactly the
following, copied verbatim from `remote_libvirt/retrieve/host_dump_capture.py` (lines 47-100)
and `remote_libvirt/retrieve/common.py:35`:

```python
"""Shared drgn vmcore-file helpers (ADR-0203): build-id + dmesg from a core on disk.

Used by both providers' Retrieve planes. The drgn calls are live_vm-gated; the surrounding
provider code injects these as seams so the orchestration is unit-tested with fakes.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from kdive.domain.errors import CategorizedError, ErrorCategory

MAX_CORE_BYTES = 5 * 1024**3

DMESG_UNAVAILABLE = (
    b"[kdive] dmesg could not be extracted from this core "
    b"(kernel debuginfo required); see the crash postmortem for the kernel log\n"
)


def open_core_program(core: Path) -> Any:  # pragma: no cover - live_vm (drgn)
    try:
        import drgn  # noqa: PLC0415  # ty: ignore[unresolved-import]  # operator-provided
    except ImportError as exc:
        raise CategorizedError(
            "drgn is not installed on this worker host; core build-id/dmesg needs it",
            category=ErrorCategory.MISSING_DEPENDENCY,
        ) from exc
    prog = drgn.Program()
    prog.set_core_dump(os.fspath(core))
    return prog


def read_core_build_id_from_file(core: Path) -> str:  # pragma: no cover - live_vm (drgn)
    """The crashed kernel's GNU build-id from a compressed-kdump core's VMCOREINFO."""
    prog = open_core_program(core)
    vmcoreinfo = bytes(prog["VMCOREINFO"].value_())
    match = re.search(rb"BUILD-ID=([0-9a-f]{40})", vmcoreinfo)
    if match is None:
        raise CategorizedError(
            "core carries no VMCOREINFO BUILD-ID line; cannot verify provenance",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return match.group(1).decode("ascii")


def read_core_dmesg_from_file(core: Path) -> bytes:  # pragma: no cover - live_vm (drgn)
    """The kernel log buffer from an ELF/kdump core (drgn ``get_dmesg``)."""
    from drgn.helpers.linux.printk import (  # noqa: PLC0415  # ty: ignore[unresolved-import]
        get_dmesg,
    )

    prog = open_core_program(core)
    try:
        return get_dmesg(prog)
    except Exception as exc:
        raise CategorizedError(
            "could not extract dmesg from the core; the printk ring buffer needs the "
            "guest kernel's debuginfo, which is not loaded at capture time",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        ) from exc
```

- [ ] **Step 3: Delete the moved defs from remote host_dump and import from shared**

In `src/kdive/providers/remote_libvirt/retrieve/host_dump_capture.py`: delete the
`DMESG_UNAVAILABLE` constant (lines 47-50), `open_core_program` (60-70),
`read_core_build_id_from_file` (73-83), and `read_core_dmesg_from_file` (86-100). Remove the
now-unused `import os`, `import re` if nothing else uses them (check first). Add:

```python
from kdive.providers.shared.debug_common.core_file import (
    DMESG_UNAVAILABLE,
    read_core_build_id_from_file,
    read_core_dmesg_from_file,
)
```

In `src/kdive/providers/remote_libvirt/retrieve/common.py`, replace the
`MAX_CORE_BYTES = 5 * 1024**3` definition (line 35) with a re-import so its existing importers
(`kdump_capture.py:28`, `facade.py:20`) are unchanged:

```python
from kdive.providers.shared.debug_common.core_file import MAX_CORE_BYTES
```

Keep `MAX_CORE_BYTES` in `common.py`'s `__all__` if it has one.

- [ ] **Step 4: Update the remote host_dump test import**

In `tests/providers/remote_libvirt/retrieve/test_retrieve_host_dump.py`, change the import of
`DMESG_UNAVAILABLE` (and any moved helper it references) from
`...retrieve.host_dump_capture` to
`kdive.providers.shared.debug_common.core_file`.

- [ ] **Step 5: Run guardrails + the remote retrieve tests; confirm still green**

Run: `uv run python -m pytest tests/providers/remote_libvirt/retrieve/ -q && just lint && just type`
Expected: PASS, zero warnings (behavior unchanged; symbols relocated).

- [ ] **Step 6: Commit**

```bash
git add src/kdive/providers/shared/debug_common/core_file.py \
        src/kdive/providers/remote_libvirt/retrieve/host_dump_capture.py \
        src/kdive/providers/remote_libvirt/retrieve/common.py \
        tests/providers/remote_libvirt/retrieve/test_retrieve_host_dump.py
git commit -m "refactor(capture): share drgn core-file helpers in debug_common

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Pure kdump-harvest selection + size cap

The pure core of the harvest: given a `GuestCoreReader` over the overlay, pick the newest
`/var/crash/*/vmcore`, enforce the 5 GiB ceiling, and read its bytes — or return `None` when
no core is present (which `LocalLibvirtRetrieve.capture` already maps to `READINESS_FAILURE`).

**Files:**
- Create: `src/kdive/providers/local_libvirt/retrieve_kdump.py`
- Test: `tests/providers/local_libvirt/test_retrieve_kdump.py`

**Interfaces:**
- Consumes: `MAX_CORE_BYTES` (Task 1).
- Produces (for Task 4):
  - `class VmcoreEntry(NamedTuple): path: str; mtime: float; size_bytes: int`
  - `class GuestCoreReader(Protocol): def list_vmcores(self, overlay: str) -> list[VmcoreEntry]; def read_vmcore(self, overlay: str, path: str) -> bytes`
  - `select_newest(entries: list[VmcoreEntry]) -> VmcoreEntry | None`
  - `harvest_vmcore(reader: GuestCoreReader, overlay: str, *, max_bytes: int) -> bytes | None`

- [ ] **Step 1: Write the failing tests**

Create `tests/providers/local_libvirt/test_retrieve_kdump.py`:

```python
"""Tests for the local-libvirt kdump host-side overlay harvest (ADR-0203)."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.retrieve_kdump import (
    GuestCoreReader,
    VmcoreEntry,
    harvest_vmcore,
    select_newest,
)

_OVERLAY = "/var/lib/kdive/rootfs/sys-overlay.qcow2"


@dataclass
class _FakeReader:
    entries: list[VmcoreEntry]
    blobs: dict[str, bytes] = field(default_factory=dict)
    reads: list[str] = field(default_factory=list)

    def list_vmcores(self, overlay: str) -> list[VmcoreEntry]:
        return list(self.entries)

    def read_vmcore(self, overlay: str, path: str) -> bytes:
        self.reads.append(path)
        return self.blobs[path]


def test_select_newest_picks_highest_mtime() -> None:
    entries = [
        VmcoreEntry("/var/crash/a/vmcore", 100.0, 10),
        VmcoreEntry("/var/crash/c/vmcore", 300.0, 30),
        VmcoreEntry("/var/crash/b/vmcore", 200.0, 20),
    ]
    assert select_newest(entries) == entries[1]


def test_select_newest_empty_is_none() -> None:
    assert select_newest([]) is None


def test_harvest_reads_newest_core_bytes() -> None:
    reader = _FakeReader(
        entries=[
            VmcoreEntry("/var/crash/old/vmcore", 100.0, 3),
            VmcoreEntry("/var/crash/new/vmcore", 200.0, 5),
        ],
        blobs={"/var/crash/new/vmcore": b"NEWER"},
    )
    out = harvest_vmcore(reader, _OVERLAY, max_bytes=1024)
    assert out == b"NEWER"
    assert reader.reads == ["/var/crash/new/vmcore"]


def test_harvest_absent_core_returns_none() -> None:
    reader = _FakeReader(entries=[])
    assert harvest_vmcore(reader, _OVERLAY, max_bytes=1024) is None


def test_harvest_oversize_core_is_configuration_error() -> None:
    reader = _FakeReader(
        entries=[VmcoreEntry("/var/crash/big/vmcore", 100.0, 4096)],
        blobs={"/var/crash/big/vmcore": b"X"},
    )
    with pytest.raises(CategorizedError) as exc:
        harvest_vmcore(reader, _OVERLAY, max_bytes=1024)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert reader.reads == []  # rejected before reading the bytes


def test_guest_core_reader_protocol_is_runtime_checkable() -> None:
    assert isinstance(_FakeReader(entries=[]), GuestCoreReader)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_retrieve_kdump.py -q`
Expected: FAIL with `ModuleNotFoundError: kdive.providers.local_libvirt.retrieve_kdump`.

- [ ] **Step 3: Write the minimal pure implementation**

Create `src/kdive/providers/local_libvirt/retrieve_kdump.py`:

```python
"""Local-libvirt kdump capture: host-side overlay harvest (ADR-0203).

A local QEMU domain runs on the worker host, so its guest-written
``/var/crash/<ts>/vmcore`` lands on the per-System qcow2 overlay this host owns. The pure
helpers here select the newest core and enforce the single-object ceiling over an injected
``GuestCoreReader``; ``retrieve.py`` supplies the real libguestfs-backed reader behind the
``live_vm`` gate.
"""

from __future__ import annotations

from typing import NamedTuple, Protocol, runtime_checkable

from kdive.domain.errors import CategorizedError, ErrorCategory


class VmcoreEntry(NamedTuple):
    path: str
    mtime: float
    size_bytes: int


@runtime_checkable
class GuestCoreReader(Protocol):
    def list_vmcores(self, overlay: str) -> list[VmcoreEntry]: ...
    def read_vmcore(self, overlay: str, path: str) -> bytes: ...


def select_newest(entries: list[VmcoreEntry]) -> VmcoreEntry | None:
    """The most recently written core (highest mtime), or ``None`` when none exist."""
    if not entries:
        return None
    return max(entries, key=lambda e: e.mtime)


def harvest_vmcore(
    reader: GuestCoreReader, overlay: str, *, max_bytes: int
) -> bytes | None:
    """Read the newest ``/var/crash/*/vmcore`` from ``overlay``; ``None`` if none present.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when the core exceeds ``max_bytes``.
    """
    chosen = select_newest(reader.list_vmcores(overlay))
    if chosen is None:
        return None
    if chosen.size_bytes > max_bytes:
        raise CategorizedError(
            "kdump core exceeds the single-object ceiling",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"size_bytes": chosen.size_bytes, "max_bytes": max_bytes},
        )
    return reader.read_vmcore(overlay, chosen.path)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_retrieve_kdump.py -q`
Expected: PASS (6 tests).

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type
git add src/kdive/providers/local_libvirt/retrieve_kdump.py \
        tests/providers/local_libvirt/test_retrieve_kdump.py
git commit -m "feat(capture): pure kdump overlay-harvest selection + size cap

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Pure dmesg-degrade + redaction + temp-file plumbing helpers

The build-id and dmesg readers take a `Path`, but the harvest yields `bytes`. Add a temp-file
bridge, plus the dmesg degrade-to-sentinel decision (mirroring remote `_dmesg_best_effort`:
re-raise `MISSING_DEPENDENCY`, degrade everything else) and the Redactor application that the
CLAUDE.md invariant requires for the persisted dmesg.

**Files:**
- Modify: `src/kdive/providers/local_libvirt/retrieve_kdump.py`
- Modify: `tests/providers/local_libvirt/test_retrieve_kdump.py`

**Interfaces:**
- Consumes: `DMESG_UNAVAILABLE` (Task 1), `SecretRegistry`, `Redactor`.
- Produces (for Task 4):
  - `read_via_tempfile(data: bytes, path_reader: Callable[[Path], str]) -> str`
  - `extract_dmesg_or_sentinel(data: bytes, extractor: Callable[[Path], bytes]) -> bytes`
  - `redact_dmesg(data: bytes, extractor: Callable[[Path], bytes], registry: SecretRegistry) -> bytes`

- [ ] **Step 1: Write the failing tests**

Append to `tests/providers/local_libvirt/test_retrieve_kdump.py`:

```python
from pathlib import Path

from kdive.providers.local_libvirt.retrieve_kdump import (
    extract_dmesg_or_sentinel,
    read_via_tempfile,
    redact_dmesg,
)
from kdive.providers.shared.debug_common.core_file import DMESG_UNAVAILABLE
from kdive.security.secrets.secret_registry import SecretRegistry


def test_read_via_tempfile_passes_a_path_holding_the_bytes() -> None:
    seen: dict[str, bytes] = {}

    def reader(path: Path) -> str:
        seen["bytes"] = path.read_bytes()
        return "deadbeef"

    assert read_via_tempfile(b"COREBYTES", reader) == "deadbeef"
    assert seen["bytes"] == b"COREBYTES"


def test_read_via_tempfile_removes_the_temp_file() -> None:
    captured: list[Path] = []

    def reader(path: Path) -> str:
        captured.append(path)
        return "x"

    read_via_tempfile(b"X", reader)
    assert not captured[0].exists()


def test_extract_dmesg_success_returns_bytes() -> None:
    assert extract_dmesg_or_sentinel(b"core", lambda _p: b"kernel log") == b"kernel log"


def test_extract_dmesg_degrades_infrastructure_failure_to_sentinel() -> None:
    def boom(_p: Path) -> bytes:
        raise CategorizedError("no debuginfo", category=ErrorCategory.INFRASTRUCTURE_FAILURE)

    assert extract_dmesg_or_sentinel(b"core", boom) == DMESG_UNAVAILABLE


def test_extract_dmesg_reraises_missing_dependency() -> None:
    def no_drgn(_p: Path) -> bytes:
        raise CategorizedError("drgn missing", category=ErrorCategory.MISSING_DEPENDENCY)

    with pytest.raises(CategorizedError) as exc:
        extract_dmesg_or_sentinel(b"core", no_drgn)
    assert exc.value.category is ErrorCategory.MISSING_DEPENDENCY


def test_redact_dmesg_scrubs_a_registered_secret() -> None:
    registry = SecretRegistry()
    registry.register("hunter2")
    out = redact_dmesg(b"core", lambda _p: b"login password=hunter2 done", registry)
    assert b"hunter2" not in out
    assert b"password=" in out
```

Note: confirm the `SecretRegistry.register(...)` API against `security/secrets/secret_registry.py` before running; if registration takes a different shape (e.g. a named ref), mirror an existing test that registers a literal secret.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_retrieve_kdump.py -q`
Expected: FAIL with `ImportError` on `read_via_tempfile` / `extract_dmesg_or_sentinel` / `redact_dmesg`.

- [ ] **Step 3: Write the minimal implementation**

Append to `src/kdive/providers/local_libvirt/retrieve_kdump.py` (add imports at top):

```python
import tempfile
from collections.abc import Callable
from pathlib import Path

from kdive.providers.shared.debug_common.core_file import DMESG_UNAVAILABLE
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry
```

```python
def read_via_tempfile(data: bytes, path_reader: Callable[[Path], str]) -> str:
    """Spool ``data`` to a temp file so a Path-based drgn reader can open it; clean up after."""
    with tempfile.NamedTemporaryFile(prefix="kdive-kdump-", suffix=".vmcore") as handle:
        handle.write(data)
        handle.flush()
        return path_reader(Path(handle.name))


def extract_dmesg_or_sentinel(
    data: bytes, extractor: Callable[[Path], bytes]
) -> bytes:
    """Extract dmesg from the core bytes; degrade to the sentinel, but never hide a missing drgn.

    Mirrors remote host_dump: a `MISSING_DEPENDENCY` (drgn absent) is an operator fault that
    must surface; any other failure (printk needs debuginfo) degrades to `DMESG_UNAVAILABLE`
    so the core + build-id still get captured.
    """
    try:
        return read_via_tempfile_bytes(data, extractor)
    except CategorizedError as exc:
        if exc.category is ErrorCategory.MISSING_DEPENDENCY:
            raise
        return DMESG_UNAVAILABLE


def read_via_tempfile_bytes(data: bytes, path_reader: Callable[[Path], bytes]) -> bytes:
    with tempfile.NamedTemporaryFile(prefix="kdive-kdump-", suffix=".vmcore") as handle:
        handle.write(data)
        handle.flush()
        return path_reader(Path(handle.name))


def redact_dmesg(
    data: bytes, extractor: Callable[[Path], bytes], registry: SecretRegistry
) -> bytes:
    """Extract dmesg (degrading on failure) and scrub registered secrets before persistence."""
    dmesg = extract_dmesg_or_sentinel(data, extractor)
    redacted = Redactor(registry=registry).redact_text(dmesg.decode("utf-8", "replace"))
    return redacted.encode("utf-8")
```

(If a `read_via_tempfile` for `str` and a `read_via_tempfile_bytes` for `bytes` feel
redundant, collapse to one generic helper typed `Callable[[Path], T]` with a `TypeVar`; keep
whichever the type checker accepts cleanly under `ty`.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_retrieve_kdump.py -q`
Expected: PASS.

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type
git add src/kdive/providers/local_libvirt/retrieve_kdump.py \
        tests/providers/local_libvirt/test_retrieve_kdump.py
git commit -m "feat(capture): dmesg degrade + redaction + temp-file bridge

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Wire the real seams, advertise KDUMP, flip ADR-0203 to Accepted

Replace the placeholder `_real_wait_for_vmcore` / `_real_extract_redacted` with the real
libguestfs/drgn seams, wire a real bytes→build-id reader, and add `KDUMP` to local-libvirt's
`supported_capture_methods`. The libguestfs / `domain.destroy()` / drgn calls are the only
`live_vm`-gated code; everything testable is exercised through the Task 2-3 helpers and the
composition test.

**Files:**
- Modify: `src/kdive/providers/local_libvirt/retrieve.py` (seams + `from_env` wiring)
- Modify: `src/kdive/providers/local_libvirt/composition.py:114-116` (advertise KDUMP)
- Modify: `tests/providers/local_libvirt/test_composition.py` (assert KDUMP advertised)
- Modify: `docs/adr/0203-local-libvirt-kdump-overlay-harvest.md` (Status → Accepted)
- Modify: `docs/adr/README.md` (0203 row status → Accepted)
- Check: `tests/mcp/lifecycle/test_vmcore_tools.py` — `test_fetch_rejects_unsupported_method` (uses a fake runtime, not real local composition; confirm it does not assert local rejects kdump. If it constructs from real local composition, update it.)

**Interfaces:**
- Consumes: `harvest_vmcore`, `redact_dmesg`, `read_via_tempfile`, `GuestCoreReader`, `VmcoreEntry` (Tasks 2-3); `MAX_CORE_BYTES`, `read_core_build_id_from_file`, `read_core_dmesg_from_file` (Task 1); `overlay_path` (`local_libvirt/lifecycle/storage.py`); `domain_name_for` (`providers/shared/runtime_paths.py`).

- [ ] **Step 1: Write the failing composition test**

Add to `tests/providers/local_libvirt/test_composition.py` (match the file's existing build
pattern; if a `supported_capture_methods` assertion already exists, extend it):

```python
def test_local_runtime_advertises_kdump_capture() -> None:
    from kdive.domain.capture import CaptureMethod
    from kdive.providers.local_libvirt.composition import build_runtime
    from kdive.security.secrets.secret_registry import SecretRegistry

    runtime = build_runtime(secret_registry=SecretRegistry())
    assert CaptureMethod.KDUMP in runtime.supported_capture_methods
    assert CaptureMethod.HOST_DUMP in runtime.supported_capture_methods  # unchanged
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_composition.py::test_local_runtime_advertises_kdump_capture -q`
Expected: FAIL (`KDUMP not in supported_capture_methods`).

- [ ] **Step 3: Advertise KDUMP in composition**

In `src/kdive/providers/local_libvirt/composition.py`, change lines 114-116 to:

```python
        supported_capture_methods=frozenset(
            {
                CaptureMethod.CONSOLE,
                CaptureMethod.HOST_DUMP,
                CaptureMethod.GDBSTUB,
                CaptureMethod.KDUMP,
            }
        ),
```

- [ ] **Step 4: Implement the real seams + `from_env` wiring in `retrieve.py`**

In `src/kdive/providers/local_libvirt/retrieve.py`:

(a) Add imports:

```python
import logging

import libvirt  # ty: ignore[unresolved-import]  # operator-provided C extension

from kdive.providers.local_libvirt.lifecycle.storage import overlay_path
from kdive.providers.local_libvirt.retrieve_kdump import (
    GuestCoreReader,
    VmcoreEntry,
    harvest_vmcore,
    read_via_tempfile,
    redact_dmesg,
)
from kdive.providers.shared.debug_common.core_file import (
    MAX_CORE_BYTES,
    read_core_build_id_from_file,
    read_core_dmesg_from_file,
)
from kdive.providers.shared.runtime_paths import domain_name_for
```

(b) Replace the placeholder bodies (lines 186-206) with the real seams. The build-id reader
must replace the wired `default_read_vmcore_build_id` placeholder, and redaction must use the
`secret_registry` already passed to `from_env`:

```python
_log = logging.getLogger(__name__)

_LIBGUESTFS_ABSENT = "libguestfs (the guestfs Python binding) is required for local kdump capture"


class _LibguestfsCoreReader:  # pragma: no cover - live_vm (libguestfs)
    """Read-only libguestfs view of a System's overlay, listing/reading /var/crash cores."""

    def list_vmcores(self, overlay: str) -> list[VmcoreEntry]:
        g = self._mount(overlay)
        try:
            entries: list[VmcoreEntry] = []
            for path in g.glob_expand("/var/crash/*/vmcore"):
                st = g.statns(path)
                entries.append(VmcoreEntry(path=path, mtime=st["st_mtime_sec"], size_bytes=st["st_size"]))
            return entries
        finally:
            g.close()

    def read_vmcore(self, overlay: str, path: str) -> bytes:
        g = self._mount(overlay)
        try:
            return g.read_file(path)
        finally:
            g.close()

    @staticmethod
    def _mount(overlay: str):
        try:
            import guestfs  # noqa: PLC0415  # ty: ignore[unresolved-import]
        except ImportError as exc:
            raise CategorizedError(
                _LIBGUESTFS_ABSENT, category=ErrorCategory.MISSING_DEPENDENCY
            ) from exc
        g = guestfs.GuestFS(python_return_dict=True)
        g.add_drive_opts(overlay, readonly=1)
        g.launch()
        roots = g.inspect_os()
        if not roots:
            g.close()
            raise CategorizedError(
                "could not inspect the System overlay to find /var/crash",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            )
        g.mount_ro(roots[0], "/")
        return g


def _force_off_domain(system_id: UUID) -> None:  # pragma: no cover - live_vm (libvirt)
    """Force the System's domain off (idempotent) so its overlay is safe to read offline."""
    conn = libvirt.open(None)
    try:
        try:
            domain = conn.lookupByName(domain_name_for(system_id))
        except libvirt.libvirtError:
            return  # already gone — nothing running to quiesce
        if domain.isActive():
            domain.destroy()
    finally:
        conn.close()


def _real_wait_for_vmcore(system_id: UUID) -> bytes | None:  # pragma: no cover - live_vm
    _force_off_domain(system_id)
    return harvest_vmcore(_LibguestfsCoreReader(), overlay_path(system_id), max_bytes=MAX_CORE_BYTES)


def _real_read_build_id(data: bytes) -> str:  # pragma: no cover - live_vm (drgn)
    return read_via_tempfile(data, read_core_build_id_from_file)
```

Delete `_real_extract_redacted` (lines 202-206) — redaction now needs the registry, so it is
built in `from_env` as a closure rather than a bare module function. Update `from_env`
(lines 83-96) to wire the real seams:

```python
    @classmethod
    def from_env(cls, *, secret_registry: SecretRegistry) -> LocalLibvirtRetrieve:
        """Build from env; does not poll the host, open S3, or spawn `crash` (lazy seams)."""
        return cls(
            tenant="local",
            store_factory=object_store_from_env,
            wait_for_vmcore=_real_wait_for_vmcore,
            read_vmcore_build_id=_real_read_build_id,
            extract_redacted=lambda data: redact_dmesg(
                data, read_core_dmesg_from_file, secret_registry
            ),
            host_dump_capture=_real_host_dump_capture,
            fetch_object=default_fetch_object,
            run_crash=default_run_crash,
            secret_registry=secret_registry,
        )
```

Remove the now-unused `default_read_vmcore_build_id` import if `from_env` was its only user
(check; it may still be re-exported elsewhere — only drop the local import).

- [ ] **Step 5: Run the focused tests + the existing retrieve orchestration tests**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_composition.py tests/providers/local_libvirt/test_retrieve.py tests/providers/local_libvirt/test_retrieve_kdump.py -q`
Expected: PASS. The existing `test_retrieve.py` KDUMP orchestration tests still pass (they
inject fakes, so the real seams are not touched). Confirm `from_env(secret_registry=...)`
imports and builds without raising (a smoke assertion already covered by composition).

- [ ] **Step 6: Confirm the vmcore-tools admission test still holds**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_vmcore_tools.py -q`
Expected: PASS. If `test_fetch_rejects_unsupported_method` constructs its runtime from real
local composition and now fails, switch it to a method genuinely unsupported by local (none
remain among the four — in that case rewrite it to assert a synthetic unsupported method via
a fake runtime whose `supported_capture_methods` omits the requested one, preserving the
"method not supported by provider" branch coverage). Do not delete the branch's coverage.

- [ ] **Step 7: Flip ADR-0203 to Accepted (now cited in `src/`)**

In `docs/adr/0203-local-libvirt-kdump-overlay-harvest.md` change `- **Status:** Proposed` to
`- **Status:** Accepted`. In `docs/adr/README.md` change the trailing `| Proposed |` of the
0203 row to `| Accepted |`. Add the ADR citation to the new modules' docstrings if not
already present (`retrieve_kdump.py` and the `retrieve.py` seam section reference ADR-0203).

Run: `python3 scripts/check_adr_status.py`
Expected: `index in sync, no shipped-but-Proposed drift`.

- [ ] **Step 8: Guardrails + commit**

```bash
just lint && just type
git add src/kdive/providers/local_libvirt/retrieve.py \
        src/kdive/providers/local_libvirt/composition.py \
        tests/providers/local_libvirt/test_composition.py \
        tests/mcp/lifecycle/test_vmcore_tools.py \
        docs/adr/0203-local-libvirt-kdump-overlay-harvest.md \
        docs/adr/README.md
git commit -m "feat(capture): local-libvirt kdump overlay harvest seam

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Document the Tier 3 capture path (maturity meta + runbook)

Surface the new capability where operators and agents will look: the `vmcore.fetch` maturity
`providers=` line and the local-libvirt runbook. Live panic→capture verification is
operator-run (hardware), so the runbook carries the procedure rather than a placeholder
`live_vm` test.

**Files:**
- Modify: `src/kdive/mcp/tools/lifecycle/vmcore.py:342-345` (`providers=` line)
- Modify: the local-libvirt runbook under `docs/operating/runbooks/` (find the local-libvirt one; if none, add a Tier 3 section to the closest local-libvirt operating doc)
- Check: `tests/mcp/lifecycle/test_vmcore_tools.py` for any assertion pinning the `providers=` text; update it if present.

**Interfaces:** none (docs + a one-line meta string).

- [ ] **Step 1: Locate the runbook and any test pinning the providers line**

Run: `ls docs/operating/runbooks/ && rg -n "local-libvirt: HOST_DUMP" src tests`
Expected: the runbook path + the `providers=` string site(s).

- [ ] **Step 2: Update the maturity `providers=` line**

In `src/kdive/mcp/tools/lifecycle/vmcore.py`, change the `providers=` text (lines ~342-345) to
reflect that local-libvirt now does KDUMP:

```python
            providers=(
                "local-libvirt: HOST_DUMP/KDUMP; remote-libvirt: HOST_DUMP/KDUMP; "
                "fault-inject: simulated HOST_DUMP."
            ),
```

- [ ] **Step 3: Add the runbook Tier 3 section**

Add a "Tier 3 (kdump) capture — local-libvirt" subsection to the runbook covering: the host
prerequisite (`libguestfs` / `guestfs` Python binding, alongside drgn), setting the profile
`crashkernel` so the System boots kdump-capable, and the operator sequence
`control.force_crash` (or a deliberate guest panic) → wait for `CRASHED` →
`vmcore.fetch(method=kdump)` → `postmortem.crash`. Note the consequence that the harvest
force-stops the domain (ADR-0203).

- [ ] **Step 4: Update any test pinning the providers text; run doc guards**

If Step 1 found a test asserting the old `providers=` string, update it to the new value.

Run: `uv run python -m pytest tests/mcp/lifecycle/test_vmcore_tools.py -q && bash scripts/check-doc-links.sh && bash scripts/check-doc-paths.sh`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/mcp/tools/lifecycle/vmcore.py docs/operating/runbooks/ tests/mcp/lifecycle/test_vmcore_tools.py
git commit -m "docs(capture): document local-libvirt Tier 3 kdump path

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Full-suite verification

**Files:** none (verification only).

- [ ] **Step 1: Run the full CI gate locally**

Run: `just ci`
Expected: every gate green — lint, type, lint-shell, lint-ansible, test-ansible, lint-workflows, check-mermaid, docs-links, docs-paths, adr-status-check, docs-check, config-docs-check, config-guard, env-docs-check, resources-docs-check, chart-version-check, test. The `live_vm`/`live_stack` suites stay skipped (no host); that is expected.

- [ ] **Step 2: If any gate fails, fix in its owning task's files and re-run before pushing.**

## Self-Review

- **Spec coverage:** Task 4 advertises KDUMP + implements the harvest seam (spec §Approach 1-3, 6); Task 4 wires the real build-id reader (spec §Approach 4); Task 3 + Task 4 wire dmesg degrade + redaction (spec §Approach 5); Task 2 covers selection/size-cap/None contract (spec §Testability, §Acceptance CI-verifiable); Task 1 shares the drgn helpers (ADR-0203 consequence); Task 5 covers the maturity-meta + runbook documentation (spec §Documentation, §Acceptance live-hardware). Precondition (spec §Precondition) is encoded in `_force_off_domain` idempotency (Task 4) and the ADR.
- **Placeholder scan:** no "TBD"/"handle edge cases"; every code step shows full code. The two intentionally-deferred items (live verification, the optional generic-tempfile collapse) are explicit operator/implementer choices, not plan gaps.
- **Type consistency:** `harvest_vmcore`, `select_newest`, `VmcoreEntry`, `GuestCoreReader`, `read_via_tempfile`, `extract_dmesg_or_sentinel`, `redact_dmesg` names are used identically in Tasks 2-4. `_real_wait_for_vmcore`/`_real_read_build_id` match the `_WaitForVmcore`/`_ReadBuildId` seam types in `retrieve.py`.
