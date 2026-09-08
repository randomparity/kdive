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
contribution, whose ids ride a single worker job and are reconstructed through the inline codec.
**Tech stack:** Python 3.14, `uv`, pytest, ruff, `ty`. No new dependency.

Expected implementation size: 220–300 changed lines (M) — from the file map below: ~26 lines of new
shared module, ~50 of new check class, ~55 of new contribution, ~15 of edits across five existing
source files, ~8 of operator doc, and ~110 of tests.

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
- Doc-style guard: use **Milestone**, never "Sprint"; avoid "critical", "robust", "comprehensive",
  "elegant" in prose, ADRs, commit messages, and code comments. Commits follow Conventional Commits
  1.0.0, imperative, subject ≤72 characters, one logical change. Guardrails: `just format` before
  committing a Python-only change; `just lint`, `just type`, and focused `pytest` while iterating;
  `just ci > <file> 2>&1 < /dev/null` as the pre-push gate, run **bare** — no pipe, no
  `>/dev/null`, no `|| true`, no trailing `; echo $?`.

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
| `docs/operating/install.md` | changed | One operator-facing note: the `kmod` package, the four-directory search, and the new check |
| `docs/debt/0012-shipped-worker-image-has-no-depmod.md` | created (design phase) | The deferred `Dockerfile` provisioning gap this check exposes |

## Task 1 — Share the module-staging tool contract

**Creates:** `src/kdive/providers/shared/module_staging_tools.py`
**Modifies:** `src/kdive/providers/local_libvirt/lifecycle/boot/guest_kernel_writer.py`

