# Implementation plan — x86_64-guest symmetry audit for ppc64le hosts (#1155)

Spec: `docs/design/2026-07-15-x86-guest-symmetry-audit-1155.md`
ADR: `docs/adr/0354-host-arch-guest-symmetry-invariant.md`
Branch: `feat/x86-guest-symmetry-audit-1155` · Base: `main`
Epic: #1139 · Depends on: #1141, #1142 (both merged)

## Shape of the work

This is a **test + audit** issue. The audit (in the spec) found no production code path derives
guest-facing behavior from the host arch, so **no `src/` file changes**. The deliverables are:

- one new static guard test (the load-bearing artifact),
- two additions to existing behavioral test files (admission + domain-XML),
- the already-committed spec + ADR-0354 + index row.

The deadline dimension needs no test (structural — the deadline path takes only `accel`).

Guardrails (run before each commit; CI runs these sub-recipes individually):
`just lint` · `just type` (whole tree, src + tests) · `just test`. Single test:
`uv run python -m pytest <path>::<name> -q`.

Because the production code is unchanged, the TDD rhythm here is: for the guard's **detection
function**, write a failing unit test (synthetic positive fixture) → implement → green; for the
**whole-tree** and **behavioral** assertions, they characterize existing behavior and go green
immediately, so their non-vacuity is proven by a transient local mutation (verify-it-fails, then
revert — never committed) plus the guard's own negative fixture.

Conventions: absolute imports only; ≤100 lines/function; Google-style docstrings on non-trivial
public helpers; line length 100; ruff lint set `E,F,I,UP,B,SIM`; tests mirror the package tree
under `tests/`. Doc-style: no "sprint"/"critical"/"robust"/"comprehensive"/"elegant".

---

## Task 1 — Static host-arch confinement guard (the core deliverable)

**Where it fits:** encodes ADR-0354 / AC#2 of the issue — "no code path derives guest-facing
behavior from the host arch except accelerator selection." Enforces that host-arch reads stay
confined to the three accel/gdb binary-selection modules.

**File to create:** `tests/domain/platform/test_host_arch_confinement.py` (new; the
`tests/domain/platform/` dir already exists — `test_arch_traits.py` lives there).

**Implement, in this order:**

1. A pure detection helper in the test module (kept in the test tree — it is test infrastructure,
   not production API):
   ```
   def module_reads_host_arch(source: str) -> bool
   def host_arch_reading_modules(package_root: Path) -> tuple[set[str], int]
   ```
   `module_reads_host_arch` parses `source` with `ast` and returns `True` iff it contains an
   `ast.Attribute` whose attribute name is one of `machine`/`uname`/`processor`/`architecture`
   on a `platform` value, or `uname` on an `os` value. Match on the `ast.Attribute` node
   (`node.value` is an `ast.Name` with `id in {"platform", "os"}` and `node.attr` the idiom), so
   `platform.machine()`, the bare `platform.machine` default-arg, and `platform.uname().machine`
   all count, while a docstring/comment mention does not (AST ignores string contents).
   `host_arch_reading_modules` walks `package_root.rglob("*.py")`, applies the predicate, returns
   `({repo-relative "kdive/..." paths that hit}, total_files_scanned)`.

2. `_HOST_ARCH_READ_ALLOWLIST = frozenset({...})` — the three modules from the spec/ADR, keyed
   as `"kdive/diagnostics/guest_arch_accel.py"`, `"kdive/diagnostics/multiarch_gdb.py"`,
   `"kdive/providers/shared/debug_common/gdbmi/core/engine.py"`, each with an inline comment
   naming its accel/gdb-selection role.

3. Tests:
   - `test_detects_platform_machine_read` / `test_detects_os_uname_read` /
     `test_detects_platform_uname_dot_machine` — positive fixtures (synthetic source strings)
     are reported. (Non-vacuity — proves the walker matches something, independent of the tree.)
   - `test_ignores_docstring_and_comment_mention` — a synthetic source whose only
     `platform.machine()` occurrence is in a docstring and a comment is **not** reported (the
     AST-vs-grep discrimination ADR-0354 rests on).
   - `test_host_arch_reads_confined_to_allowlist` — run `host_arch_reading_modules` over the real
     `src/kdive`; assert the module set `⊆ _HOST_ARCH_READ_ALLOWLIST`. On failure, the assertion
     message lists the offending modules and points at ADR-0354.
   - `test_scan_is_non_vacuous` — assert the same call's `total_files_scanned >= 100` (real count
     is ~636), so an empty/misrooted glob fails loudly rather than passing a vacuous subset.

   Resolve `src/kdive` from the test file location (`Path(__file__).parents[N] / "src" / "kdive"`)
   or via `import kdive; Path(kdive.__file__).parent`; assert the resolved path exists so a wrong
   base dir fails at the assertion, not silently.

