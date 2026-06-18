# Plan — Diagnostics `local_kernel_src` check (#533, #532)

- **Spec:** [`../../specs/2026-06-17-diagnostics-local-kernel-src.md`](../../specs/2026-06-17-diagnostics-local-kernel-src.md)
- **ADR:** [`../../adr/0163-diagnostics-local-kernel-src-check.md`](../../adr/0163-diagnostics-local-kernel-src-check.md)
- **Date:** 2026-06-17

The change is tightly coupled (one `diagnostics` package; check + probe + factory wiring must
land together for the tests to pass) and small, so it is implemented directly in one session
with TDD, not handed to independent subagents. Guardrails per commit: `just lint`, `just type`,
and the focused `tests/diagnostics/` run; the full `just ci` before the first push.

## Conventions (apply to every task)

- Ruff line length 100; lint set `E,F,I,UP,B,SIM`; `ty` strict (whole tree). Absolute imports
  only. Google-style docstrings on the new public seams. `from __future__ import annotations`.
- `checks.py` stays free of any provider/transport import — the provider-owned predicate import
  lives only in the new `diagnostics/kernel_src.py` adapter (`diagnostics → providers` is the
  only legal direction).
- Detail/fix strings in `checks.py` are the test-asserted literals; the spec/ADR wording is the
  source of truth (PASS = "points at an existing absolute tree", source-path usability only).
- Doc-style guard: plain factual prose, no "critical/robust/comprehensive/elegant".

## Task 1 — `WarmTreeSourceOutcome` + `LocalKernelSrcCheck` in `checks.py`

**Where it fits:** the policy half of the check, next to the other `Check`s, mirroring
`BaseImageStagingCheck`. No filesystem/config dependency — driven by an injected async probe.

**Files:** `src/kdive/diagnostics/checks.py`, `tests/diagnostics/test_local_kernel_src.py`.

**TDD:**
1. Failing unit tests (probe injected, no config/FS): construct `LocalKernelSrcCheck` with a
   fake probe returning each `WarmTreeSourceOutcome`; assert
   - `id == "local_kernel_src"` (a new `LOCAL_KERNEL_SRC_ID` constant), `vantage == Vantage.SERVER`;
   - `USABLE` → `status=pass`, `fix is None`, `failure_category is None`, `provider is None`,
     detail contains "points at an existing absolute tree";
   - `UNSET` → `status=fail`, `failure_category == "configuration_error"`, `fix == LOCAL_KERNEL_SRC_FIX`,
     detail mentions `KDIVE_KERNEL_SRC` is unset;
   - `INVALID` → `status=fail`, `failure_category == "configuration_error"`, `fix == LOCAL_KERNEL_SRC_FIX`,
     detail mentions not an absolute existing tree;
   - both `fail` fixes are the same `LOCAL_KERNEL_SRC_FIX` literal (one remediation, two cases);
   - `CheckResult.__post_init__` invariants hold (no fix on pass, fix present on fail) — implied
     by construction succeeding.
2. Implement: add `LOCAL_KERNEL_SRC_ID`, `LOCAL_KERNEL_SRC_FIX` (names the two build lanes:
   stage a tree + set `KDIVE_KERNEL_SRC`, or register a git build host — an independent literal,
   not imported from `workspace.py`), `WarmTreeSourceOutcome(StrEnum)` (`USABLE`/`UNSET`/`INVALID`),
   `WarmTreeSourceProbe = Callable[[], Awaitable[WarmTreeSourceOutcome]]`, and `LocalKernelSrcCheck`.
   Reuse the module-level `_CONFIGURATION_ERROR` label already in `checks.py`.

**Acceptance:** the unit tests pass; `checks.py` imports nothing from `providers`.

## Task 2 — `diagnostics/kernel_src.py` probe adapter

**Where it fits:** the IO/predicate-boundary half, mirroring `reachability.py` / `base_image_staging.py`.

**Files:** `src/kdive/diagnostics/kernel_src.py`, same test file.

