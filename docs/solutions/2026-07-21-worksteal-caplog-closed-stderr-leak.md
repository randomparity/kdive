---
title: pytest caplog silently drops a WARNING because an earlier test left a StreamHandler bound to a closed capture buffer (exposed by --dist worksteal)
date: 2026-07-21
tags: [test-isolation, logging, pytest, xdist, flaky-test]
components: [tests/conftest.py, tests/test_main_version.py, tests/mcp/jobs/test_jobs_tools.py, src/kdive/log.py]
---

## Problem

After switching the `just test` recipe from the default xdist scheduler to
`-n auto --dist worksteal` (issue #1332, PR#1394), CI's `lint · type · test` job
failed two tests that had always passed:

- `tests/mcp/jobs/test_jobs_tools.py:548` — `test_wait_job_degrades_invariant_violating_terminal_row`
- `tests/mcp/jobs/test_jobs_tools.py:692` — sibling degrade test

Each forces a job row into an invariant-violating state
(`_mark_failed_without_category` → `state='failed', error_category=NULL`), calls the
tool, and asserts the response was degraded AND that the degrade was logged:

```python
assert resp.error_category == "infrastructure_failure"   # PASSED
assert any(
    record.exc_info is not None and f"job {job_id}" in record.message
    for record in caplog.records                          # assert False
)
```

The degrade response was correct (the earlier asserts passed) and the WARNING
*was* emitted (`kdive.mcp.tools.jobs` logs it at `src/kdive/mcp/tools/jobs.py:128`,
visible in CI captured stderr), but `caplog.records` was empty for it. It passed
locally (`just test` twice, 9059 passed) and failed only in CI — an order/timing
signature. The CI log also carried, from an unrelated point in the run:
`ValueError: I/O operation on closed file` inside `StreamHandler.emit`.

## Root cause

`tests/test_main_version.py` invokes the CLI entrypoint `main([...])` (e.g.
`build-fs`, `reconciler`). `main` runs `bootstrap_stdout_floor`, which installs a
`_KdiveHandler` (a `StreamHandler`) on the **root** logger. At import/test time
`sys.stderr` is pytest's per-test capture buffer, so the handler is bound to *that*
buffer. Nothing removes the handler when the test ends, and pytest then **closes**
the capture buffer.

On a later test that runs on the same xdist worker, any log record propagating to
the root logger hits the leaked handler, whose `.emit()` writes to the now-closed
buffer and raises `ValueError: I/O operation on closed file` inside
`logging.Logger.callHandlers`. The logging machinery swallows that handler error,
but the record never reaches pytest's own `LogCaptureHandler` — so `caplog.records`
comes up empty for the `kdive.mcp.tools.jobs` WARNING, and the assertion fails.

Why worksteal exposed it: xdist runs tests **serially within a worker** — worksteal
adds no concurrency, it changes *order*. `--dist load`'s fixed round-robin happened
to keep the leaking `test_main_version.py` tests off the same workers as the jobs
degrade tests; worksteal's dynamic stealing put them together. The latent leak had
been there all along, masked by scheduler ordering.

## Solution

Add an autouse fixture that snapshots and restores mutable global logging state
around every test, so no test can leak a root handler (or level / disable floor)
into a later one regardless of ordering. In `tests/conftest.py`:

```python
@pytest.fixture(autouse=True)
def restore_root_logging():
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_disable = logging.root.manager.disable
    try:
        yield
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        logging.disable(saved_disable)
```

This is a test-isolation fix only; the product code path is correct (a real process
runs `bootstrap_stdout_floor` once and keeps the handler for its lifetime).

Verified: the deterministic single-process repro
`uv run python -m pytest tests/test_main_version.py \
  tests/mcp/jobs/test_jobs_tools.py::test_wait_job_degrades_invariant_violating_terminal_row \
  -n0` fails before the fixture and passes after; full `just test` green twice
(9059 passed, 14 skipped) under `--dist worksteal`; CI all checks SUCCESS
(PR#1394, ADR-0418, merged as commit 68df98886).

## Prevention

- The `restore_root_logging` autouse fixture (above) is itself the guard — it
  neutralizes this whole class of "a test leaked global logging state" flake.
- Documented convention: tests that call an entrypoint which configures logging
  (`main`, `configure_logging`, `bootstrap_stdout_floor`) must not rely on manual
  handler cleanup; the autouse fixture covers them.
- When changing an xdist `--dist` scheduler, expect order-dependent, CI-only
  flakes: a scheduler change reorders tests and surfaces every latent cross-test
  state leak. Grep CI logs for `I/O operation on closed file`, not just the failing
  assertion — the real error is often a swallowed handler exception elsewhere in the
  run.
