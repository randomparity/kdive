# Ledger report CLI verbs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `kdivectl ledger report-all` and `ledger report-granted` curated read verbs that map to `accounting.report_all_projects` and `accounting.report_granted_set`, rendering the rows-plus-totals report envelope.

**Architecture:** The CLI is a FastMCP client (ADR-0089). Each verb is one `Verb` registry entry plus a handler in `cli/commands/reads.py` that builds the tool payload, calls the tool, and renders. The report envelope carries per-row `items` and envelope `data` totals — a shape the existing `_list`/`_record` helpers each drop half of — so a new `render_report` in `cli/render.py` tables the rows and prints the projected totals as a footer. See [the spec](../specs/2026-06-25-ledger-report-cli-verbs-818.md) and [ADR-0250](../../adr/0250-ledger-report-cli-verbs.md).

**Tech Stack:** Python 3.14, `uv`, `pytest`, `ruff`, `ty`, `argparse`, FastMCP client.

## Global Constraints

- Ruff line length 100; lint set `E,F,I,UP,B,SIM`; `ty` strict (whole tree incl. `tests/`).
- Absolute imports only (no relative). Google-style docstrings on non-trivial public APIs.
- Guardrails before every commit: `just lint`, `just type`, and the focused tests
  (`uv run python -m pytest tests/cli/test_render.py tests/cli/test_read_verbs.py tests/cli/test_dispatch_wiring.py -q`).
- Doc-style guard: plain factual prose; never "Sprint", "critical", "robust", "comprehensive", "elegant".
- Conventional-commit subjects ≤72 chars, ending with the `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` trailer.
- No new MCP tool, schema, RBAC role, or migration. Verbs are `read_only=True`.

---

### Task 1: `render_report` — rows table + projected totals footer

**Files:**
- Modify: `src/kdive/cli/render.py` (add `render_report`)
- Test: `tests/cli/test_render.py`

**Interfaces:**
- Consumes: existing `render(rows, *, columns, as_json)` and `render_record(record, *, as_json)`.
- Produces: `render_report(rows: Sequence[Mapping[str, object]], totals: Mapping[str, object], *, columns: Sequence[str], total_columns: Sequence[str], as_json: bool) -> None`. JSON mode prints `{"items": [...projected rows...], "totals": {...projected totals...}}`; table mode prints the rows table, a blank line, then the projected totals as `render_record` key/value lines. Both halves are projected onto their declared key sets.

- [ ] **Step 1: Write the failing tests**

Add to `tests/cli/test_render.py`:

```python
from kdive.cli.render import render_report

_COLS = ["project", "reserved"]
_TCOLS = ["scope", "total_reserved"]


def test_render_report_json_emits_items_and_projected_totals(capsys) -> None:
    rows = [{"project": "p", "reserved": "1.0", "secret": "x"}]
    totals = {"scope": "all-projects", "total_reserved": "1.0", "extra": "drop-me"}
    render_report(rows, totals, columns=_COLS, total_columns=_TCOLS, as_json=True)
    parsed = json.loads(capsys.readouterr().out)
    assert parsed == {
        "items": [{"project": "p", "reserved": "1.0"}],
        "totals": {"scope": "all-projects", "total_reserved": "1.0"},
    }


def test_render_report_table_has_rows_then_totals_footer(capsys) -> None:
    rows = [{"project": "p", "reserved": "1.0"}]
    totals = {"scope": "all-projects", "total_reserved": "1.0"}
    render_report(rows, totals, columns=_COLS, total_columns=_TCOLS, as_json=False)
    out = capsys.readouterr().out
    assert "project" in out and "p" in out  # row table
    assert "scope" in out and "all-projects" in out  # totals footer
    assert "" in out.splitlines()  # blank separator line


def test_render_report_empty_rows_still_prints_header_and_totals(capsys) -> None:
    render_report([], {"scope": "all-projects"}, columns=_COLS, total_columns=["scope"], as_json=False)
    out = capsys.readouterr().out
    assert "project" in out and "scope" in out and "all-projects" in out


def test_render_report_json_missing_total_key_renders_null(capsys) -> None:
    render_report([], {}, columns=_COLS, total_columns=_TCOLS, as_json=True)
    parsed = json.loads(capsys.readouterr().out)
    assert parsed == {"items": [], "totals": {"scope": None, "total_reserved": None}}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run python -m pytest tests/cli/test_render.py -q -k render_report`
