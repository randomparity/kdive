# Implementation plan — module-staging toolchain diagnostics vantage (#2339)

- **Spec:** [design](../specs/2026-09-08-depmod-toolchain-vantage-design.md)
- **ADR:** [ADR-0635](../../adr/0635-module-staging-toolchain-vantage.md)
- **Branch:** `feat/depmod-toolchain-vantage-2339` off `main`

**Goal.** Report at `ops.diagnostics` time whether the worker host's module-staging binary
(`depmod`) resolves under the four explicit directories the install path searches, naming those
directories in the verdict.

**Architecture.** The binary name and search directories move from a private constant in the
local-libvirt overlay writer into `src/kdive/providers/shared/module_staging_tools.py`. The install
resolver imports them; a new worker-vantage check imports the same names and reports whether
`shutil.which` finds the binary there. The check joins the one existing `local-libvirt`
contribution, whose ids are dispatched to the worker as a single job and reconstructed through the
inline result codec.

**Tech stack.** Python 3.14, `uv`, pytest, ruff, `ty`. No new dependency.

Expected implementation size: 220–300 changed lines (M) — from the file map below: ~26 lines of new
shared module, ~50 of new check class, ~55 of new contribution, ~15 of edits across five existing
source files, and ~110 of tests.

## Global Constraints

Transcribed from the spec and `AGENTS.md`:

- Python 3.14. Ruff line length **100**, lint set `E,F,I,UP,B,SIM`. `ty` runs whole-tree
  (`src` + `tests`) with strict defaults.
- `ErrorCategory` is a closed taxonomy in `src/kdive/domain/errors.py` — pick the most specific
  existing value, never invent a string. This change uses `ErrorCategory.MISSING_DEPENDENCY` only.
- Check ids are lowercase snake_case module constants in `src/kdive/diagnostics/checks.py`,
  matching `multiarch_gdb`, `pseries_fadump`, `guest_arch_accel`; this change adds exactly one,
  `depmod_toolchain`. `result_codec._ALLOWED_IDS` gains exactly **one** additive entry and is not
  restructured — #2344 owns that file's structure.
- `CheckResult` invariants (`checks.py::CheckResult.__post_init__`): a `fail` **must** carry a
  `fix`; any non-`fail` status **must not**; a `pass` **must not** carry a `failure_category`.
- The four directories are `("/usr/sbin", "/usr/bin", "/sbin", "/bin")`, in that order;
  `/usr/local/{sbin,bin}` stay excluded. Value and explanatory comment move verbatim.
- Doc-style guard: use **Milestone**, never "Sprint"; avoid "critical", "robust",
  "comprehensive", "elegant" in prose, ADRs, commit messages, and code comments. Commits follow
  Conventional Commits 1.0.0, imperative, subject ≤72 characters, one logical change.
- Guardrails: `just format` before committing a Python-only change; `just lint`, `just type`, and
  focused `pytest` while iterating; `just ci > <file> 2>&1 < /dev/null` as the pre-push gate, run
  **bare** — no pipe, no `>/dev/null`, no `|| true`, no trailing `; echo $?`.

## File map

| Path | Created / changed | Answerable for |
|---|---|---|
| `src/kdive/providers/shared/module_staging_tools.py` | created | The module-staging host-tool contract: binary name, search directories, joined search path |
| `src/kdive/providers/local_libvirt/lifecycle/boot/guest_kernel_writer.py` | changed | Imports that contract instead of holding its own copy; behaviour unchanged |
| `src/kdive/diagnostics/checks.py` | changed | The stable `depmod_toolchain` check id |
| `src/kdive/diagnostics/provider_checks.py` | changed | `DepmodToolchainCheck` and its `fix` string, beside the existing check classes |
| `src/kdive/diagnostics/contributions/depmod_toolchain.py` | created | The default probe and the check/descriptor factories |
| `src/kdive/diagnostics/contributions/multiarch_gdb.py` | changed | Adds check and descriptor to the one `local-libvirt` contribution |
| `src/kdive/diagnostics/result_codec.py` | changed | One additive allowlist entry |
| `tests/diagnostics/test_depmod_toolchain.py` | created | The check's verdicts and the default probe's search path |
| `tests/diagnostics/test_result_codec.py` | changed | Round trip for the new id; the descriptor/allowlist agreement test |
| `tests/diagnostics/test_default_factory.py` | changed | The substituted worker-check id sets |
| `tests/diagnostics/test_service.py` | changed | The assembled local-libvirt dispatcher's id set |

