# Design — detect MCP spec and protocol-version drift (#1809)

- **Issue:** [#1809](https://github.com/randomparity/kdive/issues/1809)
- **ADR:** [0537](../adr/0537-mcp-protocol-version-drift-two-clocks.md)
- **Date:** 2026-08-03

## Requirement

KDIVE advertises an MCP protocol range that is a property of its pinned dependencies, not of
its own source. At `mcp==1.28.1` that range is
`SUPPORTED_PROTOCOL_VERSIONS == ['2024-11-05', '2025-03-26', '2025-06-18', '2025-11-25']`
with `LATEST_PROTOCOL_VERSION == '2025-11-25'`, and no file in the repository states it,
asserts it, or notices when it changes. Upstream has since published `schema/2026-07-28`,
unremarked.

Acceptance criteria, from the issue:

1. A dependency bump that changes the advertised protocol range fails CI until a person
   edits a KDIVE-owned constant.
2. The offline gate runs without network access, like the other `ci` guards.
3. A newly published upstream revision opens a tracking issue within a week, without gating
   any PR on network reachability.
4. Exactly one agent-facing doc states the negotiated revision, and it cannot go stale.
5. The version-comparison logic is unit-tested against a synthetic listing, with no network.

Out of scope: bumping the protocol version, and vendoring the specification schema
(ADR-0537 §Considered & rejected).

## Mechanism

### The declared constants

`src/kdive/mcp/__init__.py` is empty today and becomes the single declaration of both ends
of the compatibility surface:

```python
MCP_PROTOCOL_VERSION = "2025-11-25"
MCP_SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
```

Both are needed because `mcp` does not advertise a scalar. `mcp/server/session.py` answers
`initialize` with the client's requested version whenever it is in
`SUPPORTED_PROTOCOL_VERSIONS`, and `mcp/server/streamable_http.py` rejects against the same
list; `LATEST_PROTOCOL_VERSION` is only the fallback. Asserting the ceiling alone is blind in
the breaking direction — a release that drops `2024-11-05` leaves the ceiling untouched while
a pinned agent client stops connecting.

### `scripts/check_mcp_spec_version.py`

Two modes on one script, so the declared constants are read from one place, with three exit
codes so the caller can tell drift from breakage:

| mode | network | asserts | exit 0 | exit 1 | exit 2 |
| --- | --- | --- | --- | --- | --- |
| default | no | `LATEST_PROTOCOL_VERSION` and `tuple(SUPPORTED_PROTOCOL_VERSIONS)` equal the declared constants | equal | differ | anything else |
| `--upstream` | yes | no upstream revision exceeds `MCP_PROTOCOL_VERSION` | none newer | newer exists | anything else |

**Exit 1 means "drift determined", and nothing else.** `main()` wraps its body in a
top-level guard that maps any exception other than `SystemExit`/`KeyboardInterrupt` to exit 2
after printing the traceback. Without it a `KeyError` on a changed API response shape, a
decode error, or a bug in the report renderer would exit 1 — Python's default — and the
workflow would file an issue announcing drift it never measured.

This is the design's one deliberate departure from `rusty-imap-mcp`, and the guard is what
makes it real. There, `refresh-mcp-spec.sh` exits non-zero on drift *and* on a `curl` failure,
invalid JSON, or a missing vendored file, and `mcp-spec-drift.yml` treats every non-zero exit
as drift — so a transient network fault files an issue reporting drift that does not exist.
Drift is the value that must be proven; everything else defaults to not filing.

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

### The `ci` wiring — two places, not one

```
mcp-spec-check:
    uv run python scripts/check_mcp_spec_version.py
```

appended to the `ci` recipe's gate list, **and** a dedicated step in
`.github/workflows/ci.yml`:

```yaml
      - name: MCP protocol-version drift
        # CI invokes recipes individually (never `just ci`), so list this explicitly to gate PRs.
        run: just mcp-spec-check
```

The second is the one that satisfies criterion 1. No workflow in this repository invokes
`just ci`; `ci.yml` runs each recipe as its own step and records that hazard in eleven
separate comments. ADR-0410 hit it already — `doc-constants-check` is "added to the `ci`
umbrella and, because CI invokes recipes individually, listed as its own `ci.yml` step".
Adding the recipe to `just ci` alone would gate a local run and nothing else.

Default (offline) mode only — `--upstream` never runs in `ci`. The script imports `mcp` and
`kdive.mcp`, so it runs under `uv run` like `env-docs-check`, not as bare `python3` like the
stdlib-only guards.

### The generated doc statement

`docs/guide/agent-index.md` gains one sentence naming the negotiated ceiling, bound in
`scripts/gen_doc_constants.py` as a fourth binding:

| field | value |
| --- | --- |
| `label` | `MCP protocol version` |
| `path` | `docs/guide/agent-index.md` |
| `pattern` | `MCP protocol revision (\d{4}-\d{2}-\d{2})` |
| `expected` | `MCP_PROTOCOL_VERSION` |
| `writable` | `True` |

`writable=True`, matching the tool-count binding already in that same file. ADR-0410 scopes
the guarded kind by file kind, not by prose nuance — "a generator must not rewrite
hand-authored source docstrings (the repo's generators only ever write `docs/`)" — and both
existing guarded bindings target `.py` files. `write()` substitutes capture group 1 alone
(`match.group(0).replace(match.group(1), expected, 1)`), so it provably cannot disturb the
sentence around the date. A guarded binding here would instead make `just doc-constants` print
`wrote 4 doc constants.` while silently skipping this one, leaving `doc-constants-check` red
until the contributor found and hand-edited the sentence.

It is picked up by the existing `doc-constants-check` gate, so no new `ci` entry is needed for
it.

This binding must not be pointed at `docs/adr/0268-tool-gateway-dispatcher.md:27` or
`docs/design/2026-06-27-tool-gateway-dispatcher-866.md:34`. Both cite "MCP spec (2025-06-18)"
as the revision that established the `tools/list` rule — a historical fact, not a claim about
what KDIVE negotiates. Binding them to the live constant would fail the gate permanently from
the next bump onward, and clearing it would mean editing a historical citation;
`docs/adr/README.md:20` forbids that outright for the ADR.

### `.github/workflows/mcp-spec-drift.yml`

Weekly cron plus `workflow_dispatch`, following `test-ordering.yml`'s conventions
(SHA-pinned actions, `persist-credentials: false`, a `concurrency` group, a comment stating
why the job exists). `permissions: contents: read, issues: write`.

Steps: check out, install `libvirt-dev`, `uv sync --locked`, run `--upstream` capturing
stdout and the exit code, then branch on it —

| exit | action | job result |
| --- | --- | --- |
| 0 | nothing | pass |
| 1, no matching open issue | open one titled `MCP spec drift: upstream <newest> is newer than pinned <declared>`, labels `area:mcp-api`, `type:chore`, `status:needs-triage` | fail |
| 1, matching open issue exists | nothing — the open issue is the report | pass |
| 2 | file nothing | fail |
| anything else | file nothing | fail |

The idempotent arm is what keeps the badge meaningful. Upstream `2026-07-28` already exceeds
the declared `2025-11-25` and will keep doing so until the `mcp` 2.0.0 bump is scheduled, so
drift is the expected state for months, not a transient. A job that filed or commented every
week would hold the badge red permanently and add ~52 identical comments a year — and an
actually-broken check (changed API shape, expired token scope, failed `uv sync`) would look
exactly the same. With the idempotent arm, red means newly-detected drift or a check that
could not run.

The title embeds the version pair, so a second published revision does not match the open
issue's title and correctly opens a new one. `cancel-in-progress: false` keeps a slow run from
being killed mid-file.

## Tests

`tests/scripts/test_check_mcp_spec_version.py`, alongside `test_check_env_documented.py`:

- **`test_newer_revisions_finds_a_later_published_revision`** — over the real current listing
  shape (`2024-11-05`, `2025-03-26`, `2025-06-18`, `2025-11-25`, `2026-07-28`, `draft`)
  against declared `2025-11-25`, expecting exactly `["2026-07-28"]`. This covers criterion 5,
  not criterion 3 — it exercises a pure list comparison and asserts nothing about an issue
  being filed.
- **`test_newer_revisions_ignores_draft_and_suffixed_entries`** — `draft` and a
  `2026-09-01-rc` entry are both dropped, so a pre-release cannot file an issue KDIVE could
  not act on. This is the mutation control on the filter: a comparison without it returns
  `draft` as newest, since `d` sorts above any digit.
- **`test_newer_revisions_is_empty_when_declared_is_newest`** and
  **`test_newer_revisions_is_empty_on_an_equal_revision`** — the quiet paths, so the cron does
  not file on parity. The equal case is the boundary a `>=` would break.
- **`test_offline_mode_passes_when_the_pinned_library_agrees`** — the gate green against the
  real pin, which is the fixture.
- **`test_offline_mode_fails_when_the_ceiling_moves`** — monkeypatch
  `mcp.types.LATEST_PROTOCOL_VERSION`; assert exit 1 and that the message carries the declared
  value, the library value, and the remediation. Criterion 1.
- **`test_offline_mode_fails_when_a_supported_revision_is_dropped`** — monkeypatch
  `SUPPORTED_PROTOCOL_VERSIONS` to drop `2024-11-05` while leaving the ceiling at
  `2025-11-25`; assert exit 1. This is the test the first draft of this design would have
  failed: it is the client-breaking direction, and a ceiling-only assertion passes it green.
- **`test_offline_mode_makes_no_network_call`** — run the default mode with
  `socket.socket` monkeypatched to raise, and assert exit 0. Criterion 2, which is otherwise
  a property of the tests rather than of the gate: without this, a future network call in the
  offline path would first fail on an air-gapped or rate-limited CI run.
- **`test_upstream_mode_exits_2_on_a_fetch_failure`** — the fetch raises `URLError`; assert
  exit 2, not 1, and that the report says nothing about drift.
- **`test_upstream_mode_exits_2_on_an_unexpected_exception`** — the fetch returns a payload
  whose shape breaks the reader (a `KeyError`); assert exit 2. Separate from the `URLError`
  case because it is the top-level guard under test, not the handled-network path, and
  without the guard this returns Python's default exit 1 and files a false drift issue.

No test performs network I/O: `newer_revisions` is pure, and the `--upstream` tests inject
the fetch.

Two things are verified by running them rather than by a unit test, and both are done before
merge and reported:

- **Criterion 3** — a `workflow_dispatch` run of `mcp-spec-drift.yml` on the branch. It
  exercises the real `gh` invocation, the title format, the three labels, and the
  search-by-title dedup, none of which any unit test reaches. The run is expected to open the
  `2026-07-28` issue; a second dispatch must find it and pass without commenting, which is the
  idempotence check.
- **Criterion 4** — the existing `doc-constants-check` gate. Pointing the binding at a doc
  whose sentence does not match fails it, which is the assertion. Note this gate cannot run on
  macOS: `gen_doc_constants` imports provider composition, which resolves the Linux-only
  `fallocate` symbol at import time, so it is verified in a Linux container.