**Interfaces — provided to Task 2:** `DEPMOD: str` (`"depmod"`), `DEPMOD_SEARCH_DIRS:
tuple[str, str, str, str]` (`("/usr/sbin", "/usr/bin", "/sbin", "/bin")`), and
`DEPMOD_SEARCH_PATH: str` (`"/usr/sbin:/usr/bin:/sbin:/bin"`). **Consumes:** nothing.

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
# <every comment line that currently sits between `_DEPMOD` and `_DEPMOD_SEARCH_DIRS` in
#  guest_kernel_writer.py — nine lines on origin/main at 14f9f462b, twelve after a rebase over
#  #2340 — copied verbatim, from "depmod is an sbin tool — /usr/sbin under merged-usr …" to the
#  end of the block. Copy, do not paraphrase: they record why PATH is excluded and why
#  /usr/local/{sbin,bin} are.>
DEPMOD_SEARCH_DIRS = ("/usr/sbin", "/usr/bin", "/sbin", "/bin")
DEPMOD_SEARCH_PATH = os.pathsep.join(DEPMOD_SEARCH_DIRS)
```

2. In `guest_kernel_writer.py`, delete the module-level `_DEPMOD = "depmod"` line, **every comment
   line between it and `_DEPMOD_SEARCH_DIRS`** — whatever the count is when you run this; on a
   rebase over #2340 it includes that branch's three-line note about there being no operator
   override — and the `_DEPMOD_SEARCH_DIRS = (...)` line itself. Move all of those comment lines
   into step 1's module verbatim. Leave `_DEPMOD_STDERR_MAX = 500` above and `_DEPMOD_EXEC_ERRNOS`
   below untouched.

   **Rebase note.** Branch `feat/host-tool-resolve-2333` (unmerged) names `_DEPMOD_SEARCH_DIRS`
   twice in `src/kdive/providers/local_libvirt/lifecycle/host_tool_search.py`'s docstring
   (lines 9 and 12). Whichever of the two branches lands second repoints those two references at
   `module_staging_tools.DEPMOD_SEARCH_DIRS`.
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
`depmod_toolchain_worker_check`, `depmod_toolchain_worker_descriptor` — signatures in steps 5–8
below. Nothing later in this plan consumes them.

### Verification

All four cases below live in `tests/diagnostics/test_depmod_toolchain.py`, are red before step 5
with `ImportError: cannot import name 'DEPMOD_TOOLCHAIN_ID' from 'kdive.diagnostics.checks'`, and
go green with `uv run python -m pytest tests/diagnostics/test_depmod_toolchain.py -q`:

- **A resolved `depmod` gives a `pass` naming the path, no `fix`, no `failure_category`** —
  `::test_resolved_depmod_passes`. **An unresolved one gives a `fail` whose `detail` names the
  binary and all four directories, whose `fix` names `kmod`, and whose `failure_category` is
  `MISSING_DEPENDENCY`** — `::test_missing_depmod_fails_naming_the_searched_dirs`. **The id and
  vantage** — `::test_check_id_and_vantage`. **The default probe searches the shared list, never
  `PATH`** — `::test_default_probe_searches_the_shared_dirs`, injecting a recording resolver and
  asserting it received `DEPMOD` and `DEPMOD_SEARCH_PATH`. All four: Mode: focused-test.

The rest extend existing tests:

- **The assembled `local-libvirt` contribution dispatches the new id through exactly one
  dispatcher.** Mode: focused-test — `tests/diagnostics/test_service.py`'s
  `set(by_provider["local-libvirt"]._worker_check_ids) == {...}` assertion extended with
  `DEPMOD_TOOLCHAIN_ID`; it already asserts `set(by_provider) == {"local-libvirt",
  "remote-libvirt"}`, so a second contribution would fail it. **Red after step 5** — the extended
  four-member literal against an assembled set that still holds three — and green after step 9's
  wiring. Command: `uv run python -m pytest tests/diagnostics/test_service.py -q`.
- **The id joins the substituted set when no dispatch is wired.** Mode: focused-test —
  `tests/diagnostics/test_default_factory.py`'s two exact `unavailable_ids` assertions extended the
  same way. **Red after step 5**, green after step 9. Command:
  `uv run python -m pytest tests/diagnostics/test_default_factory.py -q`.
- **`_ALLOWED_IDS` tracks every registered descriptor, and a result survives the inline round
  trip.** Mode: focused-test — the existing
  `tests/diagnostics/test_result_codec.py::test_allowed_ids_matches_registered_worker_vantage_descriptors`
  (red once the descriptor is registered without the allowlist entry, reporting the set mismatch)
  and the new `::test_depmod_toolchain_id_survives_roundtrip` (red before step 5: the deserialized
  item is `CheckStatus.ERROR` with detail `unexpected worker-vantage check id 'depmod_toolchain'`).
  Green: `uv run python -m pytest tests/diagnostics/test_result_codec.py -q`.
- **The operator doc names the check, the package, and the four-directory search.** Mode:
  task-test-not-applicable — `docs/operating/install.md` prose has no executable consumer beyond
  `just docs-links` and `just docs-paths`, which the gate below already runs.
- **The guardrail suite passes on the assembled change.** Mode: focused-test — `just ci` run bare
  with output captured to a file; green is exit 0 and no failures in the captured log.

### Steps

1. Write the failing tests first. Create `tests/diagnostics/test_depmod_toolchain.py`:

**`pytest-asyncio` is not a dependency and pytest 9.1.1 fails a bare `async def` test**, so these
tests drive the coroutines through `asyncio.run` from synchronous test functions, matching
`tests/diagnostics/test_guest_arch_accel.py:42-47`. Do not write `@pytest.mark.asyncio`.

```python
"""Tests for the module-staging toolchain worker-vantage check (ADR-0635, #2339)."""

from __future__ import annotations

import asyncio

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


def test_missing_depmod_fails_naming_the_searched_dirs() -> None:
    result = asyncio.run(_check(None).run())
    assert result.status is CheckStatus.FAIL
    assert result.failure_category is ErrorCategory.MISSING_DEPENDENCY
    assert "depmod" in result.detail
    for directory in DEPMOD_SEARCH_DIRS:
        assert directory in result.detail
    assert result.fix is not None
    assert "kmod" in result.fix
