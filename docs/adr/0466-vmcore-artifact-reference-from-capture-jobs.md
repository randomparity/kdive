# ADR 0466 — Return the vmcore artifact reference from the capture job

- **Status:** Accepted
- **Date:** 2026-07-27
- **Issue:** #1591
- **Epic:** #1576
- **Implements:** [ADR-0456](0456-agent-operator-mcp-exposure-profiles.md) §3's retired-name search
  vocabulary, through the mechanism
  [ADR-0458](0458-fold-postmortem-triage-into-postmortem-crash.md) established, and
  [ADR-0414](0414-teardown-surfaces-allocation-release.md)'s durable terminal-kind steer.
- **Amends:** [ADR-0244](0244-per-run-vmcore-capture.md)'s `vmcore.list(run_id)` listing, and
  [ADR-0031](0031-retrieve-plane-vmcore-postmortem.md)'s two-tool retrieve plane.

## Context

`vmcore.fetch` enqueues a `capture_vmcore` job. The job's handler writes two Run-owned artifact
rows — the raw core (`.../runs/{run_id}/vmcore-{method}`, `sensitive`) and its redacted sibling
(`…-redacted`, `redacted`) — and then returned **the raw object key** as the job result. That value
reaches the agent as `refs.result`.

It is unusable. `artifacts.get` takes an artifact **id**; `artifacts.fetch_raw` is
`contributor`-gated and egresses only the `RawAsset` allow-list. A viewer handed the raw key has no
tool that accepts it. So the surface carried `vmcore.list(run_id)` — a tool whose entire job was to
convert "the capture finished" into "here is the id you can actually read". That is a lookup the
job already had in hand and threw away.

Capture enforces at most one core per Run and method ([ADR-0244](0244-per-run-vmcore-capture.md)),
so the "listing" returned a collection of exactly one. A collection envelope, a registered tool, an
RBAC row, a generated CLI verb, and a doc page existed to carry a single id across one round trip.

Three things had to be resolved before the tool could go, and the issue named none of them
completely.

**The replay path resolved the wrong key.** `precheck_run` and `finalize_capture` both short-circuit
on an existing core, and both resolved it through `raw_vmcore_key`, which explicitly *excludes*
`-redacted`. A replayed capture therefore had no redacted id anywhere in its call path.

**Historical rows already held raw keys.** Every terminal `capture_vmcore` row in an existing
database stored a raw object key in `jobs.result_ref`. Without a migration, `refs.result` would mean
"artifact id" for new jobs and "raw object key" for old ones — one field, two meanings, no way for a
reader to tell which it holds.

**`vmcore.list` was the only Run-scoped core lookup.** `artifacts.list` is System-scoped and mixes
every Run that ever booted on that System; cores are Run-owned, so it does not list them at all.
A viewer holding a `run_id` and no job id — a resumed session, a handoff — would have lost the path
to the core entirely, because `runs.get` carried no vmcore reference.

## Decision

### 1. The capture job's result is the redacted artifact id

`capture_handler` returns `str(redacted.id)` — the id of the row `ARTIFACTS.insert` persisted, read
back from the insert rather than from the pre-insert model, so it is the DB-authoritative value.
That becomes `jobs.result_ref` and is rendered as `refs.result` by the single assembler
`ToolResponse.from_job`, which every `jobs.get` / `jobs.wait` / `jobs.list` read and
`vmcore.fetch`'s own enqueue envelope already go through. No reader changed.

The raw key is not published anywhere. It stays the internal handle `raw_vmcore_key` uses for the
per-Run dedup guard and for `ensure_method_match`, which needs the `vmcore-{method}` suffix the
redacted key also carries but which only the raw row is guaranteed to have.

### 2. `refs.result` means one thing, or nothing

The redacted row can be absent while the raw core survives — artifact reclaim and expiry act on
rows independently. In that case the job publishes **no** result reference (`None` → the `refs`
dict simply omits `result`), rather than falling back to the raw key.

This is the load-bearing invariant. A reader of `refs.result` on a `capture_vmcore` job may assume
it is an artifact id it can hand to `artifacts.get`. A raw-key fallback would make that assumption
conditional on state the reader cannot observe, which is precisely the ambiguity this ADR removes.
The absence is recoverable through §4.

`ExistingCapture` — a one-field frozen dataclass — carries this across the replay short-circuit.
The previous `tuple[Run, System] | str` return leaned on `isinstance(precheck, str)` to mean "a core
already exists"; with the result now optionally `None`, a bare `str | None` would have made "no
existing core" and "existing core, no redacted row" indistinguishable.

### 3. `SUCCEEDED` capture jobs steer at the tools that consume the reference

`_TERMINAL_KIND_ACTIONS` gains `JobKind.CAPTURE_VMCORE: ["artifacts.get", "postmortem.crash"]` —
the same pair the deleted `vmcore_collection` envelope advertised. Per
[ADR-0414](0414-teardown-surfaces-allocation-release.md) this makes the steer a durable property of the
completed job: it is present on every later read of that job, not only on the enqueue envelope the
agent may no longer hold. Keyed on `SUCCEEDED` only, since a failed capture produced no core.

### 4. `runs.get` carries the same id as `refs.vmcore`

