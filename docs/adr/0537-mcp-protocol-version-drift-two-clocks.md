# 0537 — Detect MCP protocol-version drift on two clocks, without vendoring the schema

## Status

Accepted (2026-08-04)

## Context

KDIVE advertises an MCP protocol version to every agent client that connects, and nothing
in this repository states what that version is or notices when it changes.

The version is not a KDIVE fact. It is a property of the pinned dependencies —
`fastmcp-slim[server,client]==3.4.4` and `mcp==1.28.1` (`pyproject.toml:14`, `:23`). KDIVE
never serialises a JSON-RPC envelope itself; the wire layer is delegated in full. So the
version KDIVE speaks can move on any `uv lock` bump, with no diff in `src/kdive` and no
reviewer looking at it.

It is also not a single version. `mcp/server/session.py` answers `initialize` with the
client's requested version whenever that version appears in
`mcp.shared.version.SUPPORTED_PROTOCOL_VERSIONS`, falling back to
`mcp.types.LATEST_PROTOCOL_VERSION` otherwise; `mcp/server/streamable_http.py` rejects
against the same list. At the current pin those are
`['2024-11-05', '2025-03-26', '2025-06-18', '2025-11-25']` and `2025-11-25`. What KDIVE
offers an agent client is therefore a *set*, whose ceiling is the latest — and the two ends
fail differently. Raising the ceiling is additive for existing clients; dropping a member is
the change that breaks a client pinned to an older revision.

Upstream moves on a separate clock. `modelcontextprotocol/modelcontextprotocol` currently
publishes `schema/2024-11-05`, `2025-03-26`, `2025-06-18`, `2025-11-25`, `2026-07-28` and
`draft`. The July 2026 revision landed without anything here reporting it, which is the
observation that opened #1809.

The sibling repository `rusty-imap-mcp` already solves the upstream half:
`scripts/refresh-mcp-spec.sh` diffs a vendored `schema.json` against upstream, and
`.github/workflows/mcp-spec-drift.yml` runs it weekly and files a tracking issue. That
design does not transfer. It vendors the schema because
`crates/rimap-server/tests/mcp_wire_conformance.rs` validates real wire payloads against
it — that server hand-rolls protocol handling over `rmcp` and needs the document. KDIVE has
no such consumer, so a vendored copy here would exist only to be diffed against the
upstream it was copied from.

The repository already has the right idiom for the local half. `scripts/gen_doc_constants.py`
(ADR-0410) exists because agent-facing prose restates values whose source of truth is a
Python symbol, and those restatements drift silently; it was filed after a review found
`agent-index.md` claiming "~100 tools" against a much larger registry. It supports a
*generated* binding the script rewrites and a *guarded* binding it only asserts. Roughly a
dozen sibling `just *-check` recipes feed the `ci` gate (`justfile:540`), and several are
deliberately offline (ADR-0505) so the gate never depends on network reachability.

## Decision

We will detect MCP version drift as two separate facts, on two clocks, enforced in two
places — and will not vendor the specification schema.

**Local clock, offline, gates every PR.** `src/kdive/mcp/__init__.py` declares both ends of
the surface — `MCP_PROTOCOL_VERSION` for the ceiling and `MCP_SUPPORTED_PROTOCOL_VERSIONS`
for the whole set. `scripts/check_mcp_spec_version.py` asserts in its default mode that
`mcp.types.LATEST_PROTOCOL_VERSION` and `tuple(mcp.shared.version.SUPPORTED_PROTOCOL_VERSIONS)`
still equal them. Declaring the set as well as the ceiling is what makes a *dropped*
revision — the client-breaking direction — the same one-line reviewable edit as a raised
one. The check reads only the installed package, so it needs no network.

It is wired in two places, not one. `just mcp-spec-check` joins the `ci` recipe, and a
dedicated `- run: just mcp-spec-check` step joins `.github/workflows/ci.yml`. The second is
the one that gates a PR: CI invokes each recipe as its own step and never runs `just ci`, a
hazard that file records in eleven separate comments and that ADR-0410 hit before this.

**Upstream clock, network, gates nothing.** The same script's `--upstream` mode lists the
upstream `schema/` directory, keeps entries matching `^\d{4}-\d{2}-\d{2}$` — which drops
`draft` — and reports when the newest exceeds `MCP_PROTOCOL_VERSION`. ISO-8601 dates sort
lexicographically, so the comparison needs no date parsing. It fails closed on an
unrecognizable listing: the declared version must itself appear among the matched entries, or
the check reports that it could not read the directory rather than that nothing is newer.
`.github/workflows/mcp-spec-drift.yml` runs it on a weekly cron plus `workflow_dispatch` and
opens a tracking issue on the transition into drift.

**One generated statement of the version in prose.** `docs/guide/agent-index.md` gains a
sentence naming the negotiated ceiling, bound in `gen_doc_constants.py` as a *writable*
binding against `MCP_PROTOCOL_VERSION` — matching the tool-count binding already in that
file, and ADR-0410's rule that the guarded kind exists to keep generators off hand-authored
`.py` docstrings, since they "only ever write `docs/`". `write()` substitutes capture group 1
alone, so a generated binding cannot disturb the sentence around the date.

This decision does not change the protocol version KDIVE speaks.

## Consequences

