# Plan: `runs.validate_profile` (#839)

Derived from `docs/specs/2026-06-26-runs-validate-profile-839.md` and
[ADR-0259](../../adr/0259-runs-validate-profile.md). Execution mode: **direct, single
session** — the work is one cohesive tool (one handler module, one registrar wiring, one test
module) whose parts are tightly coupled, so subagent fan-out adds coordination cost without
parallelism benefit. TDD throughout (`superpowers:test-driven-development`): failing test →
confirm red for the right reason → minimal implementation → green → refactor green.

## Repo conventions (apply to every task)

- Python 3.14, `uv`. Absolute imports only (`kdive.…`), no relative `..` imports.
- ≤100 lines/function, complexity ≤8, ≤5 positional params, 100-char lines.
- Google-style docstrings on non-trivial public APIs; cite the ADR(s) implemented.
- Pick the most specific existing `ErrorCategory`; never invent strings.
- Tests mirror the package tree under `tests/`; test behavior + edge/error paths.
- Guardrails before every commit (CI runs these recipes **individually**, so each gates the PR):
  `just lint` · `just type` (whole tree) · `just test` · `just docs-check`. Doc tasks also
  run `just adr-status-check` · `just docs-links`. Regenerate the tool reference with
  `just docs` after adding the tool.
- Conventional commits, imperative ≤72-char subject, `Co-Authored-By: Claude Opus 4.8 (1M
  context) <noreply@anthropic.com>` trailer.

## Task 1 — Handler module `validate_profile.py`

**Where it fits:** the parse+compat core of the tool, independent of the FastMCP wrapper, so it
is unit-tested directly with hand-built `BuildHost` objects (the `profile_examples.py` pattern).

**Files:** create `src/kdive/mcp/tools/lifecycle/runs/validate_profile.py`.

**Shape:**

```
_OBJECT_ID = "profile-validation"
_FIX_NEXT = ["runs.profile_examples"]   # failure → go fix the shape
_OK_NEXT  = ["runs.create"]             # valid → create the Run

async def validate_build_profile(
    pool: AsyncConnectionPool, build_profile: BuildProfileInput
) -> ToolResponse:
    try:
        parsed = BuildProfile.parse(build_profile)
    except CategorizedError as exc:
        return ToolResponse.failure_from_error(_OBJECT_ID, exc, suggested_next_actions=_FIX_NEXT)
    if isinstance(parsed, ExternalBuildProfile):
        return _valid(parsed, data={"source": "external"})
    return await _validate_server(pool, parsed)
```

- `_validate_server`: `name = parsed.build_host or "worker-local"`; open ONE pool connection and
  `host = await get_by_name(conn, name)`. If `host is None`: success with
  `build_host_registered=False`, `host_kind=None`. Else
  `check_source_kind_compatibility(host_kind=host.kind, is_git=is_git_source(parsed),
  build_host=name)` inside try/except → on `CategorizedError` return `failure_from_error(...,
  suggested_next_actions=_FIX_NEXT)`; on pass success with `build_host_registered=True`,
  `host_kind=host.kind.value`.
- `_valid(parsed, *, server fields…)`: builds `data` with `source`, `profile =
  dump_build_profile(parsed)`, and (server lane) `build_host`, `build_host_registered`,
  `host_kind`, `source_kind = "git" if is_git_source(parsed) else "warm-tree"`. Returns
  `ToolResponse.success(_OBJECT_ID, "valid", data=data, suggested_next_actions=_OK_NEXT)`.
- Module docstring cites ADR-0259 and the `_compat_block_response` create-time twin it mirrors
  (the `"worker-local"` default + absent-host allow). Keep functions ≤100 lines / complexity ≤8;
  split the server branch into its own helper as above.

**Imports:** `BuildProfile, ExternalBuildProfile, ServerBuildProfile, dump_build_profile,
is_git_source` from `kdive.profiles.build`; `get_by_name` from `kdive.db.build_hosts`;
`check_source_kind_compatibility` from `kdive.services.runs.build_host_selection`;
`BuildProfileInput` from `kdive.profiles.types`; `CategorizedError` from `kdive.domain.errors`;
`ToolResponse` from `kdive.mcp.responses`; `JsonValue` from `kdive.serialization`.

**Acceptance:** `validate_build_profile` returns the spec's envelopes for every Edge & error
case row; functions within limits; `ty`/`lint` clean.

## Task 2 — Register the tool on the `runs.*` registrar

**Where it fits:** exposes the handler as an MCP tool, mirroring
`_register_runs_profile_examples`.

**Files:** edit `src/kdive/mcp/tools/lifecycle/runs/registrar.py`.

- Import `validate_build_profile as _validate_build_profile`.
- Add `_register_runs_validate_profile(app, pool)` to the `register()` body (next to
  `_register_runs_profile_examples`).
- The tool: `@app.tool(name="runs.validate_profile", annotations=_docmeta.read_only(),
  meta={"maturity": "implemented"})`. Single param `build_profile: Annotated[BuildProfileInput,
  Field(description=…)]`. Body: `current_context()` (auth-only, ADR-0117 defence-in-depth — no
  role gate, no audit), then `return await _validate_build_profile(pool, build_profile)`.
