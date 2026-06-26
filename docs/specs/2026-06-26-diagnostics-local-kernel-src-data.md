# Spec — Expose the warm-tree source (path + git HEAD) via `local_kernel_src` data (#845)

- **Issue:** [#845](https://github.com/randomparity/kdive/issues/845)
- **Builds on:** [ADR-0163](../adr/0163-diagnostics-local-kernel-src-check.md) (the
  server-vantage `local_kernel_src` check this extends) and its
  [spec](2026-06-17-diagnostics-local-kernel-src.md); [ADR-0161](../adr/0161-local-warm-tree-build-admission.md)
  (the `warm_tree_source_error` predicate the probe already resolves over).
- **No new ADR.** This is additive fielded data on an existing check; the seam-type
  widening and the server-vantage / USABLE-only scope are already decided by ADR-0163
  ("Considered & rejected": the worker-vantage refinement stays deferred). The scope
  decisions are recorded here.
- **Date:** 2026-06-26

## Problem

When an agent chooses the warm-tree/server build lane, it cannot discover **what the
worker's `KDIVE_KERNEL_SRC` points at** before building — neither the resolved path nor
the git HEAD commit/branch. It learns the resolved commit only *after* a build via
`build_provenance.resolved_commit` (`runs.get`). `ops.diagnostics`'s `local_kernel_src`
check already resolves and stats the path, but collapses the result to a three-state
verdict with static literal `detail` strings — it never surfaces the resolved path or git
HEAD. This forces guesswork and is a recurring pre-build stall.

## Decision

Extend the existing server-vantage `local_kernel_src` check to carry structured
`CheckResult.data` fields **alongside** the unchanged three-state verdict:

| Field | When present | Source |
|---|---|---|
| `vantage` = `"server"` | always (on the `USABLE` PASS) | the check's vantage, machine-readable |
| `resolved_path` | `USABLE` only | the resolved absolute `KDIVE_KERNEL_SRC` value |
| `head_commit` | `USABLE` **and** the tree is a git checkout | `git -C <path> rev-parse HEAD`, the 12-char prefix |
| `branch` | `USABLE` **and** on a named branch | `git -C <path> rev-parse --abbrev-ref HEAD` |

The disclosure uses the **fielded-data path** (`CheckResult.data`), not the
credential-careful `detail` string — the existing static `detail` strings are unchanged.
The data is labelled `vantage="server"`: it is the server process's `KDIVE_KERNEL_SRC`,
authoritative only for the shared-env (single-host / compose) deployment, not for a
split-worker deployment — the same caveat ADR-0163 records on the verdict.

### Scope: `USABLE` only

`data` is emitted only on the `USABLE` (PASS) outcome. `UNSET` has no path to disclose;
`INVALID` means the value is set but is not an existing absolute tree, so there is no
*resolved* path — disclosing the bad value is a separate, deferrable concern and is left
out to keep this change to the discoverability win the issue names.

### Layering — the git read lives in the probe

Per ADR-0163's seam, `checks.py` holds the three-state policy and stays free of the
filesystem and config; the probe adapter (`diagnostics/kernel_src.py`) is the build-host
boundary that resolves the path and now also reads git HEAD. The probe's injected seam
widens from `async () -> WarmTreeSourceOutcome` to `async () ->
WarmTreeSourceProbeResult` (a frozen dataclass: `outcome` plus optional
`resolved_path` / `head_commit` / `branch`). The check maps the result to a `CheckResult`
with `data`; it remains unit-testable by injecting a `WarmTreeSourceProbeResult` with no
filesystem or git access.

The git read reuses `_rev_parse_head` from
`providers/shared/build_host/dispatch.py` (the same best-effort `git rev-parse HEAD`
helper the post-build provenance path uses) via a function-local import inside the probe
adapter — `diagnostics → providers` is the only legal import direction, and the
function-local import keeps the adapter's top-level import graph narrow (mirroring how
`service.py` imports `buildhost_agent` function-locally). The branch is read by a sibling
best-effort helper in the adapter (`git rev-parse --abbrev-ref HEAD`). Both reads are
best-effort: any failure (not a git tree, git absent, detached HEAD) yields `None` for
that field and never changes the verdict.

### The git read must never compromise the verdict (budget + event loop)