```

Three more cases in the same file, same `asyncio.run` shape:

- `test_check_id_and_vantage` — on `_check("/usr/sbin/depmod")` (no `asyncio.run`; both are
  properties): `check.id == DEPMOD_TOOLCHAIN_ID == "depmod_toolchain"` and
  `check.vantage is Vantage.WORKER`.
- `test_resolved_depmod_passes` — on `asyncio.run(_check("/usr/sbin/depmod").run())`:
  `status is CheckStatus.PASS`, `check_id == DEPMOD_TOOLCHAIN_ID`, `provider == _PROVIDER`,
  `"/usr/sbin/depmod" in result.detail`, `result.fix is None`,
  `result.failure_category is None`.
- `test_default_probe_searches_the_shared_dirs` — a local `def _which(cmd: str, *, path: str)`
  appending `(cmd, path)` to a `seen: list[tuple[str, str]]` and returning `"/sbin/depmod"`; then
  `assert asyncio.run(default_depmod_toolchain_probe(which=_which)()) == "/sbin/depmod"` and
  `assert seen == [(DEPMOD, DEPMOD_SEARCH_PATH)]`. The keyword-only `path` in the double matters:
  it is what proves the production probe cannot be passing the path as `shutil.which`'s `mode`.

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
5. Make the four one-line additions that give the id a home:
   - `src/kdive/diagnostics/checks.py` — `DEPMOD_TOOLCHAIN_ID = "depmod_toolchain"`, after
     `GUEST_ARCH_ACCEL_ID = "guest_arch_accel"`.
   - `src/kdive/diagnostics/result_codec.py` — `DEPMOD_TOOLCHAIN_ID,` after `GUEST_ARCH_ACCEL_ID,`
     in `_ALLOWED_IDS`, plus the name in that file's `from kdive.diagnostics.checks import (...)`
     block. **Change nothing else there**: #2344 owns that file's structure. Its module docstring
     names no ids, so it needs no edit — confirm with
     `sed -n '1,12p' src/kdive/diagnostics/result_codec.py`.
   - `tests/diagnostics/test_service.py` — `DEPMOD_TOOLCHAIN_ID` in the dispatcher test's
     `from kdive.diagnostics.checks import (...)` block and in the
     `set(by_provider["local-libvirt"]._worker_check_ids) == {...}` literal.
   - `tests/diagnostics/test_default_factory.py` — the same name in the module's checks import and
     in **both** exact `unavailable_ids` sets (the one against `{PROVIDER_TLS_ID, GDBSTUB_ACL_ID,
     "provider_authority", MULTIARCH_GDB_ID, PSERIES_FADUMP_ID, GUEST_ARCH_ACCEL_ID}` and the
     identical set in `test_factory_keeps_substitution_when_no_pool`). The third such set in that
     file is synthetic and must not be touched.

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

7. In the same file, append `DepmodToolchainProbe = Callable[[], Awaitable[str | None]]` and a
   `DepmodToolchainCheck(Check)` after `GuestArchAccelCheck`. Copy the shape of
   `PseriesFadumpCheck` (`__init__(self, *, provider: str, probe: ...)` storing `self._provider` /
   `self._probe`; `id` returning `DEPMOD_TOOLCHAIN_ID`; `vantage` returning `Vantage.WORKER`). Its
   `run()` awaits the probe once and branches on `resolved is not None`. These are the parts the
   shape does not give you — the strings are asserted by the tests, so they are the contract:

```python
        # resolved is not None -> PASS, no fix, no failure_category
        detail=f"module staging can index kernel modules with depmod at {resolved}"

        # resolved is None -> FAIL
        detail=(
            "depmod is required on this worker host to index kernel modules for staging, "
            f"and was not found in any of {DEPMOD_SEARCH_PATH}"
        ),
        fix=DEPMOD_TOOLCHAIN_MISSING_FIX,
        failure_category=_MISSING_DEPENDENCY,
```

   Both results carry `check_id=self.id` and `provider=self._provider`. There is no third branch:
   `checks.run_check` already maps a timeout or an unexpected exception to `error`.

8. Create `src/kdive/diagnostics/contributions/depmod_toolchain.py`, mirroring
   `contributions/pseries_fadump.py`'s shape — a module docstring citing ADR-0635 and #2339 and
   saying the probe reads the same contract the install path resolves against,
   `_LOCAL_PROVIDER = "local-libvirt"`, and three definitions:

```python
class ToolResolver(Protocol):
    def __call__(self, cmd: str, *, path: str) -> str | None: ...

