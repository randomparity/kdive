# Implementation plan — Build-host source-kind discovery (#536)

- **Spec:** [`../../specs/2026-06-17-buildhost-source-kind-discovery.md`](../../specs/2026-06-17-buildhost-source-kind-discovery.md)
- **ADR:** [`../../adr/0160-buildhost-source-kind-discovery.md`](../../adr/0160-buildhost-source-kind-discovery.md)
- **Issue:** #536
- **Branch:** `feat/buildhost-source-kind-discovery-536` (worktree already created)

## Conventions (apply to every task)

- Python 3.13, `uv`. Tests mirror the package tree under `tests/`.
- Ruff line length 100, lint set `E,F,I,UP,B,SIM`; `ty` strict, **whole-tree** (src + tests).
- Absolute imports only (no `..`). Google-style docstrings on non-trivial public APIs.
- Conventional-commit subjects ≤72 chars, imperative, ending with the
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` trailer.
- Doc-style guard: no "critical/robust/comprehensive/elegant/significant"; "Milestone" not "Sprint".
- TDD: failing test first, confirm it fails for the right reason, minimal impl, re-run, refactor green.

## Guardrail commands

- Focused test: `uv run python -m pytest <path>::<test> -q`
- Lint: `just lint` (`ruff check` + `ruff format --check`)
- Types: `just type` (`ty check`, whole tree)
- Full PR gate: `just ci` (lint, type, lint-shell, lint-workflows, check-mermaid, test)
- ADR index guard: `python3 scripts/check_adr_status.py`
- **Generated tool reference (load-bearing for this change):** `just docs` regenerates
  `docs/guide/reference/` from the live registry; `just docs-check` is the CI gate.
  Adding `runs.profile_examples` changes the generated reference — regenerate and
  commit in the same change as the registration.
- DB-backed tests need a reachable Docker daemon (disposable Postgres via testcontainers);
  they skip when Docker is absent unless `KDIVE_REQUIRE_DOCKER=1`.

The tasks are ordered by dependency. Tasks 1–2 are the shared foundation; Task 3
(`build_hosts.list`) and Tasks 4–5 (`runs.profile_examples`) both depend on Task 1
and are otherwise independent of each other. Task 6 regenerates the committed tool
reference and depends on Task 5. Do them in order; they are small and tightly
coupled around one module, so a single implementer session is appropriate.

---

## Task 1 — Shared `SourceKind` + `accepted_source_kinds`, refactor the validator

**Where it fits:** The single source of truth the whole issue hangs on. Both read
surfaces and the existing ADR-0157 validator must derive the host-kind → source-kind
matrix from one function so the advertised lane cannot drift from the enforced one.

**Files:**
- `src/kdive/services/runs/build_host_selection.py` (add `SourceKind`,
  `accepted_source_kinds`; rewrite `check_source_kind_compatibility` to consume it).
- `tests/services/test_build_host_selection.py` (the existing validator test module;
  it already imports `BuildHostKind`/`CategorizedError`/`ErrorCategory` — add the
  parameterized drift test and the error-string pin there).

**TDD steps:**
1. Write a failing parameterized test `test_accepted_source_kinds_and_validator_agree`:
   for **every** `BuildHostKind` value and both `is_git ∈ {True, False}`, assert
   `check_source_kind_compatibility(host_kind=k, is_git=g, build_host="h")` raises
   `CategorizedError(category=CONFIGURATION_ERROR)` **iff** the submitted kind
   (`SourceKind.GIT if g else SourceKind.WARM_TREE`) is **not** in
   `accepted_source_kinds(k)`; and does not raise iff it is. Also assert
   `accepted_source_kinds(BuildHostKind.LOCAL) == (SourceKind.WARM_TREE,)` and
   `accepted_source_kinds(BuildHostKind.SSH) == accepted_source_kinds(BuildHostKind.EPHEMERAL_LIBVIRT) == (SourceKind.GIT,)`.
2. Add a test pinning the **exact** error strings/category/details are unchanged for
   the two mismatch cases (LOCAL+git → "a local build host requires a warm-tree
   kernel_source_ref, not a git ref"; remote+warm-tree → "a remote build host
   requires a git kernel_source_ref"), so the refactor is byte-identical (ADR-0157
   no-regression). If existing tests already pin these strings, reuse/keep them.
3. Confirm the new test fails (no `SourceKind`/`accepted_source_kinds` yet).
4. Implement `SourceKind(StrEnum)` (`WARM_TREE = "warm-tree"`, `GIT = "git"`) and
   `accepted_source_kinds(host_kind) -> tuple[SourceKind, ...]` exactly as the spec
   shows. Rewrite `check_source_kind_compatibility` to compute
   `submitted`/`accepted` and early-return when compatible, keeping the two
   host-kind-specific `raise` branches verbatim.
5. Run the focused tests + `just lint` + `just type`.

**Acceptance:** the drift test passes for every `BuildHostKind`; the error-string
test confirms byte-identical messages/category/details; existing
`build_host_selection` / `runs.create` / `runs.build` tests still pass unchanged.

**Rollback:** revert the module; the inline matrix in `check_source_kind_compatibility`
was self-contained, so reverting Task 1 reverts the whole foundation cleanly.

---

## Task 2 — `list_all_hosts` repository read

**Where it fits:** `runs.profile_examples` (Task 4) needs every registered build-host
row. The repository has no "list all" read yet (`list_probeable_ssh_hosts` filters to
ssh+enabled).

**Files:**
- `src/kdive/db/build_hosts.py` (add `list_all_hosts`).
- `tests/db/test_build_hosts_repo.py` (the existing repository test module).

**Fixture note (CHECK constraint — migration 0029):** the `build_hosts` table
constrains rows by kind: an `ssh` row needs `address`/`ssh_credential_ref` NOT NULL
and `base_image_volume` NULL; an `ephemeral_libvirt` row needs `base_image_volume`
NOT NULL and `address`/`ssh_credential_ref` NULL. So an ephemeral fixture **cannot**
reuse an ssh-shaped insert. Build the ephemeral row either via the existing
`register_ephemeral_libvirt_build_host(pool, _admin_ctx(), _ephemeral_request(...))`
handler (and `_ephemeral_request` helper) already used in `test_build_hosts.py`, or a
raw insert with the ephemeral column shape
`(name, kind='ephemeral_libvirt', base_image_volume=..., workspace_root, max_concurrent)`
and NULL `address`/`ssh_credential_ref`. The seeded `worker-local` is `kind='local'`.

**TDD steps:**
1. Write a failing test: seed (via the migrated DB) `worker-local` plus an inserted
   `ssh` host and an `ephemeral_libvirt` host (built per the fixture note above);
   assert `list_all_hosts(conn)` returns all three as `BuildHost` objects ordered by
   name. Assert the empty-of-operator-hosts case still returns `worker-local` (the
   seed is always present).
2. Confirm it fails (no such function).
3. Implement `async def list_all_hosts(conn: AsyncConnection) -> list[BuildHost]` —
   `SELECT * FROM build_hosts ORDER BY name`, mapping each row via `_row_to_host`
   (mirror `list_probeable_ssh_hosts`, minus the WHERE clause). Google-style docstring.
4. Run focused test + `just lint` + `just type`.

**Acceptance:** `list_all_hosts` returns all rows ordered by name as `BuildHost`
objects; the name does **not** collide with the `list_build_hosts` handler (it lives
in `db/build_hosts.py`, distinct module + distinct name).

**Rollback:** revert the function; nothing else depends on it until Task 4.

---

## Task 3 — `build_hosts.list` advertises `supported_source_kinds`

**Where it fits:** Surface 1. Additive field on an existing read tool.

**Files:**
- `src/kdive/mcp/tools/ops/build_hosts/lifecycle.py` (`list_build_hosts`: add the
  derived field to each item's `data`).
- `tests/mcp/ops/test_build_hosts.py` (extend the existing list test).

**TDD steps:**
1. Extend the existing happy-path list test (or add a new one): insert an `ssh` host
   (the existing `_insert_host` helper, or `register_ssh_build_host`) and an
   `ephemeral_libvirt` host (per the Task 2 fixture note — the ssh-shaped `_insert_host`
   helper will violate the migration-0029 CHECK for an ephemeral row, so use
   `register_ephemeral_libvirt_build_host`/`_ephemeral_request` or a raw ephemeral-shape
   insert); call `list_build_hosts`; assert the `ssh` item's
   `data["supported_source_kinds"] == ["git"]`, the ephemeral item's `== ["git"]`, and
   the seeded `worker-local` item's `== ["warm-tree"]`. Assert the field is present on
   **every** item.
2. Confirm it fails (field absent).
3. Implement: in the item comprehension, add
   `"supported_source_kinds": [k.value for k in accepted_source_kinds(BuildHostKind(row["kind"]))]`.
   Import `BuildHostKind` (already importable from `kdive.db.build_hosts`) and
   `accepted_source_kinds` from `kdive.services.runs.build_host_selection`. Note: the
   SQL already selects `kind`; no query change.
4. Run focused test + `just lint` + `just type`.

**Acceptance:** every `build_hosts.list` item carries `supported_source_kinds`;
`local`→`["warm-tree"]`, `ssh`/`ephemeral_libvirt`→`["git"]`; no secret added; same
`platform_auditor` gate; no SQL/schema change.

**Rollback:** remove the one dict entry and its import.

---

## Task 4 — `runs.profile_examples` pure handler

**Where it fits:** Surface 2 core. A pure function over a host list, independently
unit-testable without a pool.

**Files:**
- New `src/kdive/mcp/tools/lifecycle/runs/profile_examples.py` with
  `build_host_profile_examples(hosts: list[BuildHost]) -> ToolResponse` and a module
  docstring (mirror `systems/profile_examples.py`'s docstring style: read-only,
  auth-only, what it projects, the anti-rot guarantee).
- New `tests/mcp/lifecycle/test_runs_profile_examples.py` (mirror
  `tests/mcp/lifecycle/test_systems_profile_examples.py`).

**TDD steps:**
1. Write failing tests asserting the spec's acceptance for the pure handler, driving
   it with hand-built `BuildHost` objects (no DB):
   - one item per host, `object_id == host.name`;
   - each item's `data.host_kind == host.kind.value`,
     `data.supported_source_kinds == [k.value for k in accepted_source_kinds(host.kind)]`,
     `data.build_host == host.name`;
   - `data.profile` parses via `BuildProfile.parse` into a `ServerBuildProfile`;
   - **source-form/advertised-kind agreement:**
     `is_git_source(parse(profile)) is True` iff `"git" in data.supported_source_kinds`
     (string `kernel_source_ref` for local, `{"git":{...}}` for remote);
   - **host-compat:** `check_source_kind_compatibility(host_kind=host.kind,
     is_git=is_git_source(parse(profile)), build_host=host.name)` does not raise;
   - `suggested_next_actions == ["runs.create", "runs.build"]` (collection level);
   - empty host list → valid empty collection (`status == "ok"`,
     `data["count"] == "0"`, `items == []`), not an error.
2. Confirm they fail (module absent).
3. Implement the pure handler:
   - Module-level placeholder constants (`_PLACEHOLDER_WARM_TREE = "REPLACE_ME-warm-tree-name"`,
     `_PLACEHOLDER_GIT_REMOTE`, `_PLACEHOLDER_GIT_REF`), a `_NEXT_ACTIONS =
     ["runs.create", "runs.build"]`, and a `_NOTE` string.
   - `_example_profile(host) -> dict[str, JsonValue]`: build the
     `kernel_source_ref` from `accepted_source_kinds(host.kind)` (if `SourceKind.GIT`
     in accepted → the git object placeholder; else the warm-tree string), set
     `schema_version=1`, `source="server"`, `build_host=host.name`. Deriving the
     source form from `accepted_source_kinds` (not a second `if host.kind` branch) is
     what keeps the example aligned with the advertised field.
   - `_example_item(host) -> ToolResponse.success(...)` with the `data` fields above.
   - `build_host_profile_examples(hosts)` → `ToolResponse.collection("profile-examples",
     "ok", [_example_item(h) for h in hosts], suggested_next_actions=list(_NEXT_ACTIONS))`.
4. Run focused tests + `just lint` + `just type`.

**Acceptance:** all spec acceptance bullets for the examples tool hold against the
pure handler; the example's source form is derived from `accepted_source_kinds`, so a
test mutating the matrix (Task 1) would flip both the advertised field and the example
together.

**Rollback:** delete the new handler + test module; nothing imports them until Task 5.

---

## Task 5 — Register `runs.profile_examples` on the runs plane

**Where it fits:** Wires the pure handler to a pool and exposes it as the MCP tool.

**Files:**
- `src/kdive/mcp/tools/lifecycle/runs/registrar.py` (add `_register_runs_profile_examples`
  and call it from `register`).
- `tests/mcp/lifecycle/test_runs_profile_examples.py` (add a pool-backed test +
  a registrar-boundary test).

**TDD steps:**
1. Write a failing pool-backed test (mirror `test_register_creates_ssh_row_list_shows_ref_only`):
   against `migrated_url`, insert an `ssh` host, call the registered
   `runs.profile_examples` tool fn, assert one item per host (including
   `worker-local`), and the per-host source form. Add a registrar-boundary test
   asserting the tool is registered `read_only` and invokes `current_context()`
   (auth-only) — mirror the read-only-hint assertions in `test_build_hosts.py`.
2. Confirm failure (tool not registered).
3. Implement `_register_runs_profile_examples(app, pool)`:
   ```python
   @app.tool(name="runs.profile_examples", annotations=_docmeta.read_only(),
             meta={"maturity": "implemented"})
   async def runs_profile_examples() -> ToolResponse:
       """Return a ready-to-edit build profile per registered build host. Requires a token."""
       current_context()  # auth-only (ADR-0117): token presence as defence-in-depth.
       async with pool.connection() as conn:
           hosts = await list_all_hosts(conn)
       return build_host_profile_examples(hosts)
   ```
   Add the import of `list_all_hosts` and `build_host_profile_examples`; call
   `_register_runs_profile_examples(app, pool)` from `register(...)`.
4. Run focused tests + `just lint` + `just type`.

**Acceptance:** `runs.profile_examples` is registered `read_only`, auth-only (calls
`current_context()`, no platform/project gate, no audit, no mutation), pool-backed,
and returns one valid example per registered host.

**Rollback:** remove the registrar function, its call, and the imports.

---

## Task 6 — Regenerate the committed tool reference

**Where it fits:** `just docs-check` is a CI gate; a new tool changes the generated
`docs/guide/reference/`. Must be regenerated in the same change so CI stays green.

**Files:** `docs/guide/reference/**` (generated — do not hand-edit).

**Steps:**
1. Run `just docs` (regenerates from the live registry).
2. Run `just docs-check` and confirm it passes (no diff).
3. Review the diff: it should add only the `runs.profile_examples` entry (and any
   `build_hosts.list` field-doc change if the generator renders response fields).
   If unrelated entries changed, stop — that signals an unintended registry change.
4. Commit the regenerated reference together with Task 5's registration commit (or as
   its own commit immediately after), so no commit leaves `docs-check` red.

**Acceptance:** `just docs-check` is green; the diff is limited to the new tool (and
any rendered `supported_source_kinds` field doc).

**Rollback:** `git checkout docs/guide/reference` and re-run `just docs` after
reverting the registration.

---

## Final gate (before push, step 7)

- Run the **full** `just ci` once (not just focused tests): lint, type (whole tree),
  lint-shell, lint-workflows, check-mermaid, test.
- Run `python3 scripts/check_adr_status.py` (ADR index in sync — already added 0160).
- Confirm `just docs-check` green.
- Commits stay small and logically scoped (one per task where practical); do not
  squash.