**Acceptance criteria (reviewer-checkable):**
- `just test` collects and passes this module (it is unmarked, so it runs in the ordinary suite).
- `test_host_arch_reads_confined_to_allowlist` passes on the current tree (the three known reads
  are exactly the allowlist).
- Temporarily adding `import platform; platform.machine()` to a guest-facing module (e.g.
  `src/kdive/providers/local_libvirt/lifecycle/xml.py`) makes the test fail naming that module;
  revert after verifying (do not commit the mutation).
- The negative fixture passes (docstring mention not flagged) — proving AST, not grep.

**Guardrails:** `just lint && just type && uv run python -m pytest
tests/domain/platform/test_host_arch_confinement.py -q`.

**Rollback:** delete the file; no other file is touched.

---

## Task 2 — Admission inverted-matrix test (x86_64 guest → accel=tcg)

**Where it fits:** completes the inverted host/guest matrix at admission — a ppc64le host
advertising `{ppc64le: kvm, x86_64: tcg}` admits an x86_64 guest and records `accel=tcg`.
Defense-in-depth against a future x86-specific special-case in the arch-agnostic resolution.

**File to edit:** `tests/integration/test_systems_admission_arch.py` (real-DB harness; the module
already has `_X86_GUEST_ARCHES`, `_PPC_ONLY_GUEST_ARCHES`, `_set_resource_guest_arches`, and the
`migrated_url` fixture pattern).

**Implement:**
1. Add a module constant `_PPC_HOST_GUEST_ARCHES = {"ppc64le": {"accel": "kvm", "emulator":
   "/usr/bin/qemu-system-ppc64le"}, "x86_64": {"accel": "tcg", "emulator":
   "/usr/bin/qemu-system-x86_64"}}` — the inverted (POWER-host) matrix.
2. Add `test_provision_records_tcg_accel_for_x86_guest_on_ppc_host(migrated_url)` modeled on the
   existing `test_provision_records_accel_when_host_advertises_arch`: seed the allocation, set the
   Resource's `guest_arches` to `_PPC_HOST_GUEST_ARCHES` via `_set_resource_guest_arches`,
   provision the **default (x86_64)** profile, and assert the System row records
   `accel == "tcg"` (the inverse of the existing x86-native `accel == "kvm"` case).

**Acceptance criteria:**
- The new test passes against the real DB (`just test` includes it; it is not `live_*`-marked).
- It asserts `accel == "tcg"` for an x86_64 profile — the inverted-key case not previously
  asserted (existing tcg-recording tests use a ppc64le profile).

**Guardrails:** `just lint && just type && uv run python -m pytest
tests/integration/test_systems_admission_arch.py -q` (needs Docker/testcontainers; skips cleanly
without unless `KDIVE_REQUIRE_DOCKER=1`).

**Rollback:** remove the constant + test function; the file is otherwise unchanged.

---

## Task 3 — Domain-XML console assertion for the x86_64+tcg cell

**Where it fits:** documents the full inverted-matrix render (q35 + ttyS0 + type=qemu +
qemu-system-x86_64) in one place. The console is arch-derived and already covered elsewhere, so
this is completeness, not a new branch.

**File to edit:** `tests/providers/local_libvirt/test_provisioning.py`
(`test_render_domain_by_arch_and_accel` and its parametrization at the top of the render tests).

**Implement:** extend `test_render_domain_by_arch_and_accel` to also assert the `<cmdline>`
console token per arch — add an `exp_console` column to the parametrize (`"ttyS0"` for x86_64,
`"hvc0"` for ppc64le) and assert `f"console={exp_console}"` is in the rendered `os/cmdline`
text — OR add a focused `test_render_x86_tcg_uses_ttyS0_console` if extending the parametrize
signature is noisier. Prefer extending the existing parametrize so all four cells gain the
console assertion symmetrically. Keep the existing assertions intact.

**Acceptance criteria:**
- The x86_64+tcg cell asserts `console=ttyS0`; the ppc64le cells assert `console=hvc0`.
- `test_render_domain_by_arch_and_accel` still passes for all four cells.

**Guardrails:** `just lint && just type && uv run python -m pytest
tests/providers/local_libvirt/test_provisioning.py -q`.

**Rollback:** revert the parametrize/assertion change; single-file, self-contained.

---

## Final verification (before PR)

1. `just ci` green (lint, type, lint-shell, lint-workflows, check-mermaid, test).
2. `git diff --name-only main` shows only `tests/` + `docs/` (spec, ADR, README index) — **no
   `src/` changes** (AC8). If any `src/` file changed, the audit's premise was wrong — stop and
   reassess, do not silently ship a production change under a test issue.
3. The confinement guard's fail path was manually verified once (Task 1 AC) and reverted.