def default_depmod_toolchain_probe(*, which: ToolResolver = shutil.which) -> DepmodToolchainProbe:
    async def _probe() -> str | None:
        return which(DEPMOD, path=DEPMOD_SEARCH_PATH)
    return _probe

def depmod_toolchain_worker_check() -> Check:
    return DepmodToolchainCheck(provider=_LOCAL_PROVIDER, probe=default_depmod_toolchain_probe())

def depmod_toolchain_worker_descriptor() -> WorkerVantageDescriptor:
    return WorkerVantageDescriptor(id=DEPMOD_TOOLCHAIN_ID, provider=_LOCAL_PROVIDER)
```

   Three things the signatures do not tell you. `ToolResolver` is a `Protocol`, not a
   `Callable[[str, str], ...]`, because `shutil.which`'s second **positional** parameter is `mode`
   — the search path must be keyword-only or the default resolver is called wrongly. `which` is
   injected so the probe is unit-tested without depending on the host's layout. And
   `DEPMOD_SEARCH_PATH` is passed explicitly on every call: that is what keeps `PATH` and its
   `os.defpath` fallback out of the resolution, which is the defect #2300 fixed and this check
   reports on. Imports: `shutil`, `typing.Protocol`,
   `kdive.diagnostics.checks.{DEPMOD_TOOLCHAIN_ID, Check}`,
   `kdive.diagnostics.provider_checks.{DepmodToolchainCheck, DepmodToolchainProbe}`,
   `kdive.diagnostics.provider_contracts.WorkerVantageDescriptor`, and
   `kdive.providers.shared.module_staging_tools.{DEPMOD, DEPMOD_SEARCH_PATH}`.

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
   `_unavailable_worker_checks()`'s list. This is the step that turns `test_service.py` and
   `test_default_factory.py` back green: step 5 extended their id-set literals to four members
   while the assembled contribution still produced three.

10. Run `uv run python -m pytest tests/diagnostics -q`. Expected: all pass, no failures.
11. In `docs/operating/install.md`, under `## Host prerequisites` and before
    `### Development and CI toolchain`, add one subsection — matching the tone of the
    `guest_arch_accel` note at line 287:

```markdown
### Module staging (worker hosts)

Installing a built kernel indexes its modules with the host's `depmod` (ADR-0346), so a
worker host needs `kmod` — `apt install kmod` on Debian/Ubuntu, `dnf install kmod` on
Fedora. Resolution is **not** `PATH`-based: `depmod` is looked for in `/usr/sbin`,
`/usr/bin`, `/sbin`, and `/bin` only, so a copy installed elsewhere will not be found.
The service `doctor` (`kdivectl doctor --json`) carries a `depmod_toolchain` check that
fails with the package name and those four directories when it cannot resolve one.
```

12. Run `just format`, then `just lint` and `just type`. Expected: exit 0 from each.
13. Run the full gate, capturing rather than inheriting the harness's streams:
    `just ci > /tmp/ci-2339.log 2>&1 < /dev/null`. Expected: exit 0. Read the log: ruff, `ty`, the
    lock check, the shell/workflow/Ansible lints, the mermaid and doc-link guards, the
    generated-artifact checks, and pytest all report success. If `check-mermaid` reports "Mermaid
    checker dependencies are missing", run `just install-mermaid-deps` and re-run — a missing local
    prerequisite in a fresh worktree, not a defect in this change. Fix any other failure at its
    cause and re-run the recipe bare.
14. Commit: `feat(diagnostics): add a depmod module-staging toolchain vantage`.

**Acceptance.** `tests/diagnostics` is fully green; `ops.diagnostics` carries a `depmod_toolchain`
item attributed to `local-libvirt`; exactly one `JobWorkerCheckDispatcher` is built for that
provider; `result_codec.py`'s diff is one import name and one set entry; `just ci` exits 0.
**Rollback:** `git revert` the commit; the install path keeps working.

## Self-review against the spec

