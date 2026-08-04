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
| `--upstream` | yes | the listing is recognizable, and no revision in it exceeds `MCP_PROTOCOL_VERSION` | none newer | newer exists | anything else |

Both values are read as **module attributes at call time** —
`mcp.types.LATEST_PROTOCOL_VERSION` and `mcp.shared.version.SUPPORTED_PROTOCOL_VERSIONS`,
never `from ... import`. This is load-bearing for the tests rather than for the gate:
`shared/version.py` does `from mcp.types import LATEST_PROTOCOL_VERSION` and materializes
`SUPPORTED_PROTOCOL_VERSIONS` with that value already substituted, at import. A script holding
its own `from`-bound name would not see either monkeypatch, and the two offline-failure tests
below would fail while the gate silently kept asserting against the real pin.

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

**The filter needs a floor, not just a ceiling.** Before comparing, `--upstream` requires
`MCP_PROTOCOL_VERSION` itself to appear among the recognized entries; if it does not, the
listing is not the document this check knows how to read, and the script prints that and exits
2. Without the floor, any upstream restructuring that still returns HTTP 200 — a `v` prefix, a
nested `schema/versions/<date>/` layout, a move to a sibling directory — leaves every entry
failing the filter, `newer_revisions` returning `[]`, and the job passing green permanently.
The exit-2 guard does not cover that on its own, because an empty filtered list is not an
exception; it is an ordinary successful-looking result. This repository has already named that
failure class — ADR-0518: "A guard that reports success over nothing is worse than no guard,
because it also retires the attention that would have caught the problem."

On drift the script writes one human-readable report to stdout naming the declared version,
every newer revision, and the remediation. That goes to the job log and nowhere else — the
workflow does not parse it.

Everything the workflow needs arrives machine-readably instead, appended to `$GITHUB_OUTPUT`
when that variable is set: `newest=<v>`, `declared=<v>`, and `newer=<comma-joined>`. **The
workflow builds the issue title and body only from those, and never from the prose.** This is
the one interface where exactness is load-bearing: the title carries the dedup key, so a later
rewording of the human report must not be able to change it. Scraping with `grep`/`sed` would
let an empty capture produce `MCP spec drift: upstream  not yet adopted`, which matches no
existing issue and refiles every week.

`newer` carries the whole list, not just the ceiling, because only one issue is filed per run
and it is keyed on the newest. With `2026-07-28` and a later `2026-11-01` both published
between crons, the run detects both, files one issue for `2026-11-01`, and without this output
`2026-07-28` would be named nowhere. The body lists every unadopted revision.

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
it — but a second gate stacks on top. `docs/guide/agent-index.md` is an ADR-0151 doc resource:
`resources/registrar.py` registers it in `DOC_RESOURCES`, and `gen_doc_resources.py` mirrors it
byte-for-byte into `src/kdive/mcp/resources/_content/agent-index.md`, which
`resources-docs-check` diffs. The date therefore lives in **two** committed files, and
`just doc-constants` rewrites only the canonical one. Every rewrite must be followed by
`just resources-docs` and the regenerated snapshot committed, or `resources-docs-check` goes
red — including on the implementing PR, which is the first to add the sentence. ADR-0410
records this same stacking for the tool-count binding in this same file:
"`resources-docs` then re-mirrors the served snapshot, so the existing `resources-docs-check`
stacks on top." Note the recipe is `just resources-docs`; `just docs` regenerates only the tool
reference.

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
stdout and the exit code, then branch on it. Both the `--upstream` step and the issue-filing
step carry `env: GITHUB_TOKEN: ${{ github.token }}` — Actions does not export it into step
environments on its own, so without it the script's `Authorization` header is inert (the
rate-limit mitigation never applies) and `gh` has no credential at all. This is the
repository's first issue-filing workflow, so there is no prior art to inherit the wiring from;
`test-ordering.yml`, the template for everything else here, is `contents: read` with no token.

The dedup search runs over **all** issue states, and the title is built from the script's
`$GITHUB_OUTPUT` values:

