# Plan — Create-time build-host ↔ kernel-source compatibility check (#534)

Derived from [spec](../../specs/2026-06-17-create-time-build-host-source-check.md) and
[ADR-0157](../../adr/0157-create-time-build-host-source-check.md). Three tightly
coupled tasks on two source files (`services/runs/build_host_selection.py`,
`mcp/tools/lifecycle/runs/create.py`) plus their tests; executed directly in this
session, not by parallel subagents (the tasks share files and a test module — parallel
mutation would conflict).

Guardrails for every commit (from `AGENTS.md` / `justfile`):
`just lint` (ruff check + format-check) · `just type` (ty, **whole tree**) · the
touched tests via `uv run python -m pytest <path>::<name> -q`. Full `just ci` before
the first push. The DB-backed create/build tests need a reachable Docker daemon
(disposable Postgres via testcontainers); they are run here against the local Docker
that is already up. Conventional-commit subjects ≤72 chars + the
`Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` trailer.

Project conventions that bind every task (from `AGENTS.md`):
- Return the project's `ToolResponse` envelope with the most specific `ErrorCategory`;
  never invent error strings. The two compatibility messages already exist verbatim in
  `build_host_selection.py:79-88` and MUST be reused byte-for-byte.
- `ty` is whole-tree; keyword-only helper params are fine. Ruff line length 100,
  lint set `E,F,I,UP,B,SIM`. Absolute imports only.
- Tests mirror the package tree under `tests/`; drive handlers directly (no transport).

## Task A — extract the shared compatibility helper (refactor, no behavior change)

**Where it fits:** the single-source seam the spec's "Constraint" section requires and
the coordination ask for #532/#536. Extract the matrix that lives inline in
`resolve_and_admit` into one pure function both call sites invoke.

**Files:**
- `tests/services/test_build_host_selection.py` (new unit tests for the helper)
- `src/kdive/services/runs/build_host_selection.py` (add helper; call it from
  `resolve_and_admit`)

**TDD steps:**
1. Add unit tests driving the helper directly (no DB, no async needed — it is a pure
   sync function):
   - `test_compat_local_with_git_raises_config_error`: `host_kind=LOCAL, is_git=True`
     → `CategorizedError`, `category is CONFIGURATION_ERROR`, message ==
     `"a local build host requires a warm-tree kernel_source_ref, not a git ref"`,
     `details == {"build_host": "<name>", "host_kind": "local"}`.
   - `test_compat_remote_with_warm_tree_raises_config_error`: `host_kind=SSH,
     is_git=False` → message == `"a remote build host requires a git
     kernel_source_ref"`, `details == {"build_host": "<name>", "host_kind": "ssh"}`.
   - `test_compat_ephemeral_with_warm_tree_raises`: `host_kind=EPHEMERAL_LIBVIRT,
     is_git=False` → same remote message, `host_kind="ephemeral_libvirt"`.
   - `test_compat_local_with_warm_tree_ok` and `test_compat_remote_with_git_ok`: return
     `None`, raise nothing.
   Run → fails (helper does not exist).
2. Add `check_source_kind_compatibility(*, host_kind: BuildHostKind, is_git: bool,
   build_host: str) -> None` to `build_host_selection.py`. Move the two `raise
   CategorizedError(...)` blocks (lines 78-89) into it **verbatim** — same messages,
   same `category`, same `details` dict (keyed `build_host`, `host_kind`). Google-style
   docstring naming the matrix (LOCAL→warm-tree, SSH/EPHEMERAL_LIBVIRT→git).
3. In `resolve_and_admit`, replace the inline `if host.kind ...` block with:
   `check_source_kind_compatibility(host_kind=host.kind, is_git=git,
   build_host=name)`. The local `git = is_git_source(parsed_profile)` and `name`
   stay as-is.
4. Run the new unit tests → pass. Run the existing
   `tests/services/test_build_host_selection.py` (all four existing cases) → still
   green — `resolve_and_admit`'s observable behavior is unchanged.

**Acceptance check:** the matrix + its two messages exist once
(`check_source_kind_compatibility`); `resolve_and_admit` calls it; all existing
build-host-selection tests pass unchanged.

**Rollback:** inline the two raises back into `resolve_and_admit` and delete the
helper + its tests (additive).

## Task B — call the helper at `runs.create`

**Where it fits:** the create-time enforcement that is the point of #534. After the
preconditions/assertion/investigation-state checks and before `_insert_run`, resolve
the host and run the compatibility check.

**Files:**
- `tests/mcp/lifecycle/test_runs_tools.py` (new create-time tests; one host-seed helper)
- `src/kdive/mcp/tools/lifecycle/runs/create.py` (call site in `_create_locked`)

**TDD steps:**
1. Add DB-backed create tests (mirror the existing `_create` / `_seed_investigation` /
   `_seed_system` / `_insert_ssh_host` helpers already in the module; reuse `_GIT_BUILD`
   and `_VALID_BUILD`). Use a local `_create_with_host(pool, ctx, inv, sys, profile)`
   wrapper or pass `profile=` to the existing `_create`. Assert on `resp.status`,
   `resp.error_category`, and the run count
   (`SELECT count(*) FROM runs WHERE system_id=%s`):
   - `test_create_remote_host_with_warm_tree_is_config_error_no_run`: seed an ssh host,
     profile `{**_VALID_BUILD, "build_host": "<ssh>"}` (warm-tree) → `error`,
     `configuration_error`; **zero** runs on the System.
   - `test_create_local_host_with_git_ref_is_config_error_no_run`: profile
     `_GIT_BUILD` with no `build_host` (→ worker-local, kind local) → `error`,
     `configuration_error`; zero runs.
   - `test_create_remote_host_with_git_ref_succeeds`: ssh host + `_GIT_BUILD` naming it
     → `created`; one run.
   - `test_create_local_host_with_warm_tree_succeeds`: `_VALID_BUILD`, no build_host →
     `created` (this is the existing happy path; assert it stays green).
   - `test_create_absent_named_host_still_creates`: profile names a host that is not
     seeded → `created` (host existence is build-time).
   - `test_create_external_profile_skips_compat_check`: a `source="external"` profile →
     `created` (no kernel_source_ref; the check is skipped). Confirm the external
     profile parses through `BuildProfile.parse` first.
   - `test_create_live_run_precedes_compat_check`: a System that already has a
     non-terminal run + an incompatible (ssh+warm-tree) profile → `transport_conflict`
     (the live-run block), **not** `configuration_error`. Pins the spec's
     precondition-order acceptance criterion.
   Run → the rejection/ordering tests fail (create accepts today); the happy-path and
   external tests pass already.
