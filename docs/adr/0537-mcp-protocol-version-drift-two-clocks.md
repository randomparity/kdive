# 0537 — Detect MCP protocol-version drift on two clocks, without vendoring the schema

## Status

Proposed

## Context

KDIVE advertises an MCP protocol version to every agent client that connects, and nothing
in this repository states what that version is or notices when it changes.

The version is not a KDIVE fact. It is a property of the pinned dependencies —
`fastmcp-slim[server,client]==3.4.4` and `mcp==1.28.1` (`pyproject.toml:14`, `:23`) — whose
`mcp.types.LATEST_PROTOCOL_VERSION` is `2025-11-25`. KDIVE never serialises a JSON-RPC
envelope itself; the wire layer is delegated in full. So the version KDIVE speaks can move
on any `uv lock` bump, with no diff in `src/kdive` and no reviewer looking at it.

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

**Local clock, offline, gates every PR.** `src/kdive/mcp/__init__.py` declares
`MCP_PROTOCOL_VERSION`. `scripts/check_mcp_spec_version.py` asserts in its default mode
that `mcp.types.LATEST_PROTOCOL_VERSION` still equals it, and `just mcp-spec-check` runs
that mode from the `ci` recipe. A dependency bump that moves the advertised version fails
CI until a person edits the constant. The check reads only the installed package, so it
needs no network.

**Upstream clock, network, gates nothing.** The same script's `--upstream` mode lists the
upstream `schema/` directory, keeps entries matching `^\d{4}-\d{2}-\d{2}$` — which drops
`draft` — and fails when the newest exceeds `MCP_PROTOCOL_VERSION`. ISO-8601 dates sort
lexicographically, so the comparison needs no date parsing.
`.github/workflows/mcp-spec-drift.yml` runs it on a weekly cron plus `workflow_dispatch`,
opens or comments on a tracking issue, and exits non-zero so the Actions badge reflects the
state.

**One guarded statement of the version in prose.** `docs/guide/agent-index.md` gains a
sentence naming the negotiated revision, bound in `gen_doc_constants.py` as a *guarded*
binding against `MCP_PROTOCOL_VERSION`. Guarded rather than generated: the surrounding
sentence carries nuance a generator must not rewrite, matching how the upload-ceiling and
CLI-wait bindings already work.

This decision does not change the protocol version KDIVE speaks.

## Consequences

A dependency bump that moves the protocol version now costs one deliberate edit to
`MCP_PROTOCOL_VERSION` and one to the guarded sentence. That friction is the point: the
negotiated version is a compatibility surface for every agent client, and it previously
moved with no review at all.

The `ci` gate gains one offline check. It cannot fail from a GitHub outage, a rate limit,
or an air-gapped runner, because the only network-dependent mode runs on the cron.

The cron will fail on its first run and file an issue: upstream `2026-07-28` is newer than
the declared `2025-11-25`. That is the correct report, not a defect in the check. Closing
it requires leaving `mcp==1.28.1` — PyPI's current `mcp` is `2.0.0`, a major bump with its
own compatibility review — so the issue will stay open until that work is scheduled.

Drift *within* an already-published revision is not detected. Only a vendored byte-for-byte
copy would catch an in-place edit to `2025-11-25/schema.json`, and this decision declines to
carry one. The exposure is small: the specification treats published revisions as immutable,
and KDIVE consumes the schema only through `mcp`, which would carry such a change itself.

Two existing citations of "MCP spec (2025-06-18)" —
`docs/adr/0268-tool-gateway-dispatcher.md:27` and
`docs/design/2026-06-27-tool-gateway-dispatcher-866.md:34` — are deliberately left alone.
They cite the revision that established the `tools/list` rule, which is a historical fact
about when a rule was set, not a claim about what KDIVE currently negotiates. Guarding them
against the live constant would make the guard rewrite an accepted decision, which
`docs/adr/README.md:20` forbids.

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
- **Compare against `mcp.types.DEFAULT_NEGOTIATED_VERSION` (`2025-03-26`) instead of
  `LATEST_PROTOCOL_VERSION`.** The default is the fallback for a client that offers nothing
  newer; the latest is what KDIVE advertises and what a bump would move.
- **Add a dedicated `mcp-spec-drift` label, as the sibling repository does.** The existing
  `area:mcp-api` plus `type:chore` already select these issues, and a label that appears
  once a year is one nobody maintains.
