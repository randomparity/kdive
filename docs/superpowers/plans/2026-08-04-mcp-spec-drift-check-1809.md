# MCP Spec and Protocol-Version Drift Check — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect MCP protocol drift on two clocks — an offline `ci` gate that fails when a dependency bump moves the protocol range KDIVE advertises, and a weekly cron that files a tracking issue when upstream publishes a revision newer than the one KDIVE declares.

**Architecture:** One script, `scripts/check_mcp_spec_version.py`, with two modes over one pair of declared constants in `src/kdive/mcp/__init__.py`. Default mode asserts the pinned library still advertises what KDIVE declares (offline; wired into both the `ci` recipe and a dedicated `ci.yml` step). `--upstream` mode lists the specification repository's `schema/` directory and reports revisions newer than the declared ceiling (network; wired only into `.github/workflows/mcp-spec-drift.yml`, gating nothing). Three exit codes separate drift (1) from "could not determine drift" (2). A writable `gen_doc_constants` binding keeps one agent-facing sentence honest.

**Tech Stack:** Python 3.14, `uv`, stdlib `urllib.request`/`re`/`json`, `pytest`, `PyYAML` (already a dependency, used by `tests/scripts/test_live_workflow_shape.py`).

Spec: `docs/specs/2026-08-03-mcp-spec-drift-check-1809-design.md`. ADR: `docs/adr/0537-mcp-protocol-version-drift-two-clocks.md`. Issue: #1809.

## Global Constraints

- Python 3.14, `uv`. Ruff line length 100, lint set `E,F,I,UP,B,SIM`. `ty` strict (whole tree, `src`+`tests`).
- Absolute imports only (`kdive....`), no relative imports. Google-style docstrings on public APIs.
- Doc-style: plain, factual prose; never "critical/robust/comprehensive/elegant"; "Milestone" not "Sprint".
- CI invokes each `just` recipe individually and **never** runs `just ci`. A new gate needs both the `ci` recipe entry and its own `ci.yml` step, or it gates nothing.
- Run a single test: `uv run python -m pytest <path>::<name> -q`.
- **The `mcp` constants are read as module attributes at call time** (`mcp.types.LATEST_PROTOCOL_VERSION`, `mcp.shared.version.SUPPORTED_PROTOCOL_VERSIONS`), never via `from ... import`. `shared/version.py` materializes the supported list with the ceiling substituted at import, so a `from`-bound name defeats the monkeypatches Task 3's tests depend on.
- **No test may perform network I/O.** `newer_revisions` is pure; `--upstream` tests inject the fetch.

### Host note (macOS)

`gen_doc_constants` and `gen_doc_resources` import provider composition, which resolves the Linux-only `fallocate` symbol at import time — they **cannot run on macOS**. Tasks 5's gates are verified in a Linux container:

```
docker run --rm -v "$PWD:/w" -w /w -e UV_PROJECT_ENVIRONMENT=/venv -e UV_CACHE_DIR=/uvcache \
  -e DEBIAN_FRONTEND=noninteractive ghcr.io/astral-sh/uv:python3.13-bookworm bash -lc '
  apt-get -qq update >/dev/null && apt-get -qq install -y --no-install-recommends libvirt-dev pkg-config gcc >/dev/null
  uv sync --locked --quiet && uv run python -m scripts.gen_doc_constants --check'
```

`check-doc-paths.sh` needs GNU `grep -P`, and the doc scripts need bash ≥ 4 — same container, or `/opt/homebrew/bin/bash ./scripts/check-doc-links.sh` for links alone.

---

### Task 1: Declare the protocol constants

**Files:**
- Modify: `src/kdive/mcp/__init__.py` (empty today)
- Test: `tests/scripts/test_check_mcp_spec_version.py` (new module; created here, extended by Tasks 2–4)

**Interfaces:**
- Produces: `kdive.mcp.MCP_PROTOCOL_VERSION: str`, `kdive.mcp.MCP_SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...]`.

- [ ] **Step 1: Write the failing test**

Create `tests/scripts/test_check_mcp_spec_version.py` asserting both constants exist and match the pinned library — this is the gate's own fixture:

