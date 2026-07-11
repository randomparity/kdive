# Remote boot: provider-aware `root=` cmdline + XFS root support — Implementation Plan

> **For agentic workers:** Steps use checkbox (`- [ ]`) syntax. TDD per task: failing test first, confirm it fails, minimal implementation, confirm green, then guardrails.

**Goal:** Fix #587 — a remote-libvirt System boots into emergency mode because (1) the platform appends
`root=/dev/vda` over the base image's correct `root=UUID=…`, and (2) the built kernel lacks XFS to mount
the XFS root.

**Architecture:** Express the platform-owned `root=` as `ProviderRuntime.platform_root_cmdline`
(`"root=/dev/vda"` default; remote `None`), thread it through `system_required_cmdline`/`cmdline_for` and
the two call sites that already hold the runtime. Add `CONFIG_XFS_FS=y`/`CONFIG_XFS_POSIX_ACL=y` to the
`kdump` fragment.

**Tech Stack:** Python 3.14, `uv`, `ruff`, `ty`, `pytest`.

**Execution note (ordering):** Tasks 1–3 are a single atomic change — changing the
`system_required_cmdline`/`cmdline_for` signatures (Task 1) without updating both callers (Task 3) leaves
the tree red. Implement Tasks 1–3 (runtime field + helper signatures + both call sites + their tests) in
**one** commit so every commit is green; Task 4 (XFS) is a separate commit. The default `"root=/dev/vda"`
lives only on the `ProviderRuntime.platform_root_cmdline` field — `steps.py` never hardcodes a root device,
so there is one source of the default.

Authoritative design: [ADR-0183](../../adr/0183-provider-aware-platform-root-cmdline.md),
[spec](../../archive/design/remote-boot-root-cmdline-and-xfs.md).

## Global Constraints

- Guardrails before every commit: `just lint`, `just type` (whole tree), focused `pytest`. Full `just ci`
  before push.
- Conventional commits, `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>` trailer.
- No new migration, no schema change, no tool-surface change. Additive defaulted dataclass field only.
- `_PLATFORM_OWNED_CMDLINE_TOKENS` admission set stays unchanged.

---

## File Structure

- `src/kdive/providers/core/runtime.py` — add `platform_root_cmdline: str | None = "root=/dev/vda"` field.
- `src/kdive/services/runs/steps.py` — `system_required_cmdline(method, root_cmdline)` and
  `cmdline_for(conn, run, method, *, root_cmdline)`; keep `_PLATFORM_OWNED_CMDLINE_TOKENS`.
- `src/kdive/jobs/handlers/runs_install.py` — pass `runtime.platform_root_cmdline` to `cmdline_for`.
- `src/kdive/mcp/tools/lifecycle/runs/view.py` — pass `runtime.platform_root_cmdline` to
  `system_required_cmdline`.
- `src/kdive/providers/remote_libvirt/composition.py` — set `platform_root_cmdline=None`.
- `systems.toml` + `src/kdive/build_configs/data/kdump.config` — add the two XFS lines.
- Tests: `tests/services/` (or wherever `steps` is unit-tested), `tests/mcp/lifecycle/test_runs_tools.py`,
  `tests/providers/remote_libvirt/`, `tests/build_configs/test_seed.py`, the local-provisioning test.

---

### Task 1: Provider-aware `system_required_cmdline` / `cmdline_for`

**Files:**
- Modify: `src/kdive/services/runs/steps.py`
- Test: a unit test module for `steps` cmdline helpers (locate existing; e.g.
  `tests/mcp/lifecycle/test_runs_tools.py` already exercises `cmdline_for`, and
  `tests/providers/local_libvirt/test_provisioning.py:136-140` exercises `system_required_cmdline`).

**Interfaces:**
- Produces: `system_required_cmdline(method: CaptureMethod, root_cmdline: str | None) -> str` and
  `cmdline_for(conn, run, method, *, root_cmdline: str | None) -> str`.

- [ ] **Step 1 — failing tests** for the new signature:
  - `system_required_cmdline(KDUMP, "root=/dev/vda") == "console=ttyS0 root=/dev/vda crashkernel=256M"`
  - `system_required_cmdline(KDUMP, None) == "console=ttyS0 crashkernel=256M"`
  - `system_required_cmdline(CONSOLE, None) == "console=ttyS0"`
  - `system_required_cmdline(CONSOLE, "root=/dev/vda") == "console=ttyS0 root=/dev/vda"`
- [ ] **Step 2 — run, confirm fail** (signature mismatch / wrong output).
- [ ] **Step 3 — implement.** Split `_REQUIRED_BASE_CMDLINE` into `_REQUIRED_CONSOLE = "console=ttyS0"`
  (keep `_LOCAL_ROOT_CMDLINE = "root=/dev/vda"` as the default value the runtime field uses — define it in
  `runtime.py` or import; simplest: the default literal lives on the dataclass field). Build the required
  cmdline by joining `["console=ttyS0", root_cmdline?, crashkernel?]` dropping `None`. Keep
  `_PLATFORM_OWNED_CMDLINE_TOKENS` and `platform_owned_cmdline_token` unchanged. Update `cmdline_for` to
  take `root_cmdline` keyword and forward it.
