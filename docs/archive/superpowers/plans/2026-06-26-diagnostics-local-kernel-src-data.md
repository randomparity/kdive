# Plan — `local_kernel_src` warm-tree source data (#845)

- **Spec:** [`../../specs/2026-06-26-diagnostics-local-kernel-src-data.md`](../../specs/2026-06-26-diagnostics-local-kernel-src-data.md)
- **Issue:** #845
- **Execution:** direct, single session — the change is one tightly-coupled feature
  across three files in the diagnostics subsystem plus its tests; no independent
  parallelizable tasks.
- **Guardrails (run before every commit):** `just lint`, `just type`,
  `just test`. Before push: full `pytest` + the doc gates (`just docs-links`,
  `just adr-status-check`, `just docs-check`). Commit trailer:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

The steps are ordered so each commit is green. TDD throughout: failing test first,
minimal code, refocus.

**Commit grouping (so every commit type-checks):** Tasks 2 and 3 land in **one commit** —
retyping `WarmTreeSourceProbe` in `checks.py` (Task 2) without simultaneously updating
`kernel_src.py`'s `warm_tree_source_probe` to return the new type (Task 3) would leave an
intermediate `just type` failure (the probe's declared return type would not match its
body). Commit order: (1) `CheckResult.data`; (2) probe seam + check mapping + probe git
read; (3) MCP surface.

## Task 1 — `CheckResult.data` field (`checks.py`)

Add `data: Mapping[str, str] | None = None` to the frozen `CheckResult` dataclass
(import `Mapping` from `collections.abc`). Document it (legal on any status, like
`resource_id`; the structured machine-readable companion to `detail`). No new
`__post_init__` invariant.

- **Test (`tests/diagnostics/test_checks.py` or the local-kernel-src file):** a
  `CheckResult` carrying `data` round-trips the field; default is `None`.
- **Acceptance:** field exists, defaults `None`, existing `CheckResult` constructions
  unaffected.

## Task 2 — probe-result type + check mapping (`checks.py`)

Define `WarmTreeSourceProbeResult` (frozen, slots dataclass): `outcome:
WarmTreeSourceOutcome`, `resolved_path: str | None = None`, `head_commit: str | None =
None`, `branch: str | None = None`. Retype `WarmTreeSourceProbe = Callable[[],
Awaitable[WarmTreeSourceProbeResult]]`. In `LocalKernelSrcCheck.run()`, on the `USABLE`
branch build `data` = `{"vantage": "server", "resolved_path": <path>}` plus
`head_commit`/`branch` when present, and pass it to the PASS `CheckResult`. `UNSET`/
`INVALID` branches unchanged (no `data`). The enabled-gate n/a PASS unchanged.

- **Tests (`test_local_kernel_src.py`):** update `_probe` helper to return a
  `WarmTreeSourceProbeResult`. `USABLE` + full git → PASS with all four data fields;
  `USABLE` + no git → PASS with `vantage`/`resolved_path` only; `UNSET`/`INVALID` →
  unchanged FAIL with `data is None`; existing detail/fix/vantage/disclosure assertions
  stay green.
- **Acceptance:** check stays filesystem/git-free; verdict mapping unchanged; data only
  on USABLE.

## Task 3 — probe adapter git read (`kernel_src.py`)

- `_rev_parse_branch(tree) -> str | None`: `git rev-parse --abbrev-ref HEAD`,
  best-effort (modeled on `_rev_parse_head`); map `""`/`"HEAD"` (detached) → `None`.
- `_git_head(tree) -> tuple[str | None, str | None]`: function-local-import
  `_rev_parse_head` from `providers/shared/build_host/dispatch.py`; full SHA → 12-char
  prefix; only read the branch when a commit was found; non-git → `(None, None)`.
- `warm_tree_source_probe(*, source=..., git_head=_git_head, git_timeout=_GIT_READ_TIMEOUT)`
  returns `WarmTreeSourceProbeResult`. On `USABLE`: run the git read via
  `asyncio.to_thread(git_head, path)` inside `async with asyncio.timeout(git_timeout)`;
  on `TimeoutError`/any exception → `(None, None)`. `resolved_path` = the configured
  value. `UNSET`/`INVALID` → result with only `outcome`.
- `_GIT_READ_TIMEOUT` module constant, well under the 10s per-check budget (e.g. `5.0`).

- **Tests (`test_local_kernel_src.py`):** classification cases assert `.outcome` and,
  for `USABLE`, `resolved_path` = configured value + injected git fields; `_git_head`
  with `subprocess.run` patched → 12-char commit + branch / `(None, None)` / detached →
  branch `None`; a `git_head` that blocks past a tiny injected `git_timeout` → `USABLE`,
  git fields `None` (verdict survives the hang).
- **Acceptance:** event loop never blocked; verdict never `ERROR` from the git read;
  no real checkout needed.

## Task 4 — MCP surface (`mcp/tools/ops/diagnostics.py`)

In `_item()` add `"data": dict(result.data) if result.data else {}` to the per-check
output dict.

- **Test (`tests/mcp/.../test_diagnostics*.py`):** an item built from a `CheckResult`
  with `data` surfaces it; an item with no `data` carries `{}`.
- **Acceptance:** generic mapping; no new tool/param; `just docs-check` stays green
  (the ToolResponse schema is unchanged — `data` is already `dict[str, JsonValue]`).

## Rollback / cleanup

Pure additive; no migration, no persisted state. Revert is the branch revert. If
`docs-check`/`config-docs-check` flag a generated-doc drift, run `just docs` /
`just config-docs` and review.
