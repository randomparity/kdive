# Spec — Upload deadline contract for agents (#1336)

Status: Draft
Issue: #1336
ADR: [0394](../../adr/0394-upload-deadline-contract-fields.md)

## Problem

The `artifacts.create_run_upload` / `artifacts.create_system_upload` success
responses hand an agent an under-specified upload window, so a rational agent
plans pessimistically and invents workarounds. Observed: an agent believed a
1-hour upload window was too short for a kernel image and split a tar into five
parts to "beat the clock" — misusing the chunked-upload mechanism (which exists
for the 5 GiB single-PUT *size* limit) to solve a *time* problem that did not
exist.

The response violates the codebase's own five-part limit doctrine (AGENTS.md,
"State a limit's full contract"): it states a limit exists but not the reference
clock, the limit's real value, its scope, or the recovery action.

Three concrete gaps in the current response:

1. **Relative-only deadline, no reference clock.** Each upload item emits
   `data.expires_in` (a bare integer) with no `server_time` and no absolute
   instant. An agent has no wall clock and cannot recompute remaining time after
   any elapsed reasoning.
2. **The wrong deadline is surfaced.** `expires_in` is the *presign TTL*
   (`min(3600, KDIVE_UPLOAD_TTL_SECONDS)`), but the contract the reaper enforces
   is the *manifest deadline* (`now() + KDIVE_UPLOAD_TTL_SECONDS`, refreshable).
   The agent never sees the reaper's deadline.
3. **No recovery / non-constraint hint.** Re-minting is cheap and resets the
   window; chunking is a size mechanism, not a time one — neither fact is stated,
   so the window reads as a hard wall to route around.

## Goal

A deadline-bearing upload response gives the agent everything it needs to plan
without guessing: an absolute deadline, a reference clock to measure it against,
the reaper-enforced deadline (not only the presign TTL), and a named recovery
action. The tool descriptions state the window's scope (a PUT must *begin*
before its URL expires; an in-flight transfer is not interrupted) and that
chunking is a size mechanism, not a time one.

Non-goals: no change to reaper behavior, TTL config, the manifest schema, the
`refresh_deadline` path, or the object-store presign mechanism. This is a
response-contract and description change only.

## The two deadlines (reconciliation — gap 2)

There are genuinely two windows, with distinct meaning; the fix surfaces both
rather than picking one:

- **Presigned-URL expiry** — each minted PUT URL is a boto3/SigV4 presigned URL
  whose validity is `presign_ttl = min(3600, UPLOAD_TTL_SECONDS)`. SigV4 checks
  `X-Amz-Expires` against the request's *start* time, so the PUT to a given URL
  must **begin** before that URL expires; an already-streaming transfer is not
  aborted at expiry. This is the operative "start-by" wall for each URL, so it
  is surfaced **per upload item**.
- **Manifest deadline** — `replace_manifest` stamps `deadline = now() + ttl`
  (`ttl = UPLOAD_TTL_SECONDS`) in Postgres. This is what the reaper keys off to
  reclaim an unfinalized upload's objects, and it is refreshable. It governs the
  whole upload, so it is surfaced **once at the collection level**.

When `UPLOAD_TTL_SECONDS ≤ 3600` (the default range) the two coincide; when it
is larger they diverge and both are reported. Documenting both, with which
governs what, is the "reconcile the two" path the issue asks for.

## Clock authority

