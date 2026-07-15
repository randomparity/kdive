# Cross-platform dev containers (#1182) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship ADR-0356 (already committed, Proposed) plus a CI guard,
`scripts/check_container_arch_matrix.py`, that fences the docker-compose image set against the
authoritative arch-support matrix embedded in the ADR — asserting per-token ppc64le obligations
so the ppc64le core-loop invariant is machine-checkable.

**Architecture:** One new guard script + its tests + one `justfile` recipe wired into `just ci`.
The guard parses `docker-compose.yml` with `yaml.safe_load` (PyYAML is a hard dependency) and
the ADR matrix block as a Markdown table, then runs six assertions. No production/`src` change,
no migration, no schema change, no new dependency. The ADR/spec/README already landed in the
design commits; this plan adds the code that enforces them and flips the ADR to Accepted at ship.

**Tech Stack:** Python 3.14, `uv`, `pytest`, `ruff`, `ty`; `yaml.safe_load`; `just`.

## Global Constraints

- Spec: `docs/design/2026-07-15-cross-platform-dev-containers-1182.md`; ADR:
  `docs/adr/0356-cross-platform-dev-containers.md`. Every design decision lives there — read the
  spec's "The CI guard contract" section; it is the authoritative contract for the six assertions.
- `BASE_BRANCH = main`; branch `feat/cross-platform-dev-containers-1182` (already created).
- Guardrails: `just lint` (`ruff check` + `ruff format --check`), `just type` (`ty`, whole tree
  incl. `tests/`), `just test` (excludes `live_vm`), and the full `just ci` before push. Run a
  single test with `uv run python -m pytest tests/scripts/test_check_container_arch_matrix.py::<name> -q`.
- Code-quality limits (repo + global CLAUDE.md): ≤100 lines/function, cyclomatic ≤8, ≤5
  positional params, 100-char lines, absolute imports only, Google-style docstrings on public
  APIs. Lint set `E,F,I,UP,B,SIM`; `ty` strict.
- Doc style: no "critical/crucial/essential/significant/comprehensive/robust/elegant/sprint";
  use "Milestone" not "Sprint".
- Guard-recipe convention: the guard runs via `uv run python scripts/<name>.py` (the
  `config-guard`/`env-docs-check` shape — it needs PyYAML from the venv), **not** plain
  `python3` (that shape, used by `adr-status-check`, is for stdlib-only guards). Exit 0 clean,
  non-zero on any violation; messages to `stderr`; `main() -> int` with
  `raise SystemExit(main())`.

## Authoritative facts (probed 2026-07-15; do not re-probe unless a step fails)

- Compose images (`docker-compose.yml`): `postgres:17`, `minio/minio:RELEASE.2025-04-22T22-12-26Z`,
  `minio/mc:RELEASE.2025-04-16T18-13-26Z`, `ghcr.io/navikt/mock-oauth2-server:3.0.3`,
  `prom/prometheus:v3.12.0`, `grafana/grafana:13.0.3`, `kdive:dev` (on `migrate`/`server`/
  `worker`/`reconciler`; `migrate` carries `build: .`).
- Profiles: `prometheus` and `grafana` are `profiles: ["obs"]` (opt-in); all others default.
- Anchors/merge-keys present: `x-readyz-cadence: &readyz_cadence`, `x-backends: &backends`,
  `<<: *backends`, `<<: *readyz_cadence`; top-level `volumes:` after `services:`.
- The matrix (guard-read) is the `<!-- arch-matrix:begin -->` … `<!-- arch-matrix:end -->` block
  in the ADR. Current rows resolve clean under all six assertions (postgres/minio/mc/prometheus
  = rely-on-upstream ✅; oidc = mirror #1183; grafana = accept-gap opt-in; kdive:dev = build-local
  with `— — —` arch cells).

---

## File Structure

