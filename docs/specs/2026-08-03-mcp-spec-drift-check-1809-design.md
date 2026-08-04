# Design — detect MCP spec and protocol-version drift (#1809)

- **Issue:** [#1809](https://github.com/randomparity/kdive/issues/1809)
- **ADR:** [0537](../adr/0537-mcp-protocol-version-drift-two-clocks.md)
- **Date:** 2026-08-03

## Requirement

KDIVE advertises an MCP protocol version that is a property of its pinned dependencies, not
of its own source. `mcp==1.28.1` reports `mcp.types.LATEST_PROTOCOL_VERSION == "2025-11-25"`,
and no file in the repository states that, asserts it, or notices when it changes. Upstream
has since published `schema/2026-07-28`, unremarked.

Acceptance criteria, from the issue:

1. A dependency bump that changes the advertised protocol version fails CI until a person
   edits a KDIVE-owned constant.
2. The offline gate runs without network access, like the other `ci` guards.
3. A newly published upstream revision opens or updates a tracking issue within a week,
   without gating any PR on network reachability.
4. Exactly one agent-facing doc states the negotiated revision, and it cannot go stale.
5. The version-comparison logic is unit-tested against a synthetic listing, with no network.

Out of scope: bumping the protocol version, and vendoring the specification schema
(ADR-0537 §Considered & rejected).

## Mechanism

### The declared constant

`src/kdive/mcp/__init__.py` is empty today and becomes the single declaration:

```python
MCP_PROTOCOL_VERSION = "2025-11-25"
```

Everything else in this design reads that name. It is what makes a protocol-version change a
reviewable edit in `src/kdive` rather than a silent consequence of `uv lock`.

### `scripts/check_mcp_spec_version.py`

Two modes on one script, so the declared constant is read from one place, with three exit
codes so the caller can tell drift from breakage:

| mode | network | asserts | exit 0 | exit 1 | exit 2 |
| --- | --- | --- | --- | --- | --- |
| default | no | `mcp.types.LATEST_PROTOCOL_VERSION == MCP_PROTOCOL_VERSION` | equal | differ | import fails |
| `--upstream` | yes | no upstream revision exceeds `MCP_PROTOCOL_VERSION` | none newer | newer exists | fetch/parse fails |

Separating exit 2 is the one place this design departs from `rusty-imap-mcp`'s script, and it
is deliberate. There, `refresh-mcp-spec.sh` exits non-zero on drift *and* on a `curl` failure,
invalid JSON, or a missing vendored file, and `mcp-spec-drift.yml` treats every non-zero exit
as drift — so a transient network fault files an issue reporting drift that does not exist.
Operational failure must be loud and must not file a drift issue.

`--upstream` fetches `https://api.github.com/repos/modelcontextprotocol/modelcontextprotocol/contents/schema`
with `urllib.request` (stdlib; no new dependency), sending `Authorization: Bearer $GITHUB_TOKEN`
when that variable is set — unauthenticated GitHub API calls are limited to 60/hour per IP and
Actions runners share addresses.

The comparison is a pure function, which is what makes criterion 5 reachable:

```python
def newer_revisions(entries: Iterable[str], declared: str) -> list[str]:
    """Published revisions strictly newer than ``declared``, oldest first."""
```

It keeps entries matching `^\d{4}-\d{2}-\d{2}$` — dropping `draft`, and any future
`YYYY-MM-DD-<suffix>` form, neither of which is a released revision KDIVE could adopt — and
compares as strings. ISO-8601 dates sort lexicographically, so no date parsing is involved and
no timezone can enter.

On drift the script writes one human-readable report to stdout naming the declared version,
every newer revision, and the remediation. It writes it once: the workflow tees that single
run rather than re-fetching to build an issue body.

### The `ci` wiring

```
mcp-spec-check:
    uv run python scripts/check_mcp_spec_version.py
```

appended to the `ci` recipe's gate list. Default (offline) mode only — `--upstream` never runs
in `ci`. The script imports `mcp.types` and `kdive.mcp`, so it runs under `uv run` like
`env-docs-check`, not as bare `python3` like the stdlib-only guards.

### The guarded doc statement

`docs/guide/agent-index.md` gains one sentence naming the negotiated revision, bound in
`scripts/gen_doc_constants.py` as a fourth binding with `writable=False`:

| field | value |
| --- | --- |
| `label` | `MCP protocol version` |
| `path` | `docs/guide/agent-index.md` |
| `pattern` | `MCP protocol revision (\d{4}-\d{2}-\d{2})` |
| `expected` | `MCP_PROTOCOL_VERSION` |
| `writable` | `False` |

Guarded rather than generated because the sentence carries prose a generator must not rewrite,
matching the upload-ceiling and CLI-wait bindings. It is picked up by the existing
`doc-constants-check` gate, so no new `ci` entry is needed for it.

This binding must not be pointed at `docs/adr/0268-tool-gateway-dispatcher.md:27` or
`docs/design/2026-06-27-tool-gateway-dispatcher-866.md:34`. Both cite "MCP spec (2025-06-18)"
as the revision that established the `tools/list` rule — a historical fact, not a claim about
what KDIVE negotiates. Guarding them would make the check rewrite an accepted decision, which
`docs/adr/README.md:20` forbids.

### `.github/workflows/mcp-spec-drift.yml`

Weekly cron plus `workflow_dispatch`, following `test-ordering.yml`'s conventions
(SHA-pinned actions, `persist-credentials: false`, a `concurrency` group, a comment stating
why the job exists). `permissions: contents: read, issues: write`.

Steps: check out, install `libvirt-dev`, `uv sync --locked`, run `--upstream` capturing
stdout and the exit code, then branch on it —

- **0** — succeed, do nothing.
- **1** — open an issue titled `MCP spec drift: upstream <newest> is newer than pinned <declared>`,
  or comment on the existing open one found by that title with `--search`. Labels
  `area:mcp-api`, `type:chore`, `status:needs-triage`. Then exit 1 so the badge reflects it.
- **2** — exit 2 without filing anything. The run is red because the check could not run.

The title embeds the version pair, so a second published revision produces a new issue rather
than a comment on a stale one, and `cancel-in-progress: false` keeps a slow run from being
killed mid-file.

## Tests

`tests/scripts/test_check_mcp_spec_version.py`, alongside `test_check_env_documented.py`:

- **`test_newer_revisions_finds_a_later_published_revision`** — the acceptance test for
  criterion 3, over the real current listing shape (`2024-11-05`, `2025-03-26`, `2025-06-18`,
  `2025-11-25`, `2026-07-28`, `draft`) against declared `2025-11-25`, expecting exactly
  `["2026-07-28"]`.
- **`test_newer_revisions_ignores_draft_and_suffixed_entries`** — `draft` and a
  `2026-09-01-rc` entry are both dropped, so a pre-release cannot file an issue KDIVE could
  not act on. This is the mutation control on the filter: a comparison without it returns
  `draft` as newest, since `d` sorts above any digit.
- **`test_newer_revisions_is_empty_when_declared_is_newest`** and
  **`test_newer_revisions_is_empty_on_an_equal_revision`** — the quiet paths, so the cron does
  not file on parity. The equal case is the boundary a `>=` would break.
- **`test_offline_mode_passes_when_the_pinned_library_agrees`** — the gate green against the
  real pin, which is the fixture.
- **`test_offline_mode_fails_and_names_both_versions`** — monkeypatch
  `mcp.types.LATEST_PROTOCOL_VERSION` to a different value; assert exit 1 and that the message
  carries the declared value, the library value, and the remediation. Criterion 1.
- **`test_upstream_mode_exits_2_on_a_fetch_failure`** — the fetch raises `URLError`; assert
  exit 2, not 1, and that the report says nothing about drift. This is the test that pins the
  departure from the reference script, and the one that keeps a network blip from filing a
  false drift issue.

No test performs network I/O: `newer_revisions` is pure, and the two `--upstream` tests inject
the fetch. The guarded doc binding is covered by the existing `doc-constants-check` gate rather
than a new test — pointing the binding at a doc whose sentence does not match fails that gate,
which is the assertion.