```python
"""Tests for the MCP protocol-version drift guard (ADR-0537)."""

from __future__ import annotations

import mcp.shared.version
import mcp.types

from kdive.mcp import MCP_PROTOCOL_VERSION, MCP_SUPPORTED_PROTOCOL_VERSIONS


def test_declared_constants_match_the_pinned_library() -> None:
    assert MCP_PROTOCOL_VERSION == mcp.types.LATEST_PROTOCOL_VERSION
    assert MCP_SUPPORTED_PROTOCOL_VERSIONS == tuple(mcp.shared.version.SUPPORTED_PROTOCOL_VERSIONS)
```

- [ ] **Step 2: Make it pass**

```python
"""KDIVE's MCP surface, and the protocol range it declares (ADR-0537).

The negotiated protocol range is a property of the pinned ``mcp`` dependency, not of KDIVE
source: the wire layer is delegated in full. Declaring it here makes a dependency bump that
moves either end a reviewable edit rather than a silent consequence of ``uv lock``.
``scripts/check_mcp_spec_version.py`` asserts both against the installed library.
"""

from __future__ import annotations

MCP_PROTOCOL_VERSION = "2025-11-25"
MCP_SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
```

- [ ] **Step 3: Verify** — `uv run python -m pytest tests/scripts/test_check_mcp_spec_version.py -q`, `just lint`, `just type`.

---

### Task 2: The pure comparison — `newer_revisions`

**Files:**
- Create: `scripts/check_mcp_spec_version.py`
- Test: `tests/scripts/test_check_mcp_spec_version.py`

**Interfaces:**
- Produces: `newer_revisions(entries: Iterable[str], declared: str) -> list[str]`, and `_RECOGNIZED = re.compile(r"\d{4}-\d{2}-\d{2}\Z")` applied with `fullmatch` (either guard alone rejects a trailing newline; both are kept as belt-and-braces).

- [ ] **Step 1: Write the four failing tests**

`test_newer_revisions_finds_a_later_published_revision` — the real listing shape (`2024-11-05`, `2025-03-26`, `2025-06-18`, `2025-11-25`, `2026-07-28`, `draft`) against declared `2025-11-25` returns exactly `["2026-07-28"]`.

`test_newer_revisions_ignores_draft_and_suffixed_entries` — `draft` and `2026-09-01-rc` are both dropped. **Mutation control:** without the filter this returns `draft` as newest, since `d` sorts above any digit.

`test_newer_revisions_is_empty_when_declared_is_newest` and `test_newer_revisions_is_empty_on_an_equal_revision` — the quiet paths. The equal case is the boundary a `>=` breaks.

- [ ] **Step 2: Implement**

Filter to `RECOGNIZED`, keep entries `> declared` as strings (ISO-8601 sorts lexicographically — no date parsing, no timezone), return sorted oldest-first.

- [ ] **Step 3: Verify** — the four tests pass; break the filter and confirm the `draft` test reddens, then revert.

---

### Task 3: Offline mode and the exit contract

**Files:**
- Modify: `scripts/check_mcp_spec_version.py`
- Test: `tests/scripts/test_check_mcp_spec_version.py`

**Interfaces:**
- Produces: `check_offline() -> int`, `main(argv: Sequence[str] | None = None) -> int`.

- [ ] **Step 1: Write the failing tests**

- `test_offline_mode_passes_when_the_pinned_library_agrees` — exit 0 against the real pin.
- `test_offline_mode_fails_when_the_ceiling_moves` — monkeypatch `mcp.types.LATEST_PROTOCOL_VERSION`; exit 1, and the message names the declared value, the library value, and the remediation.
- `test_offline_mode_fails_when_a_supported_revision_is_dropped` — monkeypatch `mcp.shared.version.SUPPORTED_PROTOCOL_VERSIONS` to drop `2024-11-05`, ceiling unchanged; exit 1. **This is the client-breaking direction a ceiling-only assertion passes green.**
- `test_offline_mode_makes_no_network_call` — monkeypatch `socket.socket` to raise; exit 0. Criterion 2.

- [ ] **Step 2: Implement**

`check_offline()` compares both module attributes against the declared constants and returns 0/1, printing an actionable message on mismatch (operation, both values, the fix: edit `src/kdive/mcp/__init__.py`, then `just doc-constants` and `just resources-docs`).

`main()` wraps its body so that **any** exception other than `SystemExit`/`KeyboardInterrupt` prints a traceback and returns **2**. Exit 1 must mean "drift determined" and nothing else — Python's default of 1 on an uncaught exception is what would otherwise file a false drift issue.

