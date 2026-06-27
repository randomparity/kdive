# Optional client-supplied labels on Runs and Systems (#867)

- **Status:** Accepted
- **Date:** 2026-06-27
- **Issue:** [#867](https://github.com/randomparity/kdive/issues/867) — Optional
  client-supplied names/handles on runs and systems to cut UUID-threading.
- **ADR:** [ADR-0264](../adr/0264-client-supplied-labels.md)

## Problem

A reproduce-and-fix loop juggles many lookalike bare UUIDs — investigation,
allocation, provision job, system, run, build/install/boot jobs, console artifact —
with no client-supplied human handle. One transposed id and the agent operates on the
wrong Run. Surfaced by `BLACK_BOX_REVIEW.md` §3 (🟡).

Verified against `main`:

- `runs.create` accepts no client name/label: the payload is
  `_RunsCreatePayload` (`src/kdive/mcp/tools/lifecycle/runs/registrar.py:56-115`), and
  the success envelope echoes none (`src/kdive/mcp/tools/lifecycle/runs/create.py:117-137`).
- `systems.define` / `systems.provision` accept no client name/label: the params are
  `allocation_id` + `profile` only
  (`src/kdive/mcp/tools/lifecycle/systems/registrar.py:122-156,167-201`).
- `jobs.wait` already does the right thing — `object_id == job_id` with the produced
  resource in `refs.result` (`src/kdive/mcp/responses.py:209,214`); that pattern is not
  changed here.

## Scope

This change covers the issue's well-defined parts only:

1. Accept an optional, freeform client `label` on `runs.create`,
   `systems.define`, and `systems.provision`.
2. Persist it, and echo it verbatim in the `runs.get` / `runs.list` and
   `systems.get` / `systems.list` envelopes (and on the `runs.create` success
   envelope, so the agent confirms its handle round-tripped).

Out of scope — deferred to a follow-up issue, recorded in the ADR's *Considered &
rejected*: letting `runs.*` / `systems.*` tools accept a label **in place of** a UUID
("the run from this investigation" shorthands). That is a separate, larger contract
(uniqueness rules, a lookup scope, and an ambiguous-match error) and is intentionally
not built here.

## Decision summary

- A `label` is a freeform, **non-unique** human handle. It is the caller's own input,
  echoed back like the existing `investigations.title` / `description` fields — not
  machine output — so it is not run through the secret redactor, only length- and
  character-validated.
- `label` is stored as a nullable `text` column on `runs` and `systems`
  (migration `0050`), additive and forward-only (ADR-0015). Existing rows read as
  `NULL` ("no label").
- It is set only where the row is **first minted** (`runs.create`,
  `systems.define`, `systems.provision`). `systems.reprovision` operates in place and
  does not change the label; `runs.bind` attaches a System and does not change the
  Run's label.

## Validation contract

`label` is validated in the **handler/service layer**, not as a pydantic `Field`
bound. The three tools (`runs.create`, `systems.define`, `systems.provision`) sit
behind `BindingErrorMiddleware`, whose per-tool conversions match only the
`profile` / `build_profile` binding errors
(`src/kdive/mcp/middleware/binding_errors.py:150-171`). A hard `max_length` on a
`label` `Field` would raise a `ValidationError` whose `loc` is `("label",)` /
`("request","label")`, which no conversion matches, so the middleware would re-raise
it and the caller would get a raw `ValidationError` instead of the uniform envelope
(the same hazard ADR-0247 and ADR-0259 call out). Validating in the service layer
returns a clean `configuration_error`, matching the `investigations.title`
length-check precedent (`investigations_handlers.py:65-78`).

Rules for a supplied `label` (a single shared `validate_label` helper):

- `None` → accepted (no label).
- Leading/trailing ASCII whitespace is stripped first.
- After stripping, length must be `1..=200` characters; `0` or `>200` →
  `configuration_error`, `data.reason = "invalid_label"`.
- No C0 control characters or DEL (`ord < 0x20` or `ord == 0x7f`) — labels are
  single-line handles, so an embedded newline/NUL/tab is rejected →
  `configuration_error`, `data.reason = "invalid_label"`.
- The error `detail` names the bound and the rule only; it never echoes the rejected
  value (ADR-0123).

The stored value is the stripped string.

## Surfacing contract

- `runs.get` / `runs.list`: add `data.label` (the stored value, or `null`). Built in
  the shared `envelope_for_run` so both read paths and the failed-Run path carry it.
- `systems.get` / `systems.list`: add `data.label` (the stored value, or `null`).
  Built in the shared `system_envelope`.
- `runs.create` success envelope: add `data.label` so the agent confirms acceptance.
- `data.label` is a native JSON string or `null` (ADR-0263), echoed verbatim.

## Acceptance criteria

- `runs.create`, `systems.define`, `systems.provision` accept an optional `label`;
  omitting it is unchanged behavior and stores `NULL`.
- A supplied valid `label` round-trips: it is persisted and appears as `data.label` in
  the create envelope and in the corresponding `get` / `list` envelopes.
- An over-length, empty-after-strip, or control-character `label` returns a
  `configuration_error` with `data.reason = "invalid_label"` and inserts nothing /
  consumes no capacity.
- A whitespace-padded label is stored stripped.
- The redaction, authorization, idempotency, and state-machine behavior of all three
  tools is otherwise unchanged.
- Migration `0050` is additive and forward-only; pre-existing Runs/Systems read
  `label = null`.

## Non-goals

- No label-as-reference resolution (deferred, see Scope).
- No uniqueness constraint or index on `label`.
- No edit path for an existing Run/System label (no `runs`/`systems` "set label" tool);
  the label is fixed at mint time. A follow-up may add editing if needed.
- No change to `jobs.*`, `allocations.*`, `investigations.*`, or artifact handles
  (`investigations` already carry a `title`).