Expected: FAIL with `ImportError: cannot import name 'render_report'`.

- [ ] **Step 3: Implement `render_report`**

Append to `src/kdive/cli/render.py` (after `render_record`):

```python
def render_report(
    rows: Sequence[Mapping[str, object]],
    totals: Mapping[str, object],
    *,
    columns: Sequence[str],
    total_columns: Sequence[str],
    as_json: bool,
) -> None:
    """Render report rows as a table with a totals footer, or as ``{items, totals}`` JSON.

    Both halves are projected onto their declared key sets so the scriptable contract is
    stable against server-side envelope additions: a ``totals`` key not in ``total_columns``
    never reaches the output, and a missing one renders blank (table) or ``null`` (JSON),
    matching :func:`render` / :func:`render_record`.

    Args:
        rows: The per-row records; each is projected onto ``columns``.
        totals: The envelope totals; projected onto ``total_columns``.
        columns: The ordered row column keys.
        total_columns: The ordered totals keys.
        as_json: When ``True``, emit one ``{"items": [...], "totals": {...}}`` object.
    """
    projected_totals = {c: totals.get(c) for c in total_columns}
    if as_json:
        projected_rows = [{c: row.get(c) for c in columns} for row in rows]
        print(json.dumps({"items": projected_rows, "totals": projected_totals}, indent=2, default=str))
        return
    render(rows, columns=columns, as_json=False)
    print()
    render_record(projected_totals, as_json=False)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run python -m pytest tests/cli/test_render.py -q`
Expected: PASS (all render tests, old and new).

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type
git add src/kdive/cli/render.py tests/cli/test_render.py
git commit  # feat(cli): add render_report for rows-plus-totals report output
```

---

### Task 2: report verb handlers + payload helpers

**Files:**
- Modify: `src/kdive/cli/commands/reads.py` (add `import sys`, helpers, two handlers)
- Test: `tests/cli/test_read_verbs.py`

**Interfaces:**
- Consumes: `render_report` (Task 1); existing `_fetch`, `_rows`, `_payload`, `exit_code_for_envelope`.
- Produces: `ledger_report_all(args) -> int` (calls `accounting.report_all_projects`) and `ledger_report_granted(args) -> int` (calls `accounting.report_granted_set`). Payload helpers: `_window_payload(args) -> dict` (assembles `{"window": [since, until]}` or `{}`), `_projects_arg(args) -> list[str] | None` (comma-split or `None`), `_totals(envelope) -> dict`. Module constants `_REPORT_COLUMNS` and `_REPORT_TOTAL_COLUMNS`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/cli/test_read_verbs.py` (the `_collection`/`_item`/`_install_session`/`_args` helpers already exist there):