- `scripts/check_container_arch_matrix.py` — new guard. Public, testable functions:
  - `parse_compose(text: str) -> dict[str, ImageInfo]` — `yaml.safe_load` → read only the
    `services` mapping → per image ref, aggregate `{default_profile: bool, built: bool}`
    (`default_profile` = some using service has no `profiles` key; `built` = some using service
    has a `build` key).
  - `parse_matrix(adr_text: str) -> list[MatrixRow]` — locate the marker block (hard-error if
    absent/empty), read the header row to index the `amd64`/`arm64`/`ppc64le`/`Handling` columns
    by name, take data rows (first cell a backtick-wrapped ref), hard-error on a short row.
  - `evaluate(compose_text: str, adr_text: str) -> list[str]` — run the six assertions, return
    human-readable violation messages (empty list = pass).
  - `main() -> int` — read `docker-compose.yml` and the ADR, call `evaluate`, print each
    violation to stderr, return `1` if any else `0`.
  - `HANDLING = {"rely-on-upstream", "mirror", "build-local", "accept-gap"}`,
    `ARCH_ALPHABET = {"✅", "❌", "—"}`, plus module-level file paths (repo-root relative, the
    `check_adr_status.py` pattern).
- `tests/scripts/test_check_container_arch_matrix.py` — new tests. Model on
  `tests/scripts/test_config_env_guard.py` / `test_check_env_documented.py`: import the guard's
  functions and drive `evaluate()` with crafted compose+matrix strings (no temp files), plus one
  test that runs `evaluate()` against the **real** repo `docker-compose.yml` + ADR and asserts
  zero violations (the live-contract pin).
- `justfile` — add the `container-arch-check` recipe and append it to the `ci:` recipe list.

---

## Task 1: Failing guard tests (TDD red)

**Files:**
- Create: `tests/scripts/test_check_container_arch_matrix.py`

**Interfaces:**
- Consumes: `scripts.check_container_arch_matrix` (`evaluate`, `parse_compose`, `parse_matrix`).
- Produces: the failing assertions Task 2 turns green.