Every spec requirement has a task. Scope 1, Success 4, and Success 6 are Task 1 — the shared
constant, the import that makes drift inexpressible, and the existing `_resolve_depmod` test kept
green. Scope 2 is Task 2 steps 5–7, Scope 3 steps 8–9, Scope 4 step 5, Scope 5 step 11. Success
1–3 are Task 2 steps 1, 7, and 9; Success 5 steps 2–3 and 5; Success 7 step 11; Success 8 step 13.
Threat-model boundary (a) control is Task 1 steps 1–2 (the constant and its comment move verbatim);
boundary (b)'s control is the existing allowlist agreement test, extended in Task 2 step 3. The
spec's "Deployments this changes" paragraph is discharged by
`docs/debt/0012-shipped-worker-image-has-no-depmod.md`, written in the design phase. No task serves
no requirement.

Names borrowed rather than defined, each confirmed present with the assumed signature on
`origin/main` at 14f9f462b:

- `diagnostics/checks.py` — `Check`, `CheckResult`, `CheckStatus`, `Vantage`, `run_check`;
  `provider_contracts.py` — `WorkerVantageDescriptor(id: str, provider: str)`; `result_codec.py` —
  `_ALLOWED_IDS`, `serialize_results`, `deserialize_results`; `provider_checks.py:42` —
  `_MISSING_DEPENDENCY = ErrorCategory.MISSING_DEPENDENCY`, and `domain/errors.py:25` the member.
- `contributions/multiarch_gdb.py` lines 128, 138, 146 — `_worker_checks()`,
  `_unavailable_worker_checks()`, `diagnostic_contribution()`.
- `.../boot/guest_kernel_writer.py` lines 113, 49, 59 — `_resolve_depmod`, `_DEPMOD`,
  `_DEPMOD_SEARCH_DIRS`; `.../boot/test_module_indexing.py` line 211 and
  `tests/diagnostics/test_result_codec.py` — the two existing tests named above.
- `shutil.which(cmd, mode=os.F_OK | os.X_OK, path=None)` — Python 3.14 standard library. Its second
  **positional** parameter is `mode`, not `path`; the self-review caught the probe passing the
  search path positionally, and Task 2 now uses a `Protocol` with a keyword-only `path`.
- The async-test convention: `asyncio.run` from a synchronous test function, established by
  `tests/diagnostics/test_guest_arch_accel.py:42-47` and `test_gdbstub_acl_probe.py:34`.
  `pytest-asyncio` is **not** a dependency — `rg -n "pytest-asyncio" pyproject.toml uv.lock` is
  empty — so `@pytest.mark.asyncio` would leave the coroutine uncollected and pytest 9.1.1 fails
  the test with "async def functions are not natively supported". The design review reproduced
  that; the plan's tests use `asyncio.run`.

## Deferrals carried into the build

- **The shipped worker image has no `depmod`.** Owner:
  [`docs/debt/0012-shipped-worker-image-has-no-depmod.md`](../../debt/0012-shipped-worker-image-has-no-depmod.md).
  The check's `fail` on a container deployment is correct and must not be softened; the missing
  piece is a `Dockerfile` package, outside this surface.
- **Four other literal copies of `"/usr/sbin:/usr/bin:/sbin:/bin"`** —
  `src/kdive/jobs/capture_operations/bootstrap/bootstrap_elf.py:23` (a `which` search path, the
  closest sibling), and three subprocess-environment `PATH` assignments at
  `src/kdive/__main__.py:281`, `src/kdive/jobs/capture_operations/launcher.py:292`, and
  `scripts/generate/build-capture-bootstrap-manifest.py:28`. Consolidating them is outside #2339's
  approved surface. Owner: unfiled follow-up candidate, reported in this run's completion report;
  no tracker issue exists yet.
- **`docs/operating/install.md` documents a `guest_arch_accel` `data` map that the inline codec
  drops** — `serialize_results` never emits `data`, so a worker-vantage check's `data` is always
  `{}` by the time `ops.diagnostics` projects it. Adjacent to this change. Owner: unfiled
  follow-up candidate, reported in this run's completion report.