## Task 1 — Share the module-staging tool contract

**Creates:** `src/kdive/providers/shared/module_staging_tools.py`
**Modifies:** `src/kdive/providers/local_libvirt/lifecycle/boot/guest_kernel_writer.py`

**Interfaces — provided to Task 2:**

```python
DEPMOD: str                                    # "depmod"
DEPMOD_SEARCH_DIRS: tuple[str, str, str, str]  # ("/usr/sbin", "/usr/bin", "/sbin", "/bin")
DEPMOD_SEARCH_PATH: str                        # "/usr/sbin:/usr/bin:/sbin:/bin"
```

**Consumes:** nothing.

### Verification

- **`_resolve_depmod` still resolves against the same four directories and raises the same
  `MISSING_DEPENDENCY` error naming them.** Mode: focused-test — the existing
  `tests/providers/local_libvirt/lifecycle/boot/test_module_indexing.py::test_run_host_depmod_unresolvable_names_searched_directories`
  asserts `exc.value.details["searched"] == "/usr/sbin:/usr/bin:/sbin:/bin"` and that the same
  string is in the message. Green before this task and must stay green; a reordered or altered
  tuple turns it red. Command:
  `uv run python -m pytest tests/providers/local_libvirt/lifecycle/boot/test_module_indexing.py -q`.
- **The two modules share one search-path object rather than copies.** Mode:
  task-test-not-applicable — after this task `guest_kernel_writer` holds an `import`, not a
  literal, so a divergent second value is not expressible in source and no assertion can fail on
  it.

### Steps

1. Create `src/kdive/providers/shared/module_staging_tools.py` with exactly this content:

```python
"""The module-staging host-tool contract shared by the install path and diagnostics (ADR-0635).

Module indexing runs the host's ``depmod`` against an extracted module tree (ADR-0346 §2). This
module owns *where* that binary is looked for, so the ``ops.diagnostics`` vantage reporting on the
requirement and the install path depending on it cannot name different directories.
"""

from __future__ import annotations

import os

DEPMOD = "depmod"
# <the nine comment lines currently at guest_kernel_writer.py lines 50-58, copied verbatim:
#  "depmod is an sbin tool — /usr/sbin under merged-usr …" through "… authority over guest
#  overlays." Copy them, do not paraphrase: they record why PATH is excluded and why
#  /usr/local/{sbin,bin} are.>
DEPMOD_SEARCH_DIRS = ("/usr/sbin", "/usr/bin", "/sbin", "/bin")
DEPMOD_SEARCH_PATH = os.pathsep.join(DEPMOD_SEARCH_DIRS)
```