- [ ] **Step 4 — run focused tests + `just lint` + `just type`.**
- [ ] **Step 5 — commit:** `fix(boot): make platform root= cmdline provider-aware in steps (#587)`

### Task 2: `ProviderRuntime.platform_root_cmdline` field + remote override

**Files:**
- Modify: `src/kdive/providers/core/runtime.py` (add field, default `"root=/dev/vda"`).
- Modify: `src/kdive/providers/remote_libvirt/composition.py` (set `platform_root_cmdline=None`).
- Test: `tests/providers/remote_libvirt/` (assert the assembled remote runtime exposes
  `platform_root_cmdline is None`); a local/fault-inject assertion that the default is `"root=/dev/vda"`.

**Interfaces:**
- Consumes: nothing new.
- Produces: `ProviderRuntime.platform_root_cmdline: str | None`.

- [ ] **Step 1 — failing test:** assemble the remote-libvirt runtime (use the existing remote composition
  test fixture/seam) and assert `runtime.platform_root_cmdline is None`; assert the local runtime default
  is `"root=/dev/vda"`.
- [ ] **Step 2 — run, confirm fail** (`AttributeError` / wrong value).
- [ ] **Step 3 — implement.** Add the defaulted field at the END of the `ProviderRuntime` dataclass (after
  the other defaulted fields, before/after the existing optional block — must remain a valid default
  ordering). Set `platform_root_cmdline=None` in `remote_libvirt/composition.py`'s `ProviderRuntime(...)`.
  Confirm `local_libvirt` and `fault_inject` composition sites do NOT pass it (inherit default).
- [ ] **Step 4 — guardrails.** `just type` must stay green (frozen slots dataclass).
- [ ] **Step 5 — commit:** `fix(boot): remote-libvirt runtime owns no platform root= (#587)`

### Task 3: Thread the field through the two call sites

**Files:**
- Modify: `src/kdive/jobs/handlers/runs_install.py:58` → `cmdline_for(conn, run, method,
  root_cmdline=runtime.platform_root_cmdline)`.
- Modify: `src/kdive/mcp/tools/lifecycle/runs/view.py:52-53` → `system_required_cmdline(method,
  runtime.platform_root_cmdline)`.
- Test: `tests/mcp/lifecycle/test_runs_tools.py` — extend `test_runs_get_advertises_the_system_required_cmdline`
  semantics: a local System still advertises `console=ttyS0 root=/dev/vda crashkernel=256M`; add a remote
  System case that advertises `console=ttyS0 crashkernel=256M` (no `root=`). Install-handler test: remote
  install composes a cmdline with no `root=` token; local install keeps `root=/dev/vda`.

**Interfaces:**
- Consumes: `system_required_cmdline`/`cmdline_for` new signatures (Task 1),
  `ProviderRuntime.platform_root_cmdline` (Task 2).

- [ ] **Step 1 — failing tests** for both call sites (remote advertises no `root=`; remote install cmdline
  has no `root=`).
- [ ] **Step 2 — run, confirm fail.**
- [ ] **Step 3 — implement** the two one-line call-site changes.
- [ ] **Step 4 — guardrails** + the existing advertisement/install tests stay green (local unchanged).
- [ ] **Step 5 — commit:** `fix(boot): compose remote install/advertised cmdline without root= (#587)`

### Task 4: XFS in the kdump fragment

**Files:**
- Modify: `systems.toml` (`[[build_config]]` `name = "kdump"` `content`) — add `CONFIG_XFS_FS=y` and
  `CONFIG_XFS_POSIX_ACL=y` (after `CONFIG_GDB_SCRIPTS=y`).
- Modify: `src/kdive/build_configs/data/kdump.config` — add the same two lines.
- Test: `tests/build_configs/test_seed.py` — assert the packaged fragment contains `CONFIG_XFS_FS=y`.

- [ ] **Step 1 — failing test:** extend `test_kdump_fragment_is_packaged_and_nonempty` (or a new test) to
  assert `b"CONFIG_XFS_FS=y" in data`.
- [ ] **Step 2 — run, confirm fail.**
- [ ] **Step 3 — implement:** add the two lines to both files.
- [ ] **Step 4 — guardrails.** Confirm no sha-pinned test hardcodes the old digest (the reconcile/seed
  tests compute sha from the file at runtime, so they stay green). Run
  `pytest tests/build_configs -q` and any `test_reconcile_build_configs`.
- [ ] **Step 5 — commit:** `fix(build): add XFS root support to kdump config fragment (#587)`

### Task 5: Full suite + cleanup

- [ ] Run `just ci` (full gate). Fix any architecture/doc-generation test that the changes touched.
- [ ] Confirm `_PLATFORM_OWNED_CMDLINE_TOKENS` admission tests still pass unchanged.
- [ ] Confirm `just docs-check` / generated tool reference unaffected (no tool-surface change expected).

---

## Self-Review

- Spec criteria 1–9 map to Tasks 1 (1-4), 2 (5), 3 (6-7), 4 (8), and admission unchanged (9).
- Type consistency: `root_cmdline: str | None` used identically in `system_required_cmdline`,
  `cmdline_for`, and `ProviderRuntime.platform_root_cmdline`.
- No placeholders; every task names exact files and assertions.

## Rollback

Revert the branch; the field default and unchanged admission keep local behavior identical, so reverting
is safe at any task boundary.