A dependency bump that moves either end of the supported range now costs one deliberate edit
to the declaring module, plus `just doc-constants` and `just resources-docs` to regenerate the
sentence and the packaged snapshot that mirrors it. That friction is the point: the negotiated
range is a compatibility surface for every agent client, and it previously moved with no review
at all.

The `ci` gate gains one offline check. It cannot fail from a GitHub outage, a rate limit,
or an air-gapped runner, because the only network-dependent mode runs on the cron.

The cron will report drift on its first run: upstream `2026-07-28` is newer than the declared
`2025-11-25`. It will not file anything, because issue #1485 ("Investigate MCP 2026-07-28 Spec
Update Requirements") is already open and names that revision — the dedup finds it and the run
passes quietly. That is the arm working as designed, and it is why the dedup search carries no
`--label` conjunct: #1485 does not carry this workflow's labels, and a label-filtered query
would have missed it and duplicated it on day one.

Resolving the underlying drift requires leaving `mcp==1.28.1` — PyPI's current `mcp` is
`2.0.0`, a major bump with its own compatibility review — so #1485 stays open until that work
is scheduled.

The workflow is split into two jobs, and the split is the security boundary rather than a
structural preference. `check-drift` runs `uv run python` and so imports the whole synced
dependency tree; it holds `contents: read` only. `report` holds the sole `issues: write` grant
and runs nothing but `gh` — no checkout, no dependency install, no project code. A job-level
grant on the first job would have put an issue-creating token in the same process as every
third-party package, for no benefit: the token there is only a rate-limit bump against a public
third-party repository, which any scope satisfies. This is the repository's first
elevated-permission workflow, so it is the wiring the next one will copy.

Because that state persists for months rather than days, the cron is idempotent: it files on
the *transition* into drift and succeeds quietly whenever a matching issue already exists, in
any state. A weekly job that instead commented every run would add roughly fifty identical
comments a year and hold the badge red permanently, which would make an actually-broken
check — a changed API shape, an expired token scope, a failed `uv sync` — indistinguishable
from the expected state. Red therefore means newly-detected drift or a check that could not
run, and the tracking issue, not the badge, is the standing report.

"In any state" is the load-bearing half. Dedup on an *open* issue alone would re-arm the filer
the moment a maintainer closed it — which is the ordinary disposition once the item is folded
into an `mcp` 2.0.0 epic or recorded under `docs/debt/` — producing a duplicate issue and a
red job every week thereafter. The predicate is that a human has seen this revision, not that
an issue is currently open.

Drift *within* an already-published revision is not detected. Only a vendored byte-for-byte
copy would catch an in-place edit to `2025-11-25/schema.json`, and this decision declines to
carry one. The exposure is small: the specification treats published revisions as immutable,
and KDIVE consumes the schema only through `mcp`, which would carry such a change itself.

Two existing citations of "MCP spec (2025-06-18)" —
`docs/adr/0268-tool-gateway-dispatcher.md:27` and
`docs/design/2026-06-27-tool-gateway-dispatcher-866.md:34` — are deliberately left unbound.
They cite the revision that established the `tools/list` rule, which is a historical fact
about when a rule was set, not a claim about what KDIVE currently negotiates. Binding them to
the live constant would fail `doc-constants-check` permanently from the next bump onward and
demand editing a historical citation to clear it — and for the ADR, `docs/adr/README.md:20`
forbids that edit outright.

## Considered & rejected

- **Port the `rusty-imap-mcp` design directly: vendor `schema.json` and byte-diff it
  weekly.** Nothing in KDIVE would read the vendored file. It would catch in-place edits to
  a published revision, but at the cost of a ~120 KB fixture whose only consumer is its own
  drift check, and a refresh script whose output no test validates.
- **Build a wire-conformance harness so the vendored schema has a consumer.** Most of what
  it would assert is `fastmcp`'s correctness, not KDIVE's, and `fastmcp` tests that itself.
  It is a large build justified by a gap that has not been observed.
- **Read `mcp.types.LATEST_PROTOCOL_VERSION` directly with no KDIVE-owned constant.** Docs
  would stay accurate and bumps would cost nothing, but a dependency bump could still move
  the advertised protocol version with no reviewer seeing it — the failure this ADR exists
  to close.
- **Put the upstream check in `ci` alongside the offline one.** It would make every PR
  depend on github.com being reachable and on a revision KDIVE cannot adopt without a major
  dependency bump, so the gate would sit red for weeks and be disabled.
- **Assert only the ceiling (`LATEST_PROTOCOL_VERSION`).** It was the first form of this
  decision and it is blind in the direction that breaks clients: a release that drops
  `2024-11-05` from `SUPPORTED_PROTOCOL_VERSIONS` leaves the ceiling untouched, so the gate
  stays green, the constant stays unedited, and the prose stays accurate while a pinned agent
  client stops being able to connect.
- **Assert `mcp.types.DEFAULT_NEGOTIATED_VERSION` (`2025-03-26`) as the third constant.** It
  is the fallback used when a client sends no version header at all, and it is already
  bounded by the supported set the decision now declares, so a third declaration adds an edit
  per bump without covering a case the set misses.
- **Add a dedicated `mcp-spec-drift` label, as the sibling repository does.** The existing
  `area:mcp-api` plus `type:chore` already select these issues, and a label that appears
  once a year is one nobody maintains.