```python
def _report_collection(items: list[dict], totals: dict) -> dict:
    return {"object_id": "report", "status": "ok", "data": totals, "items": items}


def test_report_all_calls_tool_with_no_optional_args(monkeypatch, capsys) -> None:
    client = _install_session(monkeypatch, _report_collection([], {"scope": "all-projects"}))
    code = asyncio.run(reads.ledger_report_all(_args(group_by=None, since=None, until=None)))
    assert code == 0
    assert client.calls == [("accounting.report_all_projects", {})]


def test_report_all_assembles_window_and_group_by(monkeypatch, capsys) -> None:
    client = _install_session(monkeypatch, _report_collection([], {}))
    asyncio.run(
        reads.ledger_report_all(
            _args(group_by="principal", since="2026-01-01T00:00:00+00:00", until=None)
        )
    )
    assert client.calls == [
        (
            "accounting.report_all_projects",
            {"group_by": "principal", "window": ["2026-01-01T00:00:00+00:00", None]},
        )
    ]


def test_report_granted_splits_projects(monkeypatch, capsys) -> None:
    client = _install_session(monkeypatch, _report_collection([], {}))
    asyncio.run(
        reads.ledger_report_granted(_args(group_by=None, since=None, until=None, projects="a, b ,c"))
    )
    assert client.calls == [("accounting.report_granted_set", {"projects": ["a", "b", "c"]})]


def test_report_granted_omits_projects_when_absent(monkeypatch, capsys) -> None:
    client = _install_session(monkeypatch, _report_collection([], {}))
    asyncio.run(
        reads.ledger_report_granted(_args(group_by=None, since=None, until=None, projects=None))
    )
    assert client.calls == [("accounting.report_granted_set", {})]


def test_report_granted_all_empty_projects_is_usage_error(monkeypatch, capsys) -> None:
    client = _install_session(monkeypatch, _report_collection([], {}))
    code = asyncio.run(
        reads.ledger_report_granted(_args(group_by=None, since=None, until=None, projects=" , "))
    )
    assert code == 2
    assert client.calls == []  # rejected before any tool call
    assert "--projects" in capsys.readouterr().err


def test_report_renders_rows_and_totals_json(monkeypatch, capsys) -> None:
    items = [_item("p", "ok", {"project": "p", "principal": "", "reserved": "20", "reconciled": "-19", "variance": "1"})]
    totals = {
        "scope": "all-projects", "group_by": "", "project_count": "1",
        "total_project": "*", "total_principal": "",
        "total_reserved": "20", "total_reconciled": "-19", "total_variance": "1",
    }
    _install_session(monkeypatch, _report_collection(items, totals))
    asyncio.run(
        reads.ledger_report_all(argparse.Namespace(json=True, group_by=None, since=None, until=None))
    )
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["items"] == [
        {"project": "p", "principal": "", "reserved": "20", "reconciled": "-19", "variance": "1"}
    ]
    assert parsed["totals"]["total_reserved"] == "20" and parsed["totals"]["scope"] == "all-projects"


def test_report_all_denial_exits_authorization_denied(monkeypatch, capsys) -> None:
    _install_session(monkeypatch, _denied("report"))
    code = asyncio.run(
        reads.ledger_report_all(_args(group_by=None, since=None, until=None))
    )
    assert code == 3
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run python -m pytest tests/cli/test_read_verbs.py -q -k report`
Expected: FAIL with `AttributeError: module 'kdive.cli.commands.reads' has no attribute 'ledger_report_all'`.

- [ ] **Step 3: Implement the helpers and handlers**

In `src/kdive/cli/commands/reads.py`, add `import sys` to the imports, `render_report` to the render import, and append after `inventory_show`:

```python
_REPORT_COLUMNS = ["project", "principal", "reserved", "reconciled", "variance"]
_REPORT_TOTAL_COLUMNS = [
    "scope",
    "group_by",
    "project_count",
    "total_project",
    "total_principal",
    "total_reserved",
    "total_reconciled",
    "total_variance",
]


def _window_payload(args: argparse.Namespace) -> dict[str, object]:
    """Assemble ``{"window": [since, until]}`` from ``--since``/``--until``, or ``{}``.

    Sends no ``window`` key when both bounds are absent (server reports all time). When only
    one bound is given the other half of the pair is ``None`` (a half-open window). Values
    pass through verbatim; the tool's parser owns ISO-8601/timezone validation.
    """
    since = getattr(args, "since", None)
    until = getattr(args, "until", None)
    if since is None and until is None:
        return {}
    return {"window": [since, until]}


def _projects_arg(args: argparse.Namespace) -> list[str] | None:
    """Comma-split ``--projects`` into a name list, or ``None`` when the flag is absent.

    Whitespace is trimmed and empty tokens dropped. A given-but-all-empty value yields an
    empty list, which the caller rejects as a usage error rather than sending ``projects=[]``.
    """
    raw = getattr(args, "projects", None)
    if raw is None:
        return None
    return [name.strip() for name in raw.split(",") if name.strip()]


def _totals(envelope: Mapping[str, object]) -> dict[str, object]:
    data = envelope.get("data")
    return {str(k): v for k, v in data.items()} if isinstance(data, Mapping) else {}


async def _report(name: str, args: argparse.Namespace, payload: Mapping[str, object]) -> int:
    envelope = await _fetch(name, payload)
    render_report(
        _rows(envelope),
        _totals(envelope),
        columns=_REPORT_COLUMNS,
        total_columns=_REPORT_TOTAL_COLUMNS,
        as_json=args.json,
    )
    return exit_code_for_envelope(envelope)


async def ledger_report_all(args: argparse.Namespace) -> int:
    """Platform-wide accounting rollup (``accounting.report_all_projects``; auditor-gated)."""
    payload = _payload(args, "group_by")
    payload.update(_window_payload(args))
    return await _report("accounting.report_all_projects", args, payload)


async def ledger_report_granted(args: argparse.Namespace) -> int:
    """Granted-project accounting rollup (``accounting.report_granted_set``)."""
    names = _projects_arg(args)
    if names == []:
        print("error: --projects was given but listed no project names", file=sys.stderr)
        return 2
    payload = _payload(args, "group_by")
    if names is not None:
        payload["projects"] = names
    payload.update(_window_payload(args))
    return await _report("accounting.report_granted_set", args, payload)
```