Both the reference clock and the manifest deadline are read from the **same
Postgres transaction** via `RETURNING now(), deadline` on the manifest upsert.
Postgres `now()` is transaction-start time, so `deadline − server_time == ttl`
holds exactly, and the agent measures against the same clock the reaper uses.
Each item's absolute URL expiry is rendered as `server_time + presign_ttl`
(the transaction start slightly precedes the boto3 signing instant, so this is
conservative — never later than the URL's true expiry).

## Response contract (after)

Per upload **item** `data` (each minted PUT URL), additive:

- `expires_in` — unchanged; the presign TTL in seconds (relative).
- `expires_at` — **new**; ISO-8601 UTC instant this URL's signature stops being
  accepted (`server_time + presign_ttl`). Start the PUT before this.

Collection-level `data`, additive:

- `server_time` — **new**; ISO-8601 UTC reference clock. `remaining =
  manifest_deadline − server_time`.
- `manifest_deadline` — **new**; ISO-8601 UTC reaper-enforced deadline for the
  whole upload if it is not finalized.
- `on_expiry` — **new**; `{ "tool": "<create upload tool>", "effect": "re-mint
  replaces the manifest and resets the deadline" }`. The recovery action, named.
- `manifest_mode` / `replaces_prior_manifest` — unchanged.

`suggested_next_actions` stays the linear happy path (`runs.complete_build` /
`systems.provision_defined`). Re-mint is *conditional* recovery, not the next
linear step on a success, so it is named in the structured `on_expiry` hint
rather than jammed into the linear action list (which would wrongly suggest an
immediate re-mint).

## Tool-description contract (gap 3, scope + non-constraint)

Both `create_run_upload` and `create_system_upload` wrapper docstrings
(agent-facing; serialized into the generated tool reference via `just docs`)
gain, in plain factual prose:

- The window's **scope**: the PUT for each URL must *begin* before that URL's
  `data.expires_at`; an in-flight transfer already begun is not interrupted.
- `data.manifest_deadline` is the reaper-enforced deadline for the whole upload;
  `data.server_time` is the reference clock to measure it against.
- **Recovery**: re-calling the tool (`manifest_mode: "replace"`) resets the
  deadline; see `data.on_expiry`.
- **Non-constraint**: `chunks` are for objects larger than the 5 GiB single-PUT
  size limit, not for time pressure.

## Why no formal AI-eval plan

The `/design` AI-surface eval-plan requirement targets surfaces where *this
codebase invokes a model* (an LLM call, prompt, retrieval path, classifier, or
agent loop). This change adds no model call: kdive is the MCP server, and the
"agent" is an external client. The success signal here is **structural** — the
response carries the new fields and the descriptions carry the scope /
non-constraint sentences — and is verified deterministically by unit tests. The
eval cases below are those deterministic assertions, which are the acceptance
criteria `/build-tdd` implements.

## Acceptance criteria

- `AC1` A successful `create_run_upload` collection response carries
  `data.server_time`, `data.manifest_deadline`, and `data.on_expiry` (naming
  `artifacts.create_run_upload` as the recovery tool); each item carries
  `data.expires_at` alongside `data.expires_in`.
- `AC2` `create_system_upload` carries the same fields, with `on_expiry.tool ==
  "artifacts.create_system_upload"`.
- `AC3` `server_time`, `manifest_deadline`, and each item's `expires_at` are
  ISO-8601 UTC strings; `manifest_deadline − server_time == UPLOAD_TTL_SECONDS`
  and `expires_at − server_time == presign_ttl`; `manifest_deadline` equals the
  deadline the reaper reads from the persisted manifest row.
- `AC4` A chunked upload reports one `expires_at` per part item, and one
  collection-level `server_time` / `manifest_deadline` / `on_expiry`.
- `AC5` Existing behavior is unchanged: `expires_in`, `manifest_mode`,
  `replaces_prior_manifest`, `required_headers`, audit row, atomic
  audit+manifest rollback, and all rejection paths still hold.
- `AC6` The generated tool reference (`docs/guide/reference/artifacts.md`, via
  `just docs`) states, for both tools: begin-the-PUT-before-`expires_at` scope,
  the in-flight-not-interrupted clause, the manifest-deadline/`server_time`
  pair, the re-mint recovery, and the chunks-are-for-size-not-time clause; a
  test asserts the scope and non-constraint sentences are present.
- `AC7` `just ci` is green (notably `docs-check` after `just docs`).

## Files in scope

- `src/kdive/artifacts/upload_manifest.py` — `replace_manifest` returns a
  `(server_time, deadline)` stamp (`RETURNING now(), deadline`).
- `src/kdive/mcp/tools/catalog/artifacts/uploads.py` — thread the stamp into the
  item/collection responses; render the new fields.
- `src/kdive/mcp/tools/catalog/artifacts/registrar.py` — wrapper docstrings.
- `tests/mcp/lifecycle/test_create_upload_tool.py`,
  `tests/db/test_upload_manifest.py`, and a registrar-description test — new
  assertions.
- `docs/guide/reference/artifacts.md` — regenerated by `just docs`.