**TDD:**
1. Failing tests (inject `source`): `warm_tree_source_probe(source=lambda: <v>)` returns
   - `UNSET` for `""`, `"   "`;
   - `USABLE` for `str(tmp_path)` (an existing absolute dir);
   - `INVALID` for a relative path (`"linux"`), a non-existent absolute path
     (`"/nonexistent/kdive-xyz"`), and a file (a `tmp_path/file` that is not a dir).
   - One test exercising the **default** `_kernel_src_from_config` source via
     `monkeypatch.setenv("KDIVE_KERNEL_SRC", str(tmp_path))` + `config.load()` → `USABLE`, and
     unset (`monkeypatch.delenv`, `config.load()`) → `UNSET`, proving the config read and that
     resolution is deferred to probe time (resolve after assembly).
2. Implement: `_kernel_src_from_config() -> str` returns `config.get(KERNEL_SRC) or ""`;
   `warm_tree_source_probe(*, source=_kernel_src_from_config)` builds an async `probe()` that
   calls `warm_tree_source_error(source())` and maps `None`→`USABLE`,
   `== KERNEL_SRC_UNSET_DETAIL`→`UNSET`, else→`INVALID`. Import `KERNEL_SRC` from
   `kdive.config.core_settings`, `config` as `kdive.config`, and `warm_tree_source_error` +
   `KERNEL_SRC_UNSET_DETAIL` from `kdive.providers.shared.build_host.workspace`.

**Acceptance:** probe tests pass; the unset/invalid split derives from the predicate's own
return values (no re-implementation of the rule).

## Task 3 — wire into `default_service_factory`

**Where it fits:** assembly. The check is always assembled (seeded `worker-local` invariant).

**Files:** `src/kdive/diagnostics/service.py`, `tests/diagnostics/test_default_factory.py`.

**TDD:**
1. Update the two existing assertions that pin the assembled set to `{SECRET_REF_ID}`
   (`test_factory_omits_remote_checks_when_not_configured`,
   `test_multiple_instances_are_not_configured_so_no_reachability_check`) to
   `{SECRET_REF_ID, LOCAL_KERNEL_SRC_ID}`. Add:
   - `local_kernel_src` is always in the assembled set (remote configured or not);
   - a run with `KDIVE_KERNEL_SRC` unset → `local_kernel_src` `fail`, `report.has_failure` True;
   - a run with `KDIVE_KERNEL_SRC` = a `tmp_path` tree → `local_kernel_src` `pass`.
   Use the existing `_set_env`/`config.load()` helpers; set/unset `KDIVE_KERNEL_SRC` alongside.
2. Implement: `import kdive.diagnostics.kernel_src as kernel_src`; add
   `def _build_host_checks() -> list[Check]: return [LocalKernelSrcCheck(probe=kernel_src.warm_tree_source_probe())]`;
   in `default_service_factory`, `checks.extend(_build_host_checks())` after `_secret_ref_check()`
   and before the `is_remote_libvirt_configured()` block. Import `LocalKernelSrcCheck` from `checks`.

**Acceptance:** updated + new default-factory tests pass; `secret_ref` and the remote checks are
unchanged in behavior.

## Task 4 — full guardrails + cleanup

Run `just lint`, `just type`, `just test` (the non-live suite), then the full `just ci` before
pushing. Fix every warning. Confirm no other test asserted the old `{secret_ref}`-only set
(grep `tests/` for `SECRET_REF_ID}` / `== {"secret_ref"}`). No migration/DDL/MCP/config/
generated-doc change is introduced (verify `git diff --stat` touches only `diagnostics/`,
`tests/diagnostics/`, and the already-committed docs).

## Rollback / cleanup

Pure addition behind no flag; rollback is reverting the commits. The only behavioral change to
existing surfaces is the always-on check (and its possible `fail`), which the ADR's Consequences
trace. No state, schema, or external-service change to unwind.

## Verification gaps acknowledged

- The check reads the server process's `KDIVE_KERNEL_SRC` (shared-env assumption — ADR-0163
  Considered & rejected); not falsified by unit tests, which inject the value directly.
- PASS asserts source-path usability, not a buildable kernel tree (bounded predicate).