The check's `run()` is bounded by `run_check`'s `asyncio.timeout` (the default per-check
budget is 10s; `checks.py:172`). `_rev_parse_head` is a **blocking** `subprocess.run`
with a 30s internal timeout — longer than the per-check budget, and a blocking sync call
inside an async coroutine is not cancellable by `asyncio.timeout` (it only fires at an
await point). Run naively, a slow/hung git read would block the event loop and push the
check past its budget, turning a healthy `USABLE` into an `ERROR` — a regression of
ADR-0163's "no error branch" invariant for this check, where a nice-to-have disclosure
would compromise the must-have verdict.

So the probe:

- runs the whole git read (commit + branch) inside `asyncio.to_thread` so the event loop
  is never blocked — the `SecretRefCheck` precedent (`checks.py:265`,
  `await asyncio.to_thread(self._resolve, ref)`); and
- bounds that offloaded read with its **own** `asyncio.timeout`
  (`_GIT_READ_TIMEOUT`, well under the 10s per-check budget) so on a hang it yields
  `None` git fields and the verdict stays `USABLE`/PASS. Any exception is likewise
  swallowed to `None` fields. The git disclosure can never produce `ERROR`, never block
  the loop, and never change the three-state verdict.

### MCP surface

`ops.diagnostics`'s per-check item gains a `data` object carrying the check's structured
fields (`{}` when a check emits none), surfaced through the existing generic
`CheckResult`-to-item mapping. No new tool, parameter, config setting, migration, or DDL.

## Components

1. **`checks.py`** — add `CheckResult.data: Mapping[str, str] | None = None` (legal on any
   status, like `resource_id`); define `WarmTreeSourceProbeResult` (frozen dataclass) and
   retype `WarmTreeSourceProbe`; in `LocalKernelSrcCheck.run()` build `data` on the
   `USABLE` branch.
2. **`kernel_src.py`** — the probe returns `WarmTreeSourceProbeResult`; on `USABLE` it
   reads the git HEAD short-commit (reused `_rev_parse_head`) and branch. The git read is
   injectable (`git_head` parameter, default the real reader) so tests need no real
   checkout.
3. **`mcp/tools/ops/diagnostics.py`** — `_item()` surfaces `result.data`.

## Test plan (TDD)

`tests/diagnostics/test_local_kernel_src.py` (update for the new probe-result type):

- **check logic** (probe result injected, no filesystem/git):
  - `USABLE` with path + commit + branch → PASS, `data` carries
    `vantage="server"`, `resolved_path`, `head_commit`, `branch`.
  - `USABLE` with path but no git (commit/branch `None`) → PASS, `data` carries
    `vantage`/`resolved_path` and omits `head_commit`/`branch`.
  - `UNSET` / `INVALID` → unchanged FAIL, `data` is `None` (no path disclosed).
  - existing verdict/fix/vantage/disclosure assertions stay green (detail unchanged).
- **probe adapter** (`source` + `git_head` injected): classification cases unchanged but
  now assert `.outcome`; `USABLE` carries `resolved_path` = the configured value and the
  injected git fields; a non-git tree (`git_head` returns `(None, None)`) carries no
  commit/branch.
- **git reader** (`_git_head`, `subprocess.run` patched — no real checkout): a git tree →
  `(short_commit, branch)` with `short_commit` the 12-char prefix; a non-git tree
  (rev-parse fails) → `(None, None)`; detached HEAD (`--abbrev-ref` returns `HEAD`) →
  commit set, branch `None`.
- **verdict survives a git hang** (probe-level): a `git_head` that blocks past
  `_GIT_READ_TIMEOUT` still yields a `USABLE` result with `resolved_path` set and
  `head_commit`/`branch` `None` — never `ERROR`, never an exceeded budget.

`CheckResult.data` round-trips through the MCP item: a focused test that
`_item()` includes the `data` object.

## Non-goals

- A worker-authoritative build-source preflight that dispatches a worker job to rev-parse
  the worker's effective `KDIVE_KERNEL_SRC` (the worker-vantage refinement deferred in
  ADR-0163 / #514 — needs the provider-neutral worker-job path it called out as missing).
- Disclosing the configured-but-invalid value on the `INVALID` outcome.
- Any change to the unchanged `detail` strings, the verdict mapping, or the `UNSET`/
  `INVALID` branches.
- Serializing `data` through `result_codec.py` (that codec carries only the two
  worker-vantage checks, neither of which emits `data`).