Update the render import line at the top of the file from
`from kdive.cli.render import render, render_record` to
`from kdive.cli.render import render, render_record, render_report`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run python -m pytest tests/cli/test_read_verbs.py -q -k report`
Expected: PASS.

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type
git add src/kdive/cli/commands/reads.py tests/cli/test_read_verbs.py
git commit  # feat(cli): add ledger report-all/report-granted handlers
```

---

### Task 3: register the verbs + per-verb help text

**Files:**
- Modify: `src/kdive/cli/commands/registry.py` (`Verb.help` field, two `REGISTRY` entries, `add_parser(help=...)`)
- Test: `tests/cli/test_read_verbs.py`, `tests/cli/test_dispatch_wiring.py`

**Interfaces:**
- Consumes: `reads.ledger_report_all`, `reads.ledger_report_granted` (Task 2).
- Produces: two `Verb` entries (`("ledger", "report-all", …)`, `("ledger", "report-granted", …)`), both `read_only=True`. A new optional `Verb.help: str = ""` field wired into `_verb_parser`'s `add_parser`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/cli/test_read_verbs.py`:

```python
def test_report_verbs_are_registered_and_read_only() -> None:
    by_path = {(v.group, v.sub): v for v in REGISTRY}
    all_v = by_path[("ledger", "report-all")]
    granted = by_path[("ledger", "report-granted")]
    assert all_v.tool == "accounting.report_all_projects" and all_v.read_only
    assert granted.tool == "accounting.report_granted_set" and granted.read_only
    assert all_v.options == ("group_by", "since", "until")
    assert granted.options == ("projects", "group_by", "since", "until")
    assert "platform_auditor" in all_v.help  # help notes the required role
```

Add to `tests/cli/test_dispatch_wiring.py` (the file already imports `build_parser`; the
existing `test_every_registry_verb_parses_through_the_built_parser` auto-covers the new verbs'
basic parse — these add the option-specific assertions):

```python
def test_report_all_parses_window_and_group_by() -> None:
    args = build_parser().parse_args(
        ["ledger", "report-all", "--group-by", "principal", "--since", "2026-01-01T00:00:00+00:00"]
    )
    assert args.command == "ledger" and args.subcommand == "report-all"
    assert args.group_by == "principal"
    assert args.since == "2026-01-01T00:00:00+00:00" and args.until is None


def test_report_granted_parses_projects_flag() -> None:
    args = build_parser().parse_args(["ledger", "report-granted", "--projects", "a,b"])
    assert args.subcommand == "report-granted" and args.projects == "a,b"


def test_report_all_rejects_projects_flag() -> None:
    # report-all has no --projects (only report-granted scopes a subset).
    with pytest.raises(SystemExit):
        build_parser().parse_args(["ledger", "report-all", "--projects", "a,b"])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run python -m pytest tests/cli/test_read_verbs.py tests/cli/test_dispatch_wiring.py -q -k report`
Expected: FAIL (`KeyError: ('ledger', 'report-all')` and/or argparse error on the unknown subcommand).

- [ ] **Step 3: Add the `help` field and the registry entries**

In `src/kdive/cli/commands/registry.py`, add the field to the `Verb` dataclass (after `read_only`):

```python
    help: str = ""
```

Add the two entries to `REGISTRY` immediately after the existing `("ledger", "show", …)` entry:

```python
    Verb(
        "ledger",
        "report-all",
        reads.ledger_report_all,
        "accounting.report_all_projects",
        options=("group_by", "since", "until"),
        help="platform-wide accounting rollup (requires a platform_auditor token)",
    ),
    Verb(
        "ledger",
        "report-granted",
        reads.ledger_report_granted,
        "accounting.report_granted_set",
        options=("projects", "group_by", "since", "until"),
        help="accounting rollup across your granted projects",
    ),
```

