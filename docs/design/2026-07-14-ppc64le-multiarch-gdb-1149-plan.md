# Implementation plan — multiarch gdb selection (#1149)

Spec: `2026-07-14-ppc64le-multiarch-gdb-1149.md` · ADR: `../adr/0347-cross-arch-gdb-binary-selection.md`
Branch: `feat/multiarch-gdb-1149` · Base: `main`
Guardrails: `just lint` · `just type` (whole tree) · `just test` · full gate `just ci`.
Run a single test: `uv run python -m pytest <path>::<name> -q`.

TDD throughout: write the failing test, then the code, then green. The engine `attach`
changes are `# pragma: no cover - live_vm`; the coverage lives in the pure helpers and the
check, so every task below is unit-testable without a real gdb or VM.

## Task 1 — Arch helpers (pure, the heart of the change)

**What.** New module `src/kdive/providers/shared/debug_common/gdbmi/policy/arch.py` with three
pure functions and their tables:

- `arch_from_elf(path: Path) -> str | None` — read the ELF prefix (magic `\x7fELF` at 0,
  `EI_CLASS`/`EI_DATA` at 4/5, `e_machine` as a 2-byte field at offset 18 with endianness from
  `EI_DATA`). Map `EM_X86_64` (62) → `"x86_64"`; `EM_PPC64` (21) with `ELFDATA2LSB` (1) →
  `"ppc64le"`. Non-ELF magic, a file shorter than 20 bytes, an unreadable file, or any machine
  outside `arch_traits.SUPPORTED_ARCHES` → `None`. Read at most the header bytes; never load the
  whole file.
- `select_gdb_binary(host_arch: str, guest_arch: str | None, which: Callable[[str], str | None])
  -> str | None` — `guest_arch is None or guest_arch == host_arch` → `which("gdb")`; else
  `which("gdb-multiarch") or which("gdb")`. Return the resolved path or `None`.
- `gdb_target_arch_name(arch: str) -> str | None` — `{"x86_64": "i386:x86-64", "ppc64le":
  "powerpc:common64"}.get(arch)`.

Keep the EM/gdb-name tables in this module (debugger specifics, out of domain `arch_traits`);
import `SUPPORTED_ARCHES` from `kdive.domain.platform.arch_traits` for the `arch_from_elf`
allowlist.

**Where.** New file only. No edits to existing files in this task.

**Tests** (`tests/providers/shared/debug_common/gdbmi/policy/test_arch.py` — mirror the src
tree; create dirs as needed):
- `select_gdb_binary` matrix with a fake `which` (dict-backed): native x86→x86 → `gdb`;
  native ppc→ppc → `gdb`; x86 host + ppc guest with both present → `gdb-multiarch`; same with
  only `gdb` → `gdb`; same with neither → `None`; `guest_arch=None` → native `gdb`; guest
  unknown-string treated as cross (host≠guest) path.
- `arch_from_elf`: build tiny ELF-header byte fixtures in-memory written to `tmp_path` for
  `EM_X86_64`/LSB, `EM_PPC64`/LSB; assert `"x86_64"`/`"ppc64le"`. Non-ELF bytes, a 4-byte
  truncated file, a missing path, and `EM_386` (3, 32-bit, unsupported) → `None`. Big-endian
  `EM_PPC64` (`ELFDATA2MSB`) → `None` (BE out of scope).
- `gdb_target_arch_name`: known arches map; unknown → `None`.

**AC.** AC1, AC2. `just lint && just type && uv run python -m pytest
tests/providers/shared/debug_common/gdbmi/policy/test_arch.py -q` green.

**Rollback.** Delete the module + test; nothing else references it yet.

## Task 2 — Engine attach uses the helpers

**What.** `src/kdive/providers/shared/debug_common/gdbmi/core/engine.py`:

- Add `import platform` and import the three helpers from the Task-1 module.
- `__init__`: add `host_arch_finder: Callable[[], str] = platform.machine` and store it.
- `attach`: reorder to resolve+validate `vmlinux` first (keep the existing `bad_vmlinux_path`
  `CONFIGURATION_ERROR`), then:
  - `guest_arch = arch_from_elf(resolved_vmlinux)`; `host_arch = self._host_arch_finder()`.
  - `gdb_path = select_gdb_binary(host_arch, guest_arch, self._gdb_path_finder)`.
  - `gdb_path is None` → `MISSING_DEPENDENCY`: when `guest_arch not in (None, host_arch)` name
    `gdb-multiarch` (`missing_tools=["gdb-multiarch", "gdb"]`, message names the multiarch
    prerequisite); else keep the current `"missing required gdb tool"` / `missing_tools=["gdb"]`.
  - Spawn `[gdb_path, "--nx", "--quiet", "--interpreter=mi3"]` (unchanged flags).
  - After `-file-exec-and-symbols`, before `_connect_with_retry`, cross path only
    (`guest_arch not in (None, host_arch)`): if `gdb_target_arch_name(guest_arch)` is truthy,
    `self.execute_mi_command(attachment, f"-gdb-set architecture {name}")`.