`runs.get` gains `refs.vmcore`, resolved by the same read model as the job result, so the two are
the same value by construction. This is the Run-keyed way back to the core, and it is what makes
removing `vmcore.list` a fold rather than a deletion: `vmcore.list` took exactly one argument,
`run_id`, and so does `runs.get`.

It is surfaced on the non-failed-Run path alongside `refs.latest_console`, and omitted until a
capture lands a redacted core. A `failed` Run renders `_failed_envelope`, which carries no refs at
all — unchanged, and consistent with every other Run artifact ref.

### 5. The replacement is `runs.get`, not `artifacts.get` or `jobs.get`

`RETIRED_TOOL_NAMES` gains `"vmcore.list": "runs.get"`.

`artifacts.get` was rejected: it answers "read these bytes", and requires an artifact id the agent
does not have. It is where you go *after* the lookup, not the lookup. `jobs.get` was rejected: it
requires a job id, which an agent that asks "which core does this Run have" by definition may not
hold — and if it did, it would already have the reference. Only `runs.get` answers the question
from the key the retired tool took. `runs.get` also gains the `vmcore` / `core` intent vocabulary
in `TOOL_KEYWORDS`, so an agent that knows the old *intent* but not the old *name* lands there too.

### 6. Migration 0079 backfills historical rows, and NULLs what it cannot map

Both artifact rows are written in one transaction from one `CaptureOutput`, so the redacted key is
the raw key plus `-redacted` across every provider. The migration joins on exactly that, with no
payload parsing and no `run_id` resolution.

A row whose redacted sibling is absent resolves to `NULL` — the value the correlated subquery
already yields on no match, so the fallback is the statement's natural behavior rather than a
second pass. NULL is the honest answer, and it is §2's rule applied to history: publish the id or
publish nothing. The agent recovers through `runs.get`'s `refs.vmcore` (also NULL-safe) or
`artifacts.list`.

The `UPDATE` is scoped by the raw-vmcore key *shape*, mirroring `raw_vmcore_key`'s own predicate.
An artifact id contains no `/vmcore-`, so the statement is idempotent: a second application matches
nothing. Migrations are byte-immutable once applied
([ADR-0015](0015-sql-migration-runner.md)), so this matters.

### 7. `vmcore.list` is removed, name and all

No alias, no deprecation period. The wrapper, the `list_vmcores` handler, the `vmcore_collection` /
`_vmcore_item` / `_is_redacted_vmcore` renderers, the now-unused
`list_redacted_run_artifacts` service, the `exposure.py` row, and the `TOOL_KEYWORDS` row are all
deleted.

The `vmcore` `NAMESPACE_TOC` entry stays — `vmcore.fetch` remains — but is reworded from "Crash dump
listing and download" to "Crash dump capture from a crashed system", because listing is no longer
what the namespace does and it never downloaded.

The `triage_panic` MCP prompt's third step named `vmcore.list`. `prompts/registrar.py` validates
every step against the live registry **at app assembly**, so leaving it would have failed the server
build, not a request. It now names `jobs.wait`, which is both the real next step after an async
capture and where the reference appears.

## Consequences

- The physical registry drops by one tool.
- An agent that polled `vmcore.fetch` → `jobs.wait` → `vmcore.list` now stops one call earlier: the
  drain already returns the reference.
- No authorization boundary moves. `vmcore.list` was the only tool removed and it was `viewer`;
  `jobs.*`, `artifacts.get`, and `runs.get` are all `viewer`, so a viewer-only agent keeps the
  lookup. Capture (`vmcore.fetch`) and raw egress (`artifacts.fetch_raw`) stay `contributor`.
- `jobs.result_ref` for `capture_vmcore` becomes a foreign key in spirit to `artifacts.id`, not a
  free-form string. Nothing enforces it in SQL; §2 and the migration are what keep it true.
- The remote live-stack spine's capture arms were repaired in the same change. They had called
  `vmcore.fetch(system_id=…)` and `vmcore.list(system_id=…)` since
  [ADR-0244](0244-per-run-vmcore-capture.md) made both Run-addressed — dead calls the gated tier
  never executed. They are now Run-addressed, and the capstone's System A gains the bound Run its
  capture requires.

## Alternatives considered

- **Publish both the id and the object key.** Rejected: `refs` values are opaque handles, and a
  second entry carrying a `sensitive` row's key would put the raw core's location into a
  viewer-readable envelope for no gain.
- **Keep `vmcore.list` and additionally publish the id.** Rejected by epic #1576 non-goal "no
  compatibility aliases, dual tool names, or deprecation period", and it would leave the
  duplicated surface the epic exists to remove.
- **Leave historical `result_ref` values as raw keys.** Rejected in §6: it makes the field's meaning
  depend on when the row was written, which no reader can determine.
- **Fall back to the raw key when the redacted row is gone.** Rejected in §2 for the same reason —
  it reintroduces the ambiguity conditionally.
- **Point the retired name at `artifacts.get`.** Rejected in §5: an agent searching the retired
  name does not yet hold an artifact id, so it would land on a tool it cannot call.
- **Widen `artifacts.list` to a Run scope instead of adding `refs.vmcore`.** Rejected: that is a
  new discriminator on a System-scoped pagination contract to answer a question with exactly one
  answer, where a single ref on the Run the agent already reads suffices.
