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
length-check precedent (`src/kdive/mcp/tools/lifecycle/investigations/common.py`).

Rules for a supplied `label` (a single shared `validate_label` helper):

- `None` → accepted (no label).
- Leading/trailing whitespace is stripped first with `str.strip()` (Unicode
  whitespace).
- After stripping, length must be `1..=200` Unicode code points (`len(label)`); `0`
  (empty after strip) or `>200` → `configuration_error`,
  `data.reason = "invalid_label"`.
- The stripped value must be printable — `label.isprintable()` must be `True`. This is
  the single character rule and it subsumes the control-character check: `isprintable`
  is `False` for any character in a control (`Cc`, incl. NUL/newline/tab and the C1
  range), format (`Cf`, incl. zero-width and bidi-override characters), surrogate
  (`Cs`), or non-`Cn` separator (`Zl`/`Zp`/`Zs`) category, while allowing the ASCII
  space (`U+0020`) so an interior space is fine. A non-printable character →
  `configuration_error`, `data.reason = "invalid_label"`. Rejecting format/separator
  characters keeps a label from rendering identically to a different label, which is the
  whole point of the handle (a confusable handle would defeat the disambiguation goal).
- The error `detail` names the bound and the rule only; it never echoes the rejected
  value (ADR-0123).

The stored value is the stripped string.

**Ordering.** `validate_label` runs as the **first step** of each tool's
handler/service path — before target/allocation resolution, advisory-lock acquisition,
capacity or reuse re-assertion, the row `INSERT`, and any audit-log write. This is what
makes the "inserts nothing / consumes no capacity / writes no audit row on an invalid
label" guarantee structural rather than incidental: in `admission.create_run` the
audit event is written inside `_insert_run` *after* `RUNS.insert`, and the bound path
acquires locks in `_create_locked`, so a label rejected anywhere later than the top of
`create_run` would already have taken a lock or written a row.

**Idempotency interaction.** Under a replayed `idempotency_key`, the stored success
envelope wins (`_idempotency.py`), so a repeated call with the same key but a *changed*
`label` returns the first call's stored `label` and ignores the new one. This is the
existing replay contract — the label is request input that the key dedups — not a new
behavior; it is called out here so the precedence is not a surprise.

One cross-tool nuance follows from where each tool validates: `runs.create` validates
the label inside `create_run`, *after* the keyed path resolves a replay, so a keyed
retry whose label changed to an *invalid* value still replays the first call's stored
success (the bad label is ignored). `systems.define` / `systems.provision` validate in
the handler *before* the replay lookup (the placement that keeps `AdmissionFailureReason`
closed and guarantees no row/audit on reject), so the same keyed retry with a *changed
invalid* label returns `invalid_label` instead of replaying. A legitimate idempotency
retry resends the same valid label and is unaffected on both; only a malformed retry
payload sees the difference. Accepted divergence — exact parity would force the systems
replay lookup ahead of validation and weaken the no-DB-before-reject property.

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
- An over-length, empty-after-strip, or non-printable (control / zero-width / bidi /
  non-ASCII-space) `label` returns a `configuration_error` with
  `data.reason = "invalid_label"` and inserts no Run/System row, writes no audit-log
  row, and consumes no capacity.
- A whitespace-padded label is stored stripped; an interior ASCII space is preserved.
- The redaction, authorization, idempotency, and state-machine behavior of all three
  tools is otherwise unchanged. Under a replayed `idempotency_key`, the stored
  envelope's `label` wins (a changed label on a replay is ignored by design).
- Migration `0050` is additive and forward-only; pre-existing Runs/Systems read
  `label = null`.

## Non-goals

- No label-as-reference resolution (deferred, see Scope).
- No uniqueness constraint or index on `label`.
- No edit path for an existing Run/System label (no `runs`/`systems` "set label" tool);
  the label is fixed at mint time. A follow-up may add editing if needed.
- No change to `jobs.*`, `allocations.*`, `investigations.*`, or artifact handles
  (`investigations` already carry a `title`).