- [ ] **Step 1: Write the test module.** Define small string fixtures: a `GOOD_COMPOSE`
  (minimal valid compose with a `services:` map — include one anchor + `<<:` merge and a
  top-level `volumes:` block so the parser's YAML handling is exercised) and a `GOOD_MATRIX`
  (a marker-delimited table whose image set matches `GOOD_COMPOSE` and whose rows are clean).
  Then one test per contract clause, each mutating a copy of the good pair:
  - `test_clean_pair_has_no_violations` — `evaluate(GOOD_COMPOSE, GOOD_MATRIX) == []`.
  - `test_real_repo_files_pass` — read the actual `docker-compose.yml` and
    `docs/adr/0356-cross-platform-dev-containers.md`; assert `evaluate(...) == []`. (This is the
    live-contract pin; it also proves the current matrix is self-consistent.)
  - `test_image_in_compose_missing_from_matrix` — add a service image absent from the matrix →
    a violation naming that image.
  - `test_matrix_row_missing_from_compose` — add a matrix row for an image not in compose → a
    violation naming that image.
  - `test_unknown_handling_token` — a row with handling `mystery` → violation.
  - `test_rely_on_upstream_requires_ppc64le` — a `rely-on-upstream` row with ppc64le `❌` →
    violation; also `test_rely_on_upstream_rejects_malformed_ppc64le_cell` — cell empty / `yes` /
    prose → violation (fail-closed, exact `✅`).
  - `test_arch_cell_outside_alphabet` — an arch cell of `partial` → violation.
  - `test_accept_gap_on_default_profile_image` — an `accept-gap` row whose image is on a service
    with no `profiles` key → violation.
  - `test_mirror_row_requires_issue_reference` — a `mirror` row with no `#NNNN` anywhere in the
    row → violation.
  - `test_build_local_requires_a_building_service` — a `build-local` row whose image no service
    `build:`s → violation.
  - `test_missing_matrix_block_is_hard_error` and `test_empty_matrix_block_is_hard_error` —
    `evaluate` raises (or returns a violation) rather than passing vacuously; assert the failure.
  - `test_header_and_separator_rows_are_not_data_rows` — a matrix whose only non-header content
    is the `|---|` separator counts as empty (hard error), proving header/separator are skipped.

- [ ] **Step 2: Run the tests, verify they FAIL** (module does not exist yet):
  `uv run python -m pytest tests/scripts/test_check_container_arch_matrix.py -q`
  Expected: collection/import error or failures — the guard module is unwritten.

- [ ] **Step 3: Do NOT commit yet.** Leave the tests red in the working tree; Task 2 commits the
  tests and the guard together so every commit is green (the repo's green-at-every-commit rule).

---

## Task 2: Implement the guard (TDD green)

**Files:**
- Create: `scripts/check_container_arch_matrix.py`

**Interfaces:**
- Consumes: `docker-compose.yml`, the ADR matrix block; `yaml.safe_load`.
- Produces: the module that turns Task 1 green.

- [ ] **Step 1: Write `parse_compose`.** `data = yaml.safe_load(text)`; iterate
  `data.get("services", {})`. For each service dict read `image` (skip services without one),
  `profiles` (presence → opt-in), `build` (presence → built). Aggregate into
  `dict[image_ref, ImageInfo(default_profile, built)]` where `default_profile` ORs "no profiles"
  across using services and `built` ORs "has build". Use a small frozen dataclass for `ImageInfo`.

- [ ] **Step 2: Write `parse_matrix`.** Slice between the two markers (hard-error via a raised
  `ValueError` if a marker is missing). Split into table rows (`|`-delimited). The header row is
  the first row whose cells include `Handling`; build a name→index map for `amd64`/`arm64`/
  `ppc64le`/`Handling` (hard-error if any missing). A data row is one whose first cell, stripped
  of backticks/whitespace, is non-empty and not `Image`/`---`; hard-error on a data row with
  fewer cells than the header. Return `list[MatrixRow(image, amd64, arm64, ppc64le, handling,
  raw_row)]`. If there are zero data rows, hard-error (no vacuous pass).

- [ ] **Step 3: Write `evaluate`.** Compose `parse_compose` + `parse_matrix`, collect violation
  strings for the six assertions (spec "What the guard asserts"):
  1. set(compose images) == set(matrix images); report each asymmetric difference by name.
  2. every `handling ∈ HANDLING`.
  3. every arch cell (amd64/arm64/ppc64le) `∈ ARCH_ALPHABET`; and `handling == rely-on-upstream`
     ⟹ `ppc64le.strip() == "✅"`.
  4. `handling == accept-gap` ⟹ the image's `ImageInfo.default_profile` is False.
  5. `handling == mirror` ⟹ `re.search(r"#\d+", raw_row)` matches.
  6. `handling == build-local` ⟹ the image's `ImageInfo.built` is True.
  Keep `evaluate` under the complexity limit by delegating each assertion to a small helper that
  returns `list[str]`; `evaluate` concatenates. A matrix image with no compose entry (assertion
  1 failure) is skipped by assertions 4/6 to avoid a `KeyError` cascade (report the drift once).

- [ ] **Step 4: Write `main`.** Read the two files (repo-root-relative module constants), call
  `evaluate`, print `f"docker-compose.yml/matrix: {msg}"` lines to stderr, print a one-line
  summary + return `1` on any violation, else print a clean line and return `0`. Guard a raised
  parse `ValueError` into a printed hard-error + return `1` (so a malformed matrix fails the
  recipe, not crashes it). `raise SystemExit(main())`.

- [ ] **Step 5: Run the tests, verify PASS:**
  `uv run python -m pytest tests/scripts/test_check_container_arch_matrix.py -q` → all green,
  including `test_real_repo_files_pass`.

- [ ] **Step 6: Lint + type the two new files:** `just lint && just type` → clean. Fix any
  complexity/line-length/`SIM` findings (split a helper rather than suppress).

- [ ] **Step 7: Commit the guard + tests together (one green commit):**
  ```bash
  git add scripts/check_container_arch_matrix.py tests/scripts/test_check_container_arch_matrix.py
  git commit -m "feat(1182): add container arch-support matrix drift guard

  Static guard: compose image set == ADR-0356 matrix, and per-handling-token
  ppc64le obligations (rely-on-upstream => ppc64le published, accept-gap =>
  opt-in-profile only, mirror => cites a tracking issue, build-local => built
  by a compose service). Parses compose with yaml.safe_load. ADR-0356."
  ```

---

## Task 3: Wire the recipe into the justfile and `just ci`

**Files:**
- Modify: `justfile`

**Interfaces:**
- Consumes: the Task 2 guard.
- Produces: `just container-arch-check` and its membership in the aggregate `ci` recipe (CI runs
  the recipe individually).

- [ ] **Step 1: Add the recipe** near `env-docs-check` (keep the doc/config guards grouped):
  ```
  # Drift guard: the compose image set matches the ADR-0356 arch-support matrix, and each
  # handling token meets its ppc64le obligation (ADR-0356). Parses compose via yaml.safe_load.
  container-arch-check:
      uv run python scripts/check_container_arch_matrix.py
  ```

- [ ] **Step 2: Append `container-arch-check` to the `ci:` recipe** dependency list (e.g. after
  `env-docs-check`), so `just ci` and CI's per-recipe invocation both gate it. Do not remove or
  reorder existing members.

- [ ] **Step 3: Run the recipe against the real files:** `just container-arch-check` → clean
  (exit 0, "matrix in sync"-style line). Then a negative smoke check: temporarily edit a matrix
  row's handling to an unknown token, re-run, confirm non-zero + a named violation, then revert
  the edit (leave the tree clean). This proves the recipe wiring surfaces failures.

- [ ] **Step 4: Lint the justfile change** (`just lint` covers formatting of tracked files; the
  `check-mermaid`/doc guards are unaffected). Commit:
  ```bash
  git add justfile
  git commit -m "build(1182): gate container-arch-check in just ci"
  ```

---

## Task 4: Full guardrail suite

**Files:** none (unless drift).

- [ ] **Step 1: Run the full gate:** `just ci` → green (lint, type, lint-shell, lint-workflows,
  check-mermaid, docs-links, docs-paths, adr-status-check, docs-check, config guards,
  `container-arch-check`, test). Per `feedback-run-just-ci-before-push`, `just test` alone misses
  generated-doc drift and cross-cutting tests — run full `ci`.

- [ ] **Step 2: Commit any generated-doc drift** (only if `just ci` produced it):
  `git add -A && git commit -m "chore(1182): regenerate docs after guard wiring"` (stage explicit
  paths; never sweep the challenge scratch file).

---

## Task 5: Flip ADR-0356 to Accepted (ship gate — run last, before push)

> Run this at the ship step, after the branch review passes — the implementing PR *is* the
> ratification (ADR README process rule). Kept as its own task so the flip is not forgotten and
> `adr-status-check` stays green (the ADR file and the index row flip together, so their status
> keywords always match).

**Files:**
- Modify: `docs/adr/0356-cross-platform-dev-containers.md` (Status line),
  `docs/adr/README.md` (0356 row's trailing status cell).

- [ ] **Step 1:** Change the ADR `- **Status:** Proposed` line to `- **Status:** Accepted`.
- [ ] **Step 2:** Change the `0356` index row's trailing `| Proposed |` to `| Accepted |`.
- [ ] **Step 3:** `just adr-status-check` → green (index in sync; and because the guard is only
  in `scripts/` + docs, the "no shipped-but-Proposed drift in `src/`" rule is unaffected either
  way). Then `just docs-links docs-paths check-mermaid` → green.
- [ ] **Step 4: Commit:**
  ```bash
  git add docs/adr/0356-cross-platform-dev-containers.md docs/adr/README.md
  git commit -m "docs(1182): accept ADR-0356 (implementing PR ratifies)"
  ```

---

## Self-review notes

- **Spec coverage:** guard exists + six assertions (Task 2) ✓; recipe + `ci` membership (Task 3)
  ✓; unit tests for every failure mode incl. malformed block + live-file pin (Task 1) ✓; ADR
  flips to Accepted with the index row (Task 5) ✓; matrix already lives in the ADR marker block
  (design commits) ✓.
- **No new public contract in `src` / no schema / no migration / no new dependency** — PyYAML is
  already pinned; the guard is a `scripts/` tool + `tests/` + one recipe.
- **Rollback:** each task is a single additive commit. Reverting Task 2/3 removes the guard and
  its recipe; reverting Task 5 returns the ADR to Proposed (both file and index together, so
  `adr-status-check` stays green in either state). No migration, no external write, no data
  change.
- **Complexity guard:** `evaluate` delegates each assertion to a `list[str]`-returning helper to
  stay ≤8 cyclomatic / ≤100 lines; `parse_matrix`/`parse_compose` are single-purpose.