- `Field` description: one focused paragraph — what it checks (parse + build-host/source
  compatibility), what `valid` does **not** guarantee (buildability, capacity, host
  availability), that it inserts no Run / consumes no capacity, and a pointer to
  `runs.profile_examples` for a ready-to-edit shape. Plain factual language (no "robust",
  "comprehensive", etc.).

**Acceptance:** tool appears in `app.list_tools()` with `readOnlyHint=True`; calling its `.fn`
returns the handler's envelope; `current_context()` is consulted (auth-only).

## Task 3 — Unit tests for the handler (driven directly)

**Files:** create `tests/mcp/lifecycle/test_runs_validate_profile.py`.

Drive `validate_build_profile` directly. The pool is only touched on the server lane; use the
`migrated_url` fixture + the `_pool` async-context helper from
`test_runs_profile_examples.py` for server-lane cases (the seeded `worker-local` LOCAL row is
always present; insert an `ssh` row via raw SQL, as `_insert_ssh_host` does, for the
remote-incompat case). External-lane and parse-failure cases need no DB — pass a closed/never-
opened pool or skip the connection by asserting they return before `get_by_name` (prefer the
real pool fixture for uniformity).

Cover every **Edge & error cases** table row (one test each):
- valid external → `status="valid"`, `data.source=="external"`, no `build_host` key.
- valid server warm-tree vs `worker-local` → `source_kind=="warm-tree"`,
  `build_host_registered is True`, `host_kind=="local"`, `data.profile` round-trips through
  `BuildProfile.parse`.
- valid server git vs `worker-local` → `source_kind=="git"`.
- server warm-tree naming the inserted ssh host → `status=="error"`,
  `error_category=="configuration_error"`, `data.build_host`/`data.host_kind` present,
  `suggested_next_actions==["runs.profile_examples"]`.
- server naming an unregistered host → `valid`, `build_host_registered is False`,
  `host_kind is None`.
- omitted `source` → `valid`, `data.profile["source"]=="server"`.
- unknown `source`, extra field, external-with-server-fields, bare-URL ref, empty-string ref,
  wrong-type `schema_version` → each `error`/`configuration_error`; assert the bare-URL case's
  detail names only the scheme, never the submitted URL (redaction).
- success path asserts `suggested_next_actions==["runs.create"]`.

**Acceptance:** all rows covered; each error test asserts category + redaction where relevant;
red-first confirmed before implementing Task 1.

## Task 4 — Registrar boundary + auth-only test

**Files:** same test module (a `--- registrar boundary ---` section, mirroring
`test_runs_profile_examples.py`).

- Build a `FastMCP` app, `runs_registrar.register(app, pool, resolver=cast(...))`,
  `monkeypatch` `runs_registrar.current_context` to a fake that records it was called.
- Assert `"runs.validate_profile"` is registered, `readOnlyHint is True`, calling `.fn(profile)`
  returns a `ToolResponse`, and the fake context was consulted exactly once (auth-only).

**Acceptance:** boundary test green; proves the wrapper is read-only and auth-gated.

## Task 5 — Parity tests (the two invariants)

**Files:** same test module (a `--- parity ---` section).

1. **Compat parity** vs `_compat_block_response`: for a matrix of `(build_profile, host-kind)`
   pairs (local+warm, local+git, ssh+warm, ssh+git, unregistered), assert
   `validate_build_profile`'s pass/fail equals `_compat_block_response`'s `None`/error for the
   same parsed profile against the same DB rows. Import `_compat_block_response` from
   `kdive.services.runs.admission` and call it with a parsed profile + a connection; compare the
   verdicts (both `None` vs both an error / `valid` vs `failure`).
2. **Structural parity** vs the boundary union: build
   `TypeAdapter(ExternalBuildProfile | ServerBuildProfile)`; for a matrix of valid and malformed
   documents (the parse rows above), assert
   `accepts_via_parse(doc) == accepts_via_union(doc)` where `accepts_*` catch the respective
   validation errors. This pins that `BuildProfile.parse` and the `runs.create` boundary cannot
   diverge on accept/reject.

**Acceptance:** both parity tests green; a deliberate local break (e.g. flipping a verdict)
makes the matching parity test fail.

## Task 6 — Regenerate generated docs + final guardrails

**Files:** `docs/guide/reference/runs.md` (generated — do not hand-edit; `just docs`).

- Run `just docs` to regenerate the tool reference so `runs.validate_profile` appears; commit
  the regenerated file. `just docs-check` must pass.
- Run the FULL local suite once before the branch review: `just lint && just type && just test
  && just docs-check && just adr-status-check && just docs-links`. Architecture/boundary/doc-gen
  tests live outside the edited dirs and only fail in a full run.

**Acceptance:** every recipe green; `docs/guide/reference/runs.md` includes the new tool.

## Rollback / cleanup

Each task is additive (new module, new registrar function, new test module, regenerated
generated doc). Rollback = `git revert` the relevant commit(s); no schema, migration, or data
change exists to undo. No external-service or destructive operation is involved.

## Sequencing

Task 3 (red tests) → Task 1 (handler, to green) → Task 2 (registrar) → Task 4 (boundary test)
→ Task 5 (parity) → Task 6 (docs + full suite). Commit per logical task with guardrails green.
