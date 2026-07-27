# ADR 0465 — Fold `fixtures.list` into `images.list`

- **Status:** Accepted
- **Date:** 2026-07-27
- **Issue:** #1590
- **Epic:** #1576
- **Implements:** [ADR-0456](0456-agent-operator-mcp-exposure-profiles.md) §3's retired-name search
  vocabulary, through the mechanism [ADR-0458](0458-fold-postmortem-triage-into-postmortem-crash.md)
  established.
- **Amends:** [ADR-0089](0089-operator-cli-mcp-client.md) §6's `fixtures.list` catalog
  read and [ADR-0112](0112-systems-inventory-config.md)'s DB-backed baseline listing.

## Context

`fixtures.list` and `images.list` were not two entities. They were one table read twice.

Both selected from `image_catalog`. Both ordered by the same natural key `(provider, name, arch)`.
Both used the same keyset pagination ([ADR-0192](0192-list-pagination-envelope.md)) with
the same `limit + 1` fetch and the same truncation derivation. Both were plain async pooled DB
reads with no platform gate, no destructive gate, no audit row, and no job. Both were
`_docmeta.read_only()` at maturity `implemented`, and both were classified `PUBLIC_TOOLS` in
`kdive.mcp.exposure` — callable by any authenticated token.

The predicates differed only by a branch. `images.list` selected

```
visibility = 'public' OR (visibility = 'private' AND owner = ANY(<viewer-authorized projects>))
```

and `fixtures.list` selected `visibility = 'public' AND owner IS NULL`. That second conjunct is
redundant: `visibility` is two-valued, and the `image_private_owner` CHECK in
`db/schema/0023_image_catalog.sql` asserts `(visibility = 'private') = (owner IS NOT NULL)`, so a
public row is exactly an owner-less row and `fixtures.list`'s predicate reduces to
`visibility = 'public'` —
precisely `images.list`'s predicate with the private-OR branch dropped. Fixtures were a **row
subset of the same table**, not a different entity, so this is a projection fold and needs no
union type.

The split also carried an equivocation inside the `fixtures` namespace itself. `fixtures.list` read
DB `image_catalog` rows; `fixtures.validate` reads a **filesystem** profile catalog
(`load_fixture_catalog`, [ADR-0120](0120-operator-fixture-profile-write-path.md)). Two tools in one
namespace meant two unrelated sources of truth under one word. Folding the listing out leaves
`fixtures` meaning exactly one thing.

Epic #1576 requirement 5 permits consolidation only where authorization, annotations, execution
class, and result shape match. The first three matched exactly. The fourth did not, and resolving
it is the substance of this decision.

## Decision

### 1. `images.list` gains a closed `scope` discriminator

`_ImagesListPayload` gains `scope: ImageListScope`, a closed two-member `StrEnum` defaulting to
`visible`:

- `visible` — the public rows plus the private rows owned by projects the caller can view. The
  pre-existing behavior, unchanged, and the default, so every existing call site keeps its
  results.
- `public_baseline` — the public rows alone: the curated baseline images any project may provision
  without registering one of its own.

The scope selects one of two SQL predicates and nothing else. The ordering, the keyset window, the
row projection, the `bind_context` principal binding, and the envelope are shared. `public_baseline`
carries no `owner IS NULL` conjunct, because the DB CHECK above already guarantees it; writing it
would be a dead predicate that reads as a second, independent rule.

### 2. The `collection` envelope is unchanged for every scope

This is the decision the shape mismatch forced, and it is deliberate.

`fixtures.list` returned rows *inside* `data`: `data={"fixtures": [ {provider, name, arch, volume} …
], truncated, next_cursor}` — hand-flattened dicts, four fields, never `model_validate`d.
`images.list` returns `ToolResponse.collection(...)`: one sub-envelope per row built from
`ImageCatalogEntry.model_validate` through `_row_envelope`, twelve fields each, with `truncated` /
`next_cursor` / `count` in the outer `data`.

`scope="public_baseline"` narrows **rows, not shape**. It returns the identical collection envelope
with the identical per-row fields. Emitting a narrowed four-field body for `public_baseline` was
rejected: that would re-introduce inside one tool exactly the shape split this fold exists to
remove, and it would mean an agent could not learn one response contract for `images.list`.

The consequence is a deliberate, breaking read for callers of the old tool. They move from
`data.fixtures[]` to `items[].data.*`, and they gain eight fields — `visibility`, `owner`, `state`,
`capabilities`, `os`, `default_kernel_version`, `has_kernel_config`, `description` — that let an
agent compare baseline images on merit in one call instead of an N+1 `images.describe` fan-out.
Nothing available before is lost: `provider`, `name`, `arch`, and `volume` all survive.

### 3. Cursors are not portable across the fold