**Where.** `engine.py` only. `attach`/`_connect_with_retry` are `# pragma: no cover - live_vm`.

**Tests.** No new unit test for `attach` itself (live-only). Add/keep a construction test that
`GdbMiEngine(host_arch_finder=lambda: "x86_64", ...)` accepts the new kwarg
(`tests/providers/shared/debug_common/gdbmi/core/test_engine*.py` — locate the existing engine
test module and extend it; if none unit-constructs the engine, assert the signature via a
direct `GdbMiEngine(...)` call). Do **not** contrive a fake that executes the `# pragma`
live-only body.

**AC.** `just lint && just type && just test` green; the two providers that construct
`GdbMiEngine` (`local_libvirt/composition.py`, `remote_libvirt/composition.py`) still typecheck
unchanged (they pass no `host_arch_finder`, so the default applies).

**Rollback.** Revert `engine.py`; Task 1 stands alone.

## Task 3 — Doctor check (framework side)

**What.**
- `src/kdive/diagnostics/checks.py`: add `MULTIARCH_GDB_ID = "multiarch_gdb"` beside the other
  `_ID` constants.
- `src/kdive/diagnostics/provider_checks.py`: add
  - `class MultiarchGdbOutcome(StrEnum)`: `SUPPORTED = "supported"`, `MISSING = "missing"`,
    `UNDETERMINABLE = "undeterminable"`.
  - `MultiarchGdbProbe = Callable[[], Awaitable[MultiarchGdbOutcome]]`.
  - `class MultiarchGdbCheck(Check)` (vantage `WORKER`, id `MULTIARCH_GDB_ID`, `provider`
    stored). `run()` maps: `SUPPORTED` → `PASS` (`detail` "a multiarch-capable gdb targets every
    supported foreign arch"); `MISSING` → `FAIL` with `fix` ("no gdb on this host can target a
    supported foreign architecture; install gdb-multiarch (Debian/Ubuntu) or a multiarch gdb
    build") and `failure_category=ErrorCategory.MISSING_DEPENDENCY`; `UNDETERMINABLE` → `ERROR`
    ("could not run a candidate gdb to a verdict").
- `src/kdive/diagnostics/result_codec.py`: import `MULTIARCH_GDB_ID` and add it to
  `_ALLOWED_IDS`.

**Where.** Those three diagnostics files. Confirm `MISSING_DEPENDENCY` exists in
`domain/errors.py` `ErrorCategory` (it is used by `engine.py` already) — reuse, do not invent.

**Tests** (`tests/diagnostics/test_provider_checks.py` — extend the existing module):
- Each outcome → the right `CheckResult` (status, presence/absence of `fix`,
  `failure_category`); a `FAIL` result satisfies the `__post_init__` "fail must name a fix"
  invariant (it will, since `fix` is set).
- `result_codec` round-trip: a `multiarch_gdb` `CheckResult` serializes and reconstructs
  (add/extend a test in `tests/diagnostics/test_result_codec.py`); confirm reconstruction no
  longer raises `unexpected worker-vantage check id`.

**AC.** AC3. `just test` green on the diagnostics tests.

**Rollback.** Remove the check class, the id constant, and the `_ALLOWED_IDS` entry.

## Task 4 — Local-libvirt diagnostic contribution + assembly

**What.**
- New `src/kdive/providers/local_libvirt/diagnostics/__init__.py` and
  `.../diagnostics/contribution.py`:
  - The real probe `default_multiarch_gdb_probe(*, host_arch=platform.machine(),
    supported=SUPPORTED_ARCHES, which=shutil.which, run=<real subprocess runner>) ->
    MultiarchGdbProbe` (async) — inject `host_arch`, `supported`, `which`, and the runner with
    real defaults so the empty-`foreign` branch is tested by passing a one-element `supported`
    set (no monkeypatching a module-imported frozenset). It computes `foreign = set(supported) -
    {host_arch}`. For each `arch` in `foreign`: `candidate = select_gdb_binary(host_arch, arch,
    which)`; if `candidate is None` → `MISSING`. Else run `candidate --batch -nx -ex "set
    architecture <name>" -ex "show architecture"` and **detect acceptance positively**: gdb's
    batch exit status does *not* reliably go non-zero on an unsupported `set architecture` (the
    exit code tracks the inferior, not a command error), so decide on stdout — require it to
    confirm the target (contains `<name>`, e.g. `The target architecture is set to "<name>"`).
    A non-matching / error-laden result for any foreign arch → `MISSING`; a spawn/`OSError`/
    timeout → `UNDETERMINABLE`. All foreign arches confirmed (or `foreign` empty) → `SUPPORTED`.
  - `diagnostic_contribution() -> DiagnosticProviderContribution`: `provider="local-libvirt"`,
    `enabled=lambda: True`, `checks=lambda: (MultiarchGdbCheck(provider="local-libvirt",
    probe=default_multiarch_gdb_probe()),)`, `unavailable_worker_checks` returning one
    `WorkerVantageDescriptor(id=MULTIARCH_GDB_ID, provider="local-libvirt")`,
    `worker_checks=` same as `checks`. Mirror `remote_libvirt/diagnostics/contribution.py` for
    the exact shape (worker-vantage checks are declared as unavailable descriptors when the
    server runs without a worker; copy that pattern).
- `src/kdive/providers/assembly/diagnostics.py`: import the local contribution and return it in
  the tuple alongside `remote_diagnostics()`.

**Where.** New local-libvirt diagnostics package + one edit to `assembly/diagnostics.py`. Read
`remote_libvirt/diagnostics/contribution.py` first and match its `_checks` /
`_unavailable_worker_checks` / `_worker_checks` structure so the assembly wiring is identical in
shape.

**Tests** (`tests/providers/local_libvirt/diagnostics/test_contribution.py`):
- Probe with an injected fake `which` + fake subprocess runner keyed on **stdout**: host
  `x86_64`, ppc runner stdout confirms `powerpc:common64` → `SUPPORTED`; candidate `None` →
  `MISSING`; runner stdout does not confirm the target (error text / wrong arch) → `MISSING`;
  runner raises `OSError` → `UNDETERMINABLE`; `supported={host_arch}` (one element, injected) →
  `SUPPORTED` with the runner never called.
- `diagnostic_contribution()` returns a `local-libvirt` contribution whose `checks()` yields one
  `MultiarchGdbCheck`, and `assembly.diagnostics.diagnostic_provider_contributions()` now
  includes it (assert provider names present).

**AC.** AC4. `just lint && just type && just test` green.

**Rollback.** Delete the local diagnostics package; revert the one-line `assembly/diagnostics.py`
edit. Tasks 1–3 stand.

## Task 5 — Agent-facing docstring note

**What.** In the debug-session **wrapper** tool docstring (the `@app.tool` wrapper that starts /
attaches a debug session — locate it under `src/kdive/mcp/tools/debug/`; the wrapper docstring,
not the inner handler, is the agent-facing contract per AGENTS.md), add one sentence: a
cross-architecture attach (guest arch ≠ host arch) needs a multiarch-capable gdb on the worker
host, and a `MISSING_DEPENDENCY` failure there means installing `gdb-multiarch` (or a multiarch
gdb build) — see the `multiarch_gdb` doctor check.

**Where.** One wrapper docstring under `mcp/tools/debug/`. Do not touch behavior.

**Tests.** If a generated-doc/schema snapshot test exists (e.g. `test_no_adr_leak`, a tool-schema
snapshot), run `just ci`/`just docs-check` so any snapshot regenerates; update the snapshot if the
repo tracks one. Do not add a new test just for prose.

**AC.** `just ci` green (this catches generated-doc drift the unit suite misses).

**Rollback.** Revert the docstring edit.

## Task 6 — Live smoke proof (AC5)

**What.** On the x86_64 host (this dev host runs KVM/libvirt + TCG directly): provision an
`arch=ppc64le` System under TCG, boot it, open a gdb debug session, and read registers. Capture
the evidence that the attach used a multiarch gdb and targeted ppc64le: the
`-data-list-register-names` set contains ppc64le GPR/SPR names (`r0`…`r31`, `pc`/`nip`) and no
x86 names (`rax`). Record the outcome in a proof note
`docs/design/2026-07-14-ppc64le-multiarch-gdb-proof-record-1149.md` (mirror the #1144/#1146 proof
records). Mark `UNVERIFIED` **only** if the host cannot run the proof at all (e.g. no ppc64le
guest bootable), never merely for effort.

**Where.** Proof note doc; no source change expected. If the live run surfaces a real defect
(wrong gdb chosen, `set architecture` rejected), fix it in `engine.py`/the helpers and add a
regression unit test.

**AC.** AC5. Proof note committed.

**Rollback.** N/A (doc + any forced fix carries its own test).

## Sequencing & cross-file notes

1 → 2 (2 imports 1). 3 → 4 (4 imports the check + id from 3). 1 also feeds 4's probe. 5 and 6
are independent tails. Order: 1, 2, 3, 4, 5, 6.

Cross-file conflict watch: `assembly/diagnostics.py`, `checks.py`, `result_codec.py`, and
`provider_checks.py` are shared diagnostics files — edits here are additive (new id, new class,
new tuple member); keep them minimal. No migration, no schema change, no new dependency, no
`docs/adr/README.md` churn beyond the row already added with the ADR.