| exit | matching issue | action | job result |
| --- | --- | --- | --- |
| 0 | — | nothing | pass |
| 1 | none | open one titled `MCP spec drift: upstream <newest> not yet adopted`, labels `area:mcp-api`, `type:chore`, `status:needs-triage` | fail |
| 1 | open | nothing — the open issue is the report | pass |
| 1 | closed | nothing — the close is a human acknowledgement | pass |
| 1, empty `newest` | — | file nothing | fail |
| 2 | — | file nothing | fail |
| anything else | — | file nothing | fail |

The `empty newest` row is not redundant with exit 2. `exit_code` is whatever the process
returned, and `check_upstream()` is the only writer of the `$GITHUB_OUTPUT` values, so an exit 1
from any *other* cause — an interpreter error, a failed `uv run` — carries no revision. Filing
on it would produce an issue titled `MCP spec drift: upstream  not yet adopted`, and the
exact-title dedup would then make that malformed title permanent. The workflow requires a
non-empty `newest` in the filing step's `if:` and re-checks it in the shell.

Two of these rows pass while a non-zero exit stands, so the failing step cannot be conditioned
on `exit_code != '0'`. The filing step reports whether it filed, and the failing step fires
unless that says `false`.

The idempotent arm is what keeps the badge meaningful. Upstream `2026-07-28` already exceeds
the declared `2025-11-25` and will keep doing so until the `mcp` 2.0.0 bump is scheduled, so
drift is the expected state for months, not a transient. A job that filed or commented every
week would hold the badge red permanently and add ~52 identical comments a year — and an
actually-broken check (changed API shape, expired token scope, failed `uv sync`) would look
exactly the same. With the idempotent arm, red means newly-detected drift or a check that
could not run.

The closed row matters as much as the open one, and searching open-only would omit it. Closing
the tracking issue is the ordinary disposition once the work is folded into an `mcp` 2.0.0
epic or recorded as a deferral under `docs/debt/` — and an open-only search would then find no
match, refile a duplicate, and fail the job every week thereafter. That is the ~52-per-year
outcome this arm exists to prevent, in a worse form than the commenting design it rejects. The
predicate is *a human has seen this*, not *an issue is open*.

**The dedup key is the upstream revision, not the title.** The workflow runs
`gh issue list --state all --search "<newest> in:title" --json number,title` and selects hits
whose title contains the revision.

**No `--label` conjunct.** `gh` ANDs a label filter into the query, which hides any issue
tracking the revision under a different label set — including a human-filed one. Measured
today: issue #1485, *"Investigate MCP 2026-07-28 Spec Update Requirements"*, is open and
carries `type:feature, priority:P2, effort:L, status:needs-triage, risk:daytime-only` but not
`area:mcp-api`. The label-filtered query returns nothing for it; the unfiltered one returns it.
With the conjunct, this workflow's very first run would have opened a duplicate of an existing
open tracking issue — the exact outcome the idempotent arm exists to prevent. The labels still
go on `gh issue create`; they must not go on the search. An unrelated issue whose title happens
to contain a specific ISO date is the far cheaper false positive: one skipped filing, visible
in the log.

Measured behaviour, against this repository with gh 2.96.0: GitHub issue search **ANDs** its
terms. Searching a real issue's exact title returns that one issue; replacing any single token
with a nonsense token returns zero hits, as does a title differing only in its date. So a
near-miss does not silently match the previous revision's issue — the `contains` select is
defence-in-depth against tokenization surprises, not the fix for a relevance-ranking failure.
(Shell-quoting the search string, incidentally, only keeps it one argv element; it does not
escape the colon for GitHub's query parser. The colon is simply harmless here. Literal matching
would need `--search '"<title>" in:title'`.)