2. In `create.py`, add a helper run inside `_create_locked` **after** the
   investigation-state check (step (c) in the spec) and **before** `_insert_run` (step
   (d)). It:
   - `isinstance(build_profile, ServerBuildProfile)` guard — return `None` (proceed)
     for the external lane.
   - `name = build_profile.build_host or "worker-local"`; `host = await
     get_by_name(conn, name)`; if `host is None`, return `None` (absent host is
     build-time — proceed to insert).
   - else `try: check_source_kind_compatibility(host_kind=host.kind,
     is_git=is_git_source(build_profile), build_host=name) except CategorizedError as
     exc: return ToolResponse.failure_from_error(str(targets.system_id), exc)`.
   Wire it: `compat = await _compat_block_response(conn, build_profile, targets);
   if compat is not None: return compat` placed between the investigation-state block
   and `run = await _insert_run(...)`.
3. Add imports to `create.py`: `from kdive.db.build_hosts import get_by_name`,
   `from kdive.profiles.build import ServerBuildProfile, is_git_source` (extend the
   existing `kdive.profiles.build` import), and
   `from kdive.services.runs.build_host_selection import
   check_source_kind_compatibility`. (`BuildHostKind` is not needed at the call site —
   the helper takes `host.kind` opaquely.)
4. Run the new create tests → pass. Run the full `tests/mcp/lifecycle/test_runs_tools.py`
   module → green (existing create + build tests unaffected).

**Acceptance check:** every spec acceptance bullet has a passing test; an incompatible
pair inserts no run; the live-run block still wins; external + absent-host + valid
combos create.

**Rollback:** delete `_compat_block_response`, its call, and the three imports; the
tests are additive.

## Task C — backstop regression test + full suite + ship

**Where it fits:** prove the build-time check still rejects when the host row mutates
between create and build (the defense-in-depth claim), then run the full gate.

**Files:**
- `tests/mcp/lifecycle/test_runs_tools.py` (one backstop test)

**TDD steps:**
1. `test_build_backstop_rejects_when_host_kind_flips_after_create`: seed an ssh host;
   `runs.create` with `_GIT_BUILD` naming it → `created` (valid at create). Then
   `UPDATE build_hosts SET kind='local' WHERE name=%s`. Then `runs.build` the run →
   `configuration_error` (the local-with-git message), no build job enqueued
   (`SELECT count(*) FROM jobs WHERE kind='build'` == 0). This exercises the
   create-valid → build-invalid path the backstop exists for. Run → passes (the
   build-time check is retained by Task A).
2. Full `just lint` · `just type` · `just test` → all green. (`just test` excludes the
   gated `live_vm` marker; the DB tests run against local Docker.)
3. `just ci` (lint, type, lint-shell, lint-workflows, check-mermaid, test) → green
   before the first push.

**Acceptance check:** the backstop test is green; full `just ci` green locally.

**Rollback:** the test is additive; nothing else changes in Task C.

## Branch review + ship

1. Adversarial-review the branch (`/challenge --base main`) → address every defensible
   finding, commit each fix, re-run to `approve` (≤5 iterations).
2. Push; open the PR (`gh pr create`) with a plain-language body ending `Closes #534`.
3. Run `/security-review` over the branch diff; fix defensible findings.
4. Drive to required-CI-green **and** `mergeStateStatus=CLEAN`/`mergeable=MERGEABLE`.
   Do **not** merge.

## Commit sequence (small, bisectable)

1. `refactor(build): extract check_source_kind_compatibility helper` — Task A (helper +
   its unit tests + `resolve_and_admit` call site; no behavior change).
2. `feat(runs): reject incompatible build-host/source at runs.create` — Task B (create
   call site + create-time tests).
3. `test(runs): pin build-time backstop after a host-kind flip` — Task C step 1.

(The spec/ADR commits already landed earlier on the branch: 561dd8ba, plus the spec
review-fix commit.)

## Verification gaps / risks called out

- **Docker dependency:** the create/build tests are DB-backed (testcontainers
  Postgres). They are exercised locally against the running Docker daemon; CI sets
  `KDIVE_REQUIRE_DOCKER=1` so a missing daemon hard-fails rather than silently skips.
- **`_create_locked` complexity budget:** the function is already near the
  cyclomatic-complexity guard. The compatibility logic goes in a **separate**
  `_compat_block_response` helper (like `_assertion_block_response`) rather than
  inline, to stay under the ≤8 complexity / ≤100-line limits and match the existing
  block-response pattern.
- **`get_by_name` under the lock:** the call adds one read on the connection already
  held inside the transaction under the SYSTEM lock. It is read-only and does not
  acquire the BUILD_HOST lock (no lease at create) — no new lock-order edge.
