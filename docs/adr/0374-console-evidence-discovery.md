# ADR 0374 — Console-evidence discovery: capped artifacts.list, latest_console ref, Observe step

- **Status:** Accepted
- **Date:** 2026-07-16
- **Deciders:** kdive maintainers

## Context

Finding the newest console evidence is clunky enough that a black-box agent blew its token
budget before reaching the right tool (#1238). Three papercuts compound:

1. **`artifacts.list` is System-scoped with no cap.** `list_redacted_system_artifacts`
   (`services/artifacts/listing.py`) ran `... owner_kind='systems' AND owner_id=%s
   ORDER BY created_at DESC` with **no `LIMIT`**, while every other catalog list
   (`runs.list`, `systems.list`) is keyset-paginated over `(created_at, id) DESC` through the
   ADR-0192 cursor helpers. The `artifacts.list` envelope hard-coded `truncated=False,
   total=len(items)` and its docstring claimed the set was "naturally bounded" — false: one
   call returned 395 items / 112 KB, over the tool-result token ceiling.
2. **No stable pointer to the newest console.** `runs.get` refs are
   `kernel`/`debuginfo`/`console`/`build-log`; `refs.console` is only the boot-window snapshot
   (`console-<run_id>`). The newest console for a chatty Run is a rotating part
   (`console-part-<gen>-<index>`, ADR-0279), reachable only by opting into the full manifest
   (`include_console_artifacts=true`) and numeric-sorting part names by hand.
3. **The manifest is discoverable too late.** `agent-index.md`'s "Observe evidence" step only
   named `runs.get` + `artifacts.list`/`artifacts.get`, so an agent reading the session flow
   top-down hit the uncapped System-scoped `artifacts.list` wall before learning the
   run-scoped console tools exist.

The design fork (partial vs full fix) was resolved by the maintainer: implement all three.

## Decision

**(a) Cap + keyset-paginate the System-scoped `artifacts.list`.** The query selects
`(id, object_key, created_at)`, orders `(created_at, id) DESC`, fetches `limit + 1`, and seeks
past an optional `(created_at, id)` boundary — identical to `runs.list`. The handler accepts
`limit` (default `DEFAULT_LIST_LIMIT=50`, clamped to `MAX_LIST_LIMIT=200`) and an opaque
`cursor` decoded via the ADR-0192 `decode_ts_uuid_cursor` helper (a bad cursor is an
`invalid_cursor` `configuration_error`, never a silent first page). The envelope now carries the
uniform ADR-0192 pagination keys — `data.truncated` and `data.next_cursor` — matching
`runs.list`/`systems.list`. The misleading `data.total` (which was only ever the page size, never
the full count) is **removed**: keyset pagination reports truncation and a continuation cursor,
not a total.

**(b) Add a `latest_console` ref to `runs.get`.** A cheap single-row query
(`... run_id=%s AND owner_kind='systems' AND sensitivity=REDACTED ORDER BY created_at DESC,
object_key DESC LIMIT 1`) resolves the newest console artifact for the Run — the same
`(created_at, object_key) DESC` total order as the console manifest, so it is exactly
`manifest.entries[0]` without loading the manifest. It is surfaced as `refs.latest_console` (the
artifact id, mirroring `refs.console`), read with `artifacts.get`/`artifacts.find`. It is emitted
for any non-failed Run that has at least one correlated console artifact — no opt-in, since the
whole point is to avoid the manifest round-trip. When the Run has only the boot snapshot,
`latest_console == console`; the ref is still emitted so an agent can *always* read
`refs.latest_console` for "newest console" without reasoning about whether rotating parts exist.

**(c) Surface the manifest in the Observe step.** `docs/guide/agent-index.md`'s "Observe
evidence" step now names `runs.get` with `include_console_artifacts=true` (the run-scoped console
manifest) and `refs.latest_console` (the newest-console shortcut), so the run-scoped tools are
discovered before the System-scoped `artifacts.list`. The packaged mirror
(`src/kdive/mcp/resources/_content/agent-index.md`) is regenerated (ADR-0151).

**No migration.** The `artifacts` table already carries `created_at`, `id`, and the `run_id`
correlation column (used by the ADR-0279 manifest and count queries); both new reads are pure
queries over existing columns.

## Consequences

- `artifacts.list` can no longer return an unbounded System history in one call; a caller pages
  with `data.next_cursor` until `data.truncated` is false. The dropped `data.total` was
  misleading, but its removal is a wire change for any consumer that read it.
- `runs.get` runs one extra indexed `LIMIT 1` query on the non-failed read path. Agents get a
  stable newest-console pointer without the opt-in manifest.
- The agent-facing contract (wrapper docstrings + `Field` text on `runs.get` and
  `artifacts.list`) documents the cap/cursor/`truncated` semantics and the new `latest_console`
  ref.

## Alternatives considered

- **A real `COUNT` total instead of keyset truncation.** Rejected: an extra count query per list
  call, and it diverges from the `runs.list`/`systems.list` keyset convention (`truncated` +
  `next_cursor`, no total) that ADR-0192 established.
- **Emit `latest_console` only when it differs from `console`.** Rejected: forces the agent to
  reason about whether rotating parts exist; a deterministic always-present ref is simpler.
- **Resolve `latest_console` through the opt-in manifest.** Rejected: the manifest round-trip is
  the exact clunk being removed; a single indexed `LIMIT 1` is cheaper and needs no opt-in.
- **A denormalized "newest console" column on the Run.** Rejected: a migration and a write-path
  invariant to maintain, for a query the `(run_id, created_at)` order already answers cheaply.