Wire the help into `_verb_parser` — change its `add_parser` call from
`parser = group_parser.add_parser(verb.sub, parents=[parent])` to:

```python
    parser = group_parser.add_parser(verb.sub, parents=[parent], help=verb.help or None)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run python -m pytest tests/cli/test_read_verbs.py tests/cli/test_dispatch_wiring.py -q`
Expected: PASS (including the existing `test_handler_calls_the_tool_the_registry_declares` parametrization, which now also covers the two new verbs).

- [ ] **Step 5: Run the read-only gate test**

Run: `uv run python -m pytest tests/mcp/test_read_tools_annotated.py -q`
Expected: PASS — both new verbs declare `readOnlyHint`-annotated tools.

- [ ] **Step 6: Guardrails + commit**

```bash
just lint && just type
git add src/kdive/cli/commands/registry.py tests/cli/test_read_verbs.py tests/cli/test_dispatch_wiring.py
git commit  # feat(cli): register ledger report-all/report-granted verbs
```

---

### Task 4: document the verbs in the `kdivectl` runbook

**Files:**
- Modify: `docs/operating/runbooks/kdivectl.md`

**Interfaces:** none (prose only).

- [ ] **Step 1: Add the verbs to the "Read verbs" list**

In `docs/operating/runbooks/kdivectl.md`, in the read-verb code block (the one ending `kdivectl inventory show [--project <project>]`), add after the `ledger show` line:

```text
kdivectl ledger report-all [--group-by principal] [--since <ts>] [--until <ts>]
kdivectl ledger report-granted [--projects a,b] [--group-by principal] [--since <ts>] [--until <ts>]
```

- [ ] **Step 2: Add a paragraph after the `--project` paragraph**

Add prose covering: `report-all` maps to `accounting.report_all_projects` and needs a
`platform_auditor` token (it appears in the platform-axis row of the authorization matrix);
`report-granted` maps to `accounting.report_granted_set` and rolls up the caller's granted
projects, with `--projects a,b` narrowing to a named subset; `--since`/`--until` are
timezone-aware ISO-8601 bounds forming a half-open window (omit both for all time), validated
server-side; a given-but-empty `--projects` is a usage error (exit `2`); both render the rows
table with a totals footer (or `{"items": [...], "totals": {...}}` under `--json`).

Also add `accounting.report` is already named in the platform-axis matrix row (line ~121);
verify the new verbs are consistent with it — `report-all` is the `platform_auditor` read,
`report-granted` is the per-project member read (no platform role).

- [ ] **Step 3: Run the doc guardrails**

```bash
just adr-status-check
/opt/homebrew/bin/bash ./scripts/check-doc-links.sh   # macOS bash-3.2 lacks mapfile; use bash 4+
```
Expected: links resolve; ADR index in sync.

- [ ] **Step 4: Commit**

```bash
git add docs/operating/runbooks/kdivectl.md
git commit  # docs(runbook): document ledger report-all/report-granted verbs
```

---

## Self-Review

**Spec coverage:**
- Argument→payload mapping (group_by / window / projects) → Task 2 helpers + tests.
- Window half-open + omit-when-absent → Task 2 `_window_payload` + tests.
- `--projects` split + all-empty usage error (exit 2) → Task 2 `_projects_arg` + handler + test.
- Rendering rows + projected totals; `--json` `{items, totals}` → Task 1 `render_report` + tests; Task 2 wiring + test.
- Exit codes (denial → 3, server-driven) → Task 2 denial test.
- Help text notes auditor role → Task 3 `Verb.help` + test.
- Registry `read_only=True` + read-only gate → Task 3 entries + gate test.
- Runbook docs → Task 4.

**Placeholder scan:** none — every code step shows complete code.

**Type consistency:** `render_report(rows, totals, *, columns, total_columns, as_json)` is defined in Task 1 and called with exactly those kwargs in Task 2 `_report`. `_projects_arg → list[str] | None`, `_window_payload → dict`, `_totals → dict` are defined and used consistently. Registry `Verb.help` field defined in Task 3 and read by the Task 3 test.