- [ ] **Step 3: Verify** — all tests pass; `just lint`, `just type`.

---

### Task 4: Upstream mode, with the sanity floor

**Files:**
- Modify: `scripts/check_mcp_spec_version.py`
- Test: `tests/scripts/test_check_mcp_spec_version.py`

**Interfaces:**
- Produces: `fetch_schema_entries() -> list[str]` and `check_upstream() -> int`. The fetch is injected by monkeypatching the module attribute, not by a parameter — the tests patch `check_mcp_spec_version.fetch_schema_entries`.

- [ ] **Step 1: Write the failing tests**

- `test_upstream_mode_exits_2_on_a_fetch_failure` — fetch raises `URLError`; exit 2, not 1, and the report says nothing about drift.
- `test_upstream_mode_exits_2_on_an_unexpected_exception` — payload shape breaks the reader (`KeyError`); exit 2. Separate from the above: this is the top-level guard under test, not the handled path.
- `test_upstream_mode_exits_2_when_the_listing_has_no_recognized_revisions` — a restructured listing (`v2026-07-28`, `versions`, `draft`); exit **2, not 0**. The sanity-floor test.
- `test_upstream_mode_reports_drift_and_emits_github_output` — a listing containing `2026-07-28`; exit 1, and with `GITHUB_OUTPUT` pointed at a tmp file, that file contains `newest=2026-07-28` and `declared=2025-11-25`.

- [ ] **Step 2: Implement**

Fetch `https://api.github.com/repos/modelcontextprotocol/modelcontextprotocol/contents/schema` via `urllib.request`, sending `Authorization: Bearer $GITHUB_TOKEN` when set (unauthenticated GitHub API is 60/hour per IP and Actions runners share addresses).