Cursors are tagged with the tool name, and the tags differed (`fixtures.list` vs `images.list`), so
a cursor minted by the old tool does not decode against the new one — it returns the ordinary
`invalid_cursor` configuration error, not a wrong page. Within `images.list` the cursor tag is one
value across both scopes, which is correct: the scopes share an ordering key, so resuming a
`visible` cursor under `public_baseline` skips to the same key and then lists the public tail. A
cursor is tied to the tool, not to a scope.

### 4. `fixtures.list` is removed, name and all

No alias, no deprecation period, no dual name — the repository is pre-release and follows
replace-don't-deprecate, and epic #1576 requirement 6 requires the old wrapper and the old name to
go in the same change. `fixtures.list` leaves `PUBLIC_TOOLS`; the handler, its payload model, its
SQL, and its behavior-test file are deleted.

`fixtures.validate` survives untouched, so the `fixtures` namespace stays live and keeps its
`NAMESPACE_TOC` entry — reworded from "Test fixture profile listing and validation" to "Test
fixture profile validation", because listing is no longer what this namespace does.

`validate_fixtures_tool`'s `suggested_next_actions` breadcrumb pointed at `fixtures.list`. Left
alone it would have raised `ValueError` from `visible_next_actions` the moment the name left
`PUBLIC_TOOLS` ([ADR-0421](0421-schema-generated-kdivectl-verbs.md)); it now points at `images.list`.

### 5. Discoverability moves to search vocabulary

`RETIRED_TOOL_NAMES` gains one row, `"fixtures.list": "images.list"`. The existing mechanism
(`_invert_retired_names`, `_RETIRED_BY_REPLACEMENT`, `retired_names_for`, consumed by `_score` in
the gateway) is used as-is and not restructured. Because search matches substrings, the row alone
makes both `fixtures.list` and the bare word `fixture` rank the replacement.

`images.list` also gains a `TOOL_KEYWORDS` entry carrying the baseline/fixture intent vocabulary
("baseline", "fixture", "fixtures", "test", "rootfs", "catalog"). The retired name covers an agent
that knows the old *name*; the keywords cover an agent that only knows the old *intent*. The enum
value `public_baseline` also enters the search haystack through the schema-term walk, so the scope
is discoverable from the tool's own schema.

### 6. The curated `kdivectl fixtures list` verb is deleted

`kdivectl fixtures list` was a curated verb (`cli/commands/registry.py`) with a hand-written handler
that read `data.fixtures`. Both are deleted rather than repointed. **This is a breaking `kdivectl`
change**: the `fixtures list` verb no longer exists, and operators use `kdivectl images list
--scope public_baseline`.

`images list` is itself curated, and a curated verb overrides the generated shape at its path, so
the schema-derived `--scope` flag would not have reached the operator surface on its own. The
curated verb therefore declares `--scope` explicitly and threads it into the `request` wrapper; an
omitted flag sends no `request` at all, leaving the server's `visible` default authoritative rather
than restating it in the client. The curated `Verb` shape carries no argparse `choices`, so an
unrecognized scope is rejected server-side as a `configuration_error` rather than at parse time —
accepted rather than widening the curated verb spec for one flag.

Repointing the curated handler at `images.list` was rejected. Its whole body was the `data.fixtures`
flattening that no longer has a source, so "repointing" would have meant writing a new hand-curated
projection of a collection envelope the generated verb already renders. A curated verb also
overrides the generated shape at its path, so keeping one here would mask the generated
`images list` scope flag behind a narrower hand-maintained surface.

## Consequences

- The physical registry drops by one tool.
- Any caller of `fixtures.list` — agent, script, or operator — must change. The failure is loud:
  the tool name is gone from the registry, so an invocation errors rather than silently returning a
  different shape.
- One less place for the catalog read predicate to drift. A change to the row projection now lands
  on both scopes by construction.
- `data.fixtures` disappears as a response shape; `_data_list` in the CLI reads is left with one
  caller (`secrets.list`).
- The `fixtures` namespace now means exactly one thing: the on-disk profile catalog.

## Alternatives considered

- **Keep both tools and share a helper.** Rejected: it removes the duplicated *code* but leaves
  the duplicated *surface*, which is what epic #1576 is about. Two tool names for one row set is
  the cost being paid.
- **Emit the four-field flat body for `public_baseline`.** Rejected in §2 above.
- **Make `scope` a free-form filter string.** Rejected: the epic requires a closed discriminator so
  the enum values are enumerable in the schema, searchable, and renderable as CLI flag choices.
- **Keep `fixtures.list` as a deprecated alias for one release.** Rejected by epic non-goal "no
  compatibility aliases, dual tool names, or deprecation period".
- **Scope-tag the cursor.** Rejected: the scopes share an ordering key, so a cross-scope cursor
  resumes coherently. Tagging by scope would add a failure mode without preventing a wrong answer.