2. In `guest_kernel_writer.py`, delete the module-level `_DEPMOD = "depmod"` line, the nine comment
   lines beneath it (now copied into step 1's module), and the `_DEPMOD_SEARCH_DIRS = (...)` line.
   Leave `_DEPMOD_STDERR_MAX = 500` above and `_DEPMOD_EXEC_ERRNOS` below untouched.
3. In the same file, delete `import os` — the only `os.` use is the `os.pathsep.join` replaced in
   step 4, which the shared module now performs. Keep `import shutil` (`shutil.which` is still
   called). Add to the first-party imports:

```python
from kdive.providers.shared.module_staging_tools import DEPMOD, DEPMOD_SEARCH_PATH
```

4. In `_resolve_depmod`, replace its first two statements

```python
    searched = os.pathsep.join(_DEPMOD_SEARCH_DIRS)
    resolved = shutil.which(_DEPMOD, path=searched)
```

   with

```python
    searched = DEPMOD_SEARCH_PATH
    resolved = shutil.which(DEPMOD, path=searched)
```

   Leave the `CategorizedError` block, its message, category, and `details={"searched": searched}`
   exactly as they are.

5. Run `just format`. Expected: exit 0, no `F401`.
6. Run
   `uv run python -m pytest tests/providers/local_libvirt/lifecycle/boot/test_module_indexing.py -q`.
   Expected: all pass, no failures.
7. Run `just lint` and `just type`. Expected: exit 0 from each, no diagnostics.
8. Commit: `refactor(providers): share the module-staging depmod search contract`.

**Acceptance.** `module_staging_tools.py` holds the only copy of the binary name and the four
directories; `guest_kernel_writer` imports them; `test_module_indexing.py` is green unmodified;
lint and type are clean. **Rollback:** `git revert` the single commit; nothing depends on the new
module yet.

## Task 2 — The `depmod_toolchain` check, wired and codec-registered

**Creates:** `src/kdive/diagnostics/contributions/depmod_toolchain.py`,
`tests/diagnostics/test_depmod_toolchain.py`
**Modifies:** `src/kdive/diagnostics/checks.py`, `src/kdive/diagnostics/provider_checks.py`,
`src/kdive/diagnostics/contributions/multiarch_gdb.py`, `src/kdive/diagnostics/result_codec.py`,
`tests/diagnostics/test_result_codec.py`, `tests/diagnostics/test_default_factory.py`,
`tests/diagnostics/test_service.py`

This is one task because no part of it is separately reviewable: a check class nothing assembles,
or an assembled id the codec rejects, is not a shippable state.

**Interfaces — consumed from Task 1:**

```python
from kdive.providers.shared.module_staging_tools import DEPMOD, DEPMOD_SEARCH_DIRS, DEPMOD_SEARCH_PATH
```

**Interfaces — defined here:** `DEPMOD_TOOLCHAIN_ID`, `DepmodToolchainProbe`,
`DepmodToolchainCheck`, `ToolResolver`, `default_depmod_toolchain_probe`,
`depmod_toolchain_worker_check`, `depmod_toolchain_worker_descriptor` — full signatures in the
code of steps 5–8 below. Nothing later in this plan consumes them.

`shutil.which(cmd, mode=os.F_OK | os.X_OK, path=None)` takes `mode` as its second **positional**
parameter, so the search path is always passed by keyword — hence the `Protocol` rather than a
plain two-argument `Callable`.

### Verification

All four cases below live in `tests/diagnostics/test_depmod_toolchain.py`, are red before step 5
with `ImportError: cannot import name 'DEPMOD_TOOLCHAIN_ID' from 'kdive.diagnostics.checks'`, and
go green with `uv run python -m pytest tests/diagnostics/test_depmod_toolchain.py -q`:

- **A resolved `depmod` gives a `pass` naming the path, no `fix`, no `failure_category`.** Mode:
  focused-test — `::test_resolved_depmod_passes`.
- **An unresolved `depmod` gives a `fail` whose `detail` names the binary and all four directories,
  whose `fix` names `kmod`, and whose `failure_category` is `MISSING_DEPENDENCY`.** Mode:
  focused-test — `::test_missing_depmod_fails_naming_the_searched_dirs`.
- **Check id and vantage.** Mode: focused-test — `::test_check_id_and_vantage`.
- **The default probe searches the shared list, never `PATH`.** Mode: focused-test —
  `::test_default_probe_searches_the_shared_dirs`, injecting a recording resolver and asserting it
  received `DEPMOD` and `DEPMOD_SEARCH_PATH`.

The rest extend existing tests:

- **The assembled `local-libvirt` contribution dispatches the new id through exactly one
  dispatcher.** Mode: focused-test — `tests/diagnostics/test_service.py`'s
  `set(by_provider["local-libvirt"]._worker_check_ids) == {...}` assertion extended with
  `DEPMOD_TOOLCHAIN_ID`; it already asserts `set(by_provider) == {"local-libvirt",
  "remote-libvirt"}`, so a second contribution would fail it. Red before step 9 (the set comparison
  reports the missing id). Green: `uv run python -m pytest tests/diagnostics/test_service.py -q`.
- **The id joins the substituted set when no dispatch is wired.** Mode: focused-test —
  `tests/diagnostics/test_default_factory.py`'s two exact `unavailable_ids` assertions extended the
  same way. Red before step 9. Green:
  `uv run python -m pytest tests/diagnostics/test_default_factory.py -q`.
- **`_ALLOWED_IDS` tracks every registered descriptor, and a result survives the inline round
  trip.** Mode: focused-test — the existing
  `tests/diagnostics/test_result_codec.py::test_allowed_ids_matches_registered_worker_vantage_descriptors`
  (red once the descriptor is registered without the allowlist entry, reporting the set mismatch)
  and the new `::test_depmod_toolchain_id_survives_roundtrip` (red before step 10: the deserialized
  item is `CheckStatus.ERROR` with detail `unexpected worker-vantage check id 'depmod_toolchain'`).
  Green: `uv run python -m pytest tests/diagnostics/test_result_codec.py -q`.
- **The guardrail suite passes on the assembled change.** Mode: focused-test — `just ci` run bare
  with output captured to a file; green is exit 0 and no failures in the captured log.

### Steps

1. Write the failing tests first. Create `tests/diagnostics/test_depmod_toolchain.py`:

```python
"""Tests for the module-staging toolchain worker-vantage check (ADR-0635, #2339)."""

from __future__ import annotations

import pytest

from kdive.diagnostics.checks import DEPMOD_TOOLCHAIN_ID, CheckStatus, Vantage
from kdive.diagnostics.contributions.depmod_toolchain import default_depmod_toolchain_probe
from kdive.diagnostics.provider_checks import DepmodToolchainCheck
from kdive.domain.errors import ErrorCategory
from kdive.providers.shared.module_staging_tools import (
    DEPMOD,
    DEPMOD_SEARCH_DIRS,
    DEPMOD_SEARCH_PATH,
)

_PROVIDER = "local-libvirt"


def _check(resolved: str | None) -> DepmodToolchainCheck:
    async def _probe() -> str | None:
        return resolved

    return DepmodToolchainCheck(provider=_PROVIDER, probe=_probe)


def test_check_id_and_vantage() -> None:
    check = _check("/usr/sbin/depmod")
    assert check.id == DEPMOD_TOOLCHAIN_ID == "depmod_toolchain"
    assert check.vantage is Vantage.WORKER


@pytest.mark.asyncio
async def test_resolved_depmod_passes() -> None:
    result = await _check("/usr/sbin/depmod").run()
    assert result.status is CheckStatus.PASS
    assert result.check_id == DEPMOD_TOOLCHAIN_ID
    assert result.provider == _PROVIDER
    assert "/usr/sbin/depmod" in result.detail
    assert result.fix is None
    assert result.failure_category is None


@pytest.mark.asyncio
async def test_missing_depmod_fails_naming_the_searched_dirs() -> None:
    result = await _check(None).run()
    assert result.status is CheckStatus.FAIL
    assert result.failure_category is ErrorCategory.MISSING_DEPENDENCY
    assert "depmod" in result.detail
    for directory in DEPMOD_SEARCH_DIRS:
        assert directory in result.detail
    assert result.fix is not None
    assert "kmod" in result.fix


@pytest.mark.asyncio
async def test_default_probe_searches_the_shared_dirs() -> None:
    seen: list[tuple[str, str]] = []

    def _which(cmd: str, *, path: str) -> str | None:
        seen.append((cmd, path))
        return "/sbin/depmod"

    probe = default_depmod_toolchain_probe(which=_which)
    assert await probe() == "/sbin/depmod"
    assert seen == [(DEPMOD, DEPMOD_SEARCH_PATH)]
```

2. Add the failing round-trip test to `tests/diagnostics/test_result_codec.py`, beside
   `test_guest_arch_accel_id_survives_roundtrip`, adding `DEPMOD_TOOLCHAIN_ID` to that file's
   `from kdive.diagnostics.checks import (...)` block:

```python
def test_depmod_toolchain_id_survives_roundtrip() -> None:
    src = [
        CheckResult(
            DEPMOD_TOOLCHAIN_ID,
            CheckStatus.FAIL,
            "depmod was not found in any of /usr/sbin:/usr/bin:/sbin:/bin",
            fix="install kmod",
            provider="local-libvirt",
            failure_category=ErrorCategory.MISSING_DEPENDENCY,
        )
    ]
    [result] = deserialize_results(serialize_results(src))
    assert result.check_id == DEPMOD_TOOLCHAIN_ID
    assert result.status is CheckStatus.FAIL
    assert result.failure_category is ErrorCategory.MISSING_DEPENDENCY
    assert result.fix == "install kmod"
```

3. In the same file, add `depmod_toolchain_worker_descriptor().id,` to the `expected_ids` set in
   `test_allowed_ids_matches_registered_worker_vantage_descriptors`, with
   `from kdive.diagnostics.contributions.depmod_toolchain import depmod_toolchain_worker_descriptor`
   added to the imports.
4. Run `uv run python -m pytest tests/diagnostics -q`. Expected: collection errors for
   `test_depmod_toolchain.py` and `test_result_codec.py`
   (`ImportError: cannot import name 'DEPMOD_TOOLCHAIN_ID' from 'kdive.diagnostics.checks'`).
5. In `src/kdive/diagnostics/checks.py`, add one line after
   `GUEST_ARCH_ACCEL_ID = "guest_arch_accel"`:

```python
DEPMOD_TOOLCHAIN_ID = "depmod_toolchain"
```

6. In `src/kdive/diagnostics/provider_checks.py`, add `DEPMOD_TOOLCHAIN_ID` to the existing
   `from kdive.diagnostics.checks import (...)` block and
   `from kdive.providers.shared.module_staging_tools import DEPMOD_SEARCH_PATH` to the imports
   (`just format` settles ordering). Add the `fix` string beside `MULTIARCH_GDB_MISSING_FIX`:

```python
DEPMOD_TOOLCHAIN_MISSING_FIX = (
    "install kmod (which provides depmod) on this worker host, or confirm depmod is in one of "
    f"the searched directories. Resolution is deliberately not PATH-based ({DEPMOD_SEARCH_PATH}), "
    "so a depmod elsewhere on the host will not be found (ADR-0635)"
)
```

7. In the same file, append after `GuestArchAccelCheck`:

```python
DepmodToolchainProbe = Callable[[], Awaitable[str | None]]


class DepmodToolchainCheck(Check):
    """Worker-vantage: the module-staging host toolchain resolves on this worker (ADR-0635).

    A host laying ``depmod`` outside the four explicit directories (#2300) fails mid-install; this
    reports the same requirement at diagnostics time, naming the directories searched because
    "install kmod" is not actionable for an operator who already has it.
    """

    def __init__(self, *, provider: str, probe: DepmodToolchainProbe) -> None:
        self._provider = provider
        self._probe = probe

    @property
    def id(self) -> str:
        return DEPMOD_TOOLCHAIN_ID

    @property
    def vantage(self) -> Vantage:
        return Vantage.WORKER

    async def run(self) -> CheckResult:
        resolved = await self._probe()
        if resolved is not None:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.PASS,
                detail=f"module staging can index kernel modules with depmod at {resolved}",
                provider=self._provider,
            )
        return CheckResult(
            check_id=self.id,
            status=CheckStatus.FAIL,
            detail=(
                "depmod is required on this worker host to index kernel modules for staging, "
                f"and was not found in any of {DEPMOD_SEARCH_PATH}"
            ),
            fix=DEPMOD_TOOLCHAIN_MISSING_FIX,
            provider=self._provider,
            failure_category=_MISSING_DEPENDENCY,
        )
```

8. Create `src/kdive/diagnostics/contributions/depmod_toolchain.py`:

```python
"""The module-staging toolchain worker-vantage diagnostic contribution (ADR-0635, #2339).

Without this check, a ``depmod`` outside the four explicit directories (#2300) is first reported
mid-install, after a System has been allocated and a guest booted. The probe reads the same
contract the install path resolves against — from ``kdive.providers.shared``, the way
``pseries_fadump`` and ``multiarch_gdb`` read theirs — so the verdict cannot name directories the
run will not search. It attributes to ``local-libvirt`` and rides that provider's single
contribution.
"""

from __future__ import annotations

import shutil
from typing import Protocol

from kdive.diagnostics.checks import DEPMOD_TOOLCHAIN_ID, Check
from kdive.diagnostics.provider_checks import DepmodToolchainCheck, DepmodToolchainProbe
from kdive.diagnostics.provider_contracts import WorkerVantageDescriptor
from kdive.providers.shared.module_staging_tools import DEPMOD, DEPMOD_SEARCH_PATH

_LOCAL_PROVIDER = "local-libvirt"


class ToolResolver(Protocol):
    """Resolve a binary name against an explicit search path, never ``PATH``.

    ``shutil.which``'s second positional parameter is ``mode``, so the path is always keyword.
    """

    def __call__(self, cmd: str, *, path: str) -> str | None: ...


def default_depmod_toolchain_probe(
    *,
    which: ToolResolver = shutil.which,
) -> DepmodToolchainProbe:
    """Build the probe that resolves ``depmod`` against the module-staging search contract.

    ``which`` is injected (default :func:`shutil.which`) so the probe is unit-tested without
    depending on the host's layout. Passing ``DEPMOD_SEARCH_PATH`` explicitly keeps ``PATH`` — and
    its ``os.defpath`` fallback — out of the resolution, the defect #2300 fixed.
    """

    async def _probe() -> str | None:
        return which(DEPMOD, path=DEPMOD_SEARCH_PATH)

    return _probe


def depmod_toolchain_worker_check() -> Check:
    """The module-staging toolchain check, for the single local-libvirt contribution."""
    return DepmodToolchainCheck(provider=_LOCAL_PROVIDER, probe=default_depmod_toolchain_probe())


def depmod_toolchain_worker_descriptor() -> WorkerVantageDescriptor:
    """The toolchain worker-check descriptor surfaced when the worker vantage is unavailable."""
    return WorkerVantageDescriptor(id=DEPMOD_TOOLCHAIN_ID, provider=_LOCAL_PROVIDER)
```

9. Wire it into the one `local-libvirt` contribution. In
   `src/kdive/diagnostics/contributions/multiarch_gdb.py` add to the imports:

```python
from kdive.diagnostics.contributions.depmod_toolchain import (
    depmod_toolchain_worker_check,
    depmod_toolchain_worker_descriptor,
)
```

   then append `depmod_toolchain_worker_check(),` as the last element of `_worker_checks()`'s list
   and `depmod_toolchain_worker_descriptor(),` as the last element of
   `_unavailable_worker_checks()`'s list.

10. In `src/kdive/diagnostics/result_codec.py`, add `DEPMOD_TOOLCHAIN_ID` to the existing
    `from kdive.diagnostics.checks import (...)` block and exactly one line to `_ALLOWED_IDS`,
    after `GUEST_ARCH_ACCEL_ID,`:

```python
        DEPMOD_TOOLCHAIN_ID,
```

    Change nothing else in that file: #2344 owns its structure. Its module docstring names no ids,
    so it needs no edit — confirm with `sed -n '1,12p' src/kdive/diagnostics/result_codec.py`.

11. In `tests/diagnostics/test_service.py`, add `DEPMOD_TOOLCHAIN_ID` to the
    `from kdive.diagnostics.checks import (...)` block inside the dispatcher test and to the
    `set(by_provider["local-libvirt"]._worker_check_ids) == {...}` set literal.
12. In `tests/diagnostics/test_default_factory.py`, add `DEPMOD_TOOLCHAIN_ID` to the module's
    `from kdive.diagnostics.checks import (...)` block and to **both** exact-set assertions on
    `unavailable_ids` (the one comparing against `{PROVIDER_TLS_ID, GDBSTUB_ACL_ID,
    "provider_authority", MULTIARCH_GDB_ID, PSERIES_FADUMP_ID, GUEST_ARCH_ACCEL_ID}` and the
    identical set in `test_factory_keeps_substitution_when_no_pool`).
13. Run `uv run python -m pytest tests/diagnostics -q`. Expected: all pass, no failures.
14. Run `just format`, then `just lint` and `just type`. Expected: exit 0 from each.
15. Run the full gate, capturing rather than inheriting the harness's streams:
    `just ci > /tmp/ci-2339.log 2>&1 < /dev/null`. Expected: exit 0. Read the log: ruff, `ty`, the
    lock check, the shell/workflow/Ansible lints, the mermaid and doc-link guards, the
    generated-artifact checks, and pytest all report success. If `check-mermaid` reports "Mermaid
    checker dependencies are missing", run `just install-mermaid-deps` and re-run — a missing local
    prerequisite in a fresh worktree, not a defect in this change. Fix any other failure at its
    cause and re-run the recipe bare.
16. Commit: `feat(diagnostics): add a depmod module-staging toolchain vantage`.

**Acceptance.** `tests/diagnostics` is fully green; `ops.diagnostics` carries a `depmod_toolchain`
item attributed to `local-libvirt`; exactly one `JobWorkerCheckDispatcher` is built for that
provider; `result_codec.py`'s diff is one import name and one set entry; `just ci` exits 0.
**Rollback:** `git revert` the commit; the install path keeps working.

## Self-review against the spec

Every spec requirement has a task. Scope 1, Success 4, and Success 6 are Task 1 — the shared
constant, the import that makes drift inexpressible, and the existing `_resolve_depmod` test kept
green. Scope 2 is Task 2 steps 5–7, Scope 3 steps 8–9, Scope 4 step 10. Success 1–3
are Task 2 steps 1, 7, and 9; Success 5 steps 2–3 and 10; Success 7 step 15. Threat-model control 3
(the constant and its comment move verbatim) is Task 1 steps 1–2. No task serves no requirement.

Names borrowed rather than defined, each confirmed present with the assumed signature on
`origin/main` at 14f9f462b:

- `src/kdive/diagnostics/checks.py` — `Check`, `CheckResult`, `CheckStatus`, `Vantage`,
  `run_check`. `src/kdive/diagnostics/provider_contracts.py` — `WorkerVantageDescriptor(id: str,
  provider: str)`. `src/kdive/diagnostics/result_codec.py` — `_ALLOWED_IDS`, `serialize_results`,
  `deserialize_results`.
- `src/kdive/diagnostics/provider_checks.py` line 42 — `_MISSING_DEPENDENCY =
  ErrorCategory.MISSING_DEPENDENCY`; `src/kdive/domain/errors.py` line 25 — the enum member.
- `src/kdive/diagnostics/contributions/multiarch_gdb.py` lines 128, 138, 146 — `_worker_checks()`,
  `_unavailable_worker_checks()`, `diagnostic_contribution()`.
- `.../boot/guest_kernel_writer.py` lines 113, 49, 59 — `_resolve_depmod`, `_DEPMOD`,
  `_DEPMOD_SEARCH_DIRS`; `.../boot/test_module_indexing.py` line 211 and
  `tests/diagnostics/test_result_codec.py` — the two existing tests named above.
- `shutil.which(cmd, mode=os.F_OK | os.X_OK, path=None)` — Python 3.14 standard library. Its second
  **positional** parameter is `mode`, not `path`; the self-review caught the probe passing the
  search path positionally, and Task 2 now uses a `Protocol` with a keyword-only `path`.

## Deferrals carried into the build

- **`bootstrap_elf._TOOL_PATH`** (`src/kdive/jobs/capture_operations/bootstrap/bootstrap_elf.py`
  line 23) holds the same four directories as a string literal for a different tool set. Folding it
  into `module_staging_tools` is outside #2339's approved surface. Owner: unfiled follow-up
  candidate, reported in this run's completion report; no tracker issue exists yet.