**Sanity floor before comparing:** `MCP_PROTOCOL_VERSION` must appear among the recognized entries. If it does not, the listing is not the document this check reads — print that and return 2. Without it, an upstream restructure that still returns HTTP 200 leaves every entry unmatched, the comparison finds nothing newer, and the job passes green permanently (ADR-0518's named failure).

On drift: print the human report once, append `newest=`/`declared=` to `$GITHUB_OUTPUT` when set, return 1.

- [ ] **Step 3: Verify** — all tests pass. Manually run `uv run python scripts/check_mcp_spec_version.py --upstream` once and confirm it reports `2026-07-28` and exits 1.

---

### Task 5: Wire the offline gate and the doc statement

**Files:**
- Modify: `justfile` (new `mcp-spec-check` recipe; add to the `ci` list)
- Modify: `.github/workflows/ci.yml` (dedicated step)
- Modify: `scripts/gen_doc_constants.py` (fourth binding)
- Modify: `docs/guide/agent-index.md` (one sentence)
- Regenerate: `src/kdive/mcp/resources/_content/agent-index.md` via `just resources-docs`
- Test: `tests/scripts/test_check_mcp_spec_version.py`

- [ ] **Step 1: Write the failing test**

`test_ci_workflow_runs_the_mcp_spec_check_recipe` — `yaml.safe_load` `.github/workflows/ci.yml`; assert `just mcp-spec-check` appears among the job's `run` steps, in the idiom of `tests/scripts/test_live_workflow_shape.py`. Criterion 1 rests entirely on that hand-listed step, and this repo has lost that wiring twice (ADR-0518/#1723, ADR-0410) — the failure mode is green.

- [ ] **Step 2: Add the recipe and the step**

```
# Fail when the pinned mcp library no longer advertises the protocol range KDIVE
# declares (ADR-0537). Offline by design; the upstream half runs on a weekly cron.
mcp-spec-check:
    uv run python scripts/check_mcp_spec_version.py
```

Append `mcp-spec-check` to the `ci` recipe list, and add the `ci.yml` step carrying the standard comment: `# CI invokes recipes individually (never `just ci`), so list this explicitly to gate PRs.`

- [ ] **Step 3: Add the sentence and the binding**

One sentence in `docs/guide/agent-index.md` naming the negotiated ceiling, matching `MCP protocol revision (\d{4}-\d{2}-\d{2})`. Add the binding to `gen_doc_constants.bindings()` with `expected=MCP_PROTOCOL_VERSION` and **`writable=True`** — ADR-0410 scopes the guarded kind to hand-authored `.py` docstrings ("the repo's generators only ever write `docs/`"), and `write()` substitutes capture group 1 alone, so it cannot disturb the surrounding prose.

Do **not** bind `docs/adr/0268-tool-gateway-dispatcher.md:27` or `docs/design/2026-06-27-tool-gateway-dispatcher-866.md:34`. Those cite `2025-06-18` as the revision that established the `tools/list` rule — historical fact, not current negotiation. Binding them would fail the gate permanently from the next bump.

- [ ] **Step 4: Re-mirror and verify**

`agent-index.md` is an ADR-0151 doc resource. Run `just resources-docs` and commit the regenerated `src/kdive/mcp/resources/_content/agent-index.md`, or `resources-docs-check` goes red. In the Linux container: `just doc-constants-check`, `just resources-docs-check`, `just mcp-spec-check`.

---

### Task 6: The drift workflow, and ADR ratification

**Files:**
- Create: `.github/workflows/mcp-spec-drift.yml`
- Modify: `docs/adr/0537-mcp-protocol-version-drift-two-clocks.md` (Status → Accepted)
- Test: `tests/scripts/test_check_mcp_spec_version.py`

- [ ] **Step 1: Write the failing test**

A set of shape tests over the workflow (`yaml.safe_load`), one per property the live run would otherwise be first to exercise: both triggers with `pull_request` absent; the `report` job's `if:` gating the exit-0 row; `check-drift` holding `contents: read` only while `issues: write` lives on `report`, which runs no `uv run`; `GITHUB_TOKEN` on the check step and `GH_TOKEN` on the report job; `--state all`, the `contains` select and the absence of a `--label` conjunct; the empty-payload and pass-while-drifting rows; and the three label strings on `gh issue create`. Static, so they run pre-merge — which matters because the live run cannot.

- [ ] **Step 2: Write the workflow**

Weekly cron (Mondays 12:00 UTC) plus `workflow_dispatch`, `concurrency` group with `cancel-in-progress: false`, SHA-pinned `actions/checkout` with `persist-credentials: false`, a header comment stating why the job exists. Top-level `permissions: contents: read`. Two jobs: `check-drift` (`contents: read`) checks out, installs `libvirt-dev`, runs `uv sync --locked` and `--upstream`, and emits `exit_code`/`newest`/`declared`/`newer`; `report` (`issues: write`, `needs: check-drift`, `if: needs.check-drift.outputs.exit_code != '0'`) runs only `gh` and branches:

| exit | matching issue | action | job |
| --- | --- | --- | --- |
| 0 | — | nothing | pass |
| 1 | none | `gh issue create` titled `MCP spec drift: upstream <newest> not yet adopted`, labels `area:mcp-api`, `type:chore`, `status:needs-triage` | fail |
| 1 | open | nothing | pass |
| 1 | closed | nothing | pass |
| 2 | — | file nothing | fail |
| anything else | — | file nothing | fail |

Title and body built **only** from the `$GITHUB_OUTPUT` values. Dedup is `gh issue list --state all --search "<newest> in:title" --json number,title` followed by a `contains` select on the revision — keyed on the revision rather than the whole title, so it survives a triage retitle and matches a human-filed issue. **No `--label` conjunct:** `gh` ANDs it into the query, which would hide issue #1485 and duplicate it on the first run. See the design doc's dedup section for the measured GitHub search behaviour behind both choices.

Split the workflow into two jobs: `check-drift` (`contents: read`, runs `uv run python`) and `report` (`issues: write`, runs only `gh`). The elevated grant must not be in scope for the process that loads the dependency tree.

- [ ] **Step 3: Flip the ADR to Accepted**

`## Status` → `Accepted (2026-08-04)`. `check_adr_status.py` invariant 2 fails a `Proposed` ADR cited from `src/` or `tests/`, "including guard-type ADRs whose enforcement ships purely as tests" — and Task 1's constants module and every test module cite this record.

- [ ] **Step 4: Verify** — `just lint-workflows` (actionlint), `zizmor .github/workflows/mcp-spec-drift.yml`, `just adr-status-check`, and the full container run of the doc gates plus `just test`.

---

## Post-merge

Criterion 3 is verified by a `workflow_dispatch` run **after** merge, reported on #1809: GitHub resolves dispatchable workflows from the default branch, so a branch-only workflow cannot be dispatched. The run is expected to open the `2026-07-28` issue; a second dispatch must find it and pass without filing — the idempotence check.