Keying on the revision rather than the whole title is what makes the arm match its own
predicate — *a human has seen this revision*. A maintainer retitling the issue during triage,
say to fold it under an `mcp` 2.0.0 epic, would break a full-title key in both halves at once
and file a duplicate for a revision someone had demonstrably already seen. A retitle is
stronger evidence of attention than a close, and the design already honours a close via
`--state all`. The label narrows the candidate set so an unrelated issue that merely mentions
the date cannot match.

For the same reason the title carries the revision alone and not the version pair: with the
`2026-07-28` issue open against declared `2025-11-25`, a partial bump to an intermediate
revision edits the declared constant — the whole point of the offline gate — and a pair-keyed
title would match nothing and open a second issue for the same unadopted revision. `declared=`
stays in the issue body, which the script's human report already writes. A genuinely new
upstream revision still changes `<newest>` and correctly opens a new issue.
`cancel-in-progress: false` keeps a slow run from being killed mid-file.

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
- **`test_upstream_mode_exits_2_when_the_listing_has_no_recognized_revisions`** — the fetch
  returns a plausibly-restructured listing (`v2026-07-28`, `versions`, `draft`); assert exit 2,
  **not 0**. This is the sanity-floor test, and it is the one that separates "no drift" from
  "could not read the listing" — the state that would otherwise stay green forever.
- **`test_ci_workflow_runs_the_mcp_spec_check_recipe`** — load `.github/workflows/ci.yml` with
  PyYAML and assert `just mcp-spec-check` appears among the job's `run` steps, in the idiom of
  the existing `tests/scripts/test_live_workflow_shape.py`. Criterion 1 rests on that
  hand-listed step, and this repository has lost exactly that wiring twice: ADR-0518 records
  `schema-guard` sitting in the `ci` aggregate and the prek hook while CI never invoked it, so
  it passed every PR (#1723), and ADR-0410 hit the same thing. A comment is not a guard, and
  the failure mode is green.
- **`test_mcp_spec_drift_workflow_shape`** — the same `yaml.safe_load` idiom over
  `mcp-spec-drift.yml`: both the `schedule` and `workflow_dispatch` triggers present,
  `permissions` of `contents: read` plus `issues: write`, `GITHUB_TOKEN` in the `env:` of both
  the `--upstream` step and the issue-filing step, `--state all` on the dedup search, and the
  three label strings matching labels the repository actually has. This is static, so it runs
  on the branch — which matters because the live run cannot, per the note below.

No test performs network I/O: `newer_revisions` is pure, and the `--upstream` tests inject
the fetch.

Verification that a unit test cannot reach:

- **Criterion 3** — a `workflow_dispatch` or scheduled run of `mcp-spec-drift.yml`, **after
  merge**, reported back on #1809. It cannot be done before: GitHub resolves dispatchable
  workflows from the default branch, so a workflow file that exists only on this branch has
  nothing to dispatch. The run is expected to open the `2026-07-28` issue; a second dispatch
  must find it and pass without filing, which is the idempotence check. The shape test above is
  what gives the wiring pre-merge coverage in the meantime — without it the entire issue-filing
  half would ship unverified, and this is the repository's first issue-filing workflow, so none
  of it is inherited from working prior art.
- **Criterion 4** — the existing `doc-constants-check` gate. Pointing the binding at a doc
  whose sentence does not match fails it, which is the assertion. Note this gate cannot run on
  macOS: `gen_doc_constants` imports provider composition, which resolves the Linux-only
  `fallocate` symbol at import time, so it is verified in a Linux container.

Two pieces of housekeeping belong to the implementing PR rather than to this document, and
both fail a gate if missed:

- ADR-0537 opens as **Proposed** and must flip to **Accepted** in the same PR that lands the
  script. `scripts/check_adr_status.py` enforces exactly that — its second invariant fails any
  `Proposed` ADR cited from `src/` or `tests/`, "including guard-type ADRs whose enforcement
  ships purely as tests" — and the script and its tests will both cite this record.
- After adding the sentence to `agent-index.md`, run `just resources-docs` and commit the
  regenerated `src/kdive/mcp/resources/_content/agent-index.md`, or `resources-docs-check`
  goes red.
