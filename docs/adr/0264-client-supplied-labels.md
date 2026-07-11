# ADR-0264: optional client-supplied labels on Runs and Systems (#867)

- Status: Accepted
- Date: 2026-06-27

## Context

A reproduce-and-fix loop threads many lookalike bare UUIDs (investigation,
allocation, system, run, jobs, console artifact) with no client-supplied human handle,
so one transposed id operates on the wrong Run (#867, black-box review §3, 🟡).

Verified against `main`: `runs.create` (`runs/registrar.py:56-115`,
`runs/create.py:117-137`), `systems.define`, and `systems.provision`
(`systems/registrar.py:122-201`) accept no client name/label, and none is echoed.
`investigations` already carry a freeform `title`/`description`
(`src/kdive/mcp/tools/lifecycle/investigations/common.py`), so the codebase already has
a precedent for echoing client-supplied text.

This ADR scopes to the well-defined parts of #867: accept an optional freeform label
and echo it. The proposed "label in place of a UUID" shorthand is a larger, ambiguous
contract and is deferred (see *Considered & rejected*).

## Decision

Add an optional, freeform, **non-unique** `label` to the three row-minting tools —
`runs.create`, `systems.define`, `systems.provision` — persist it, and echo it
verbatim in the read envelopes.

**Storage.** Migration `0050` adds a nullable `text` column `label` to `runs` and
`systems`. Additive and forward-only (ADR-0015); existing rows read `NULL`. The domain
`Run` and `System` models gain `label: str | None = None`; the repository's
field-derived `INSERT` (`db/repositories.py:75-86`) persists it with no repository
change.

**Validation in the service layer, not the schema.** `label` is declared as a
description-only `str | None` parameter (no `Field` length bound) and validated by a
shared `validate_label` helper that returns the stripped value or raises
`configuration_error` (`data.reason = "invalid_label"`). It is validated this way
because all three tools sit behind `BindingErrorMiddleware`, whose per-tool conversions
match only `profile`/`build_profile` binding errors
(`middleware/binding_errors.py:150-171`); a hard `max_length` on a `label` `Field`
would raise a `ValidationError` no conversion matches, which the middleware re-raises
raw rather than as the uniform envelope (the ADR-0247 / ADR-0259 hazard). Rules: strip
Unicode whitespace (`str.strip()`); then require `1..=200` code points and
`str.isprintable()` (one rule that rejects control, format/zero-width/bidi, surrogate,
and non-`U+0020` separator characters, so a label cannot render identically to a
different handle); the error names the bound/rule only, never the rejected value
(ADR-0123). `validate_label` runs as the first step of each handler so an invalid label
inserts no row, takes no lock, and writes no audit record.

**Set at mint only.** The label is set where the row is first created. It is threaded
into both Run insert sites (`admission._insert_run`, the bound path, and the unbound
insert) and the System define/provision handlers. `systems.reprovision` (in place) and
`runs.bind` do not change the label.

**Surfacing.** `data.label` (stored value or `null`, a native JSON string per
ADR-0263) is added in the shared `envelope_for_run` (so `runs.get`/`runs.list` and the
failed-Run path all carry it) and the shared `system_envelope`
(`systems.get`/`systems.list`), and on the `runs.create` success envelope so the agent
confirms its handle round-tripped. The label is the caller's own input — like
`investigations.title` — so it is echoed verbatim and **not** run through the secret
redactor (the redactor governs guest/console/gdb machine output, not request input);
the length/character validation bounds what can be stored.

No RBAC, config, or state-machine change. Idempotency is unchanged for legitimate
retries (same payload), but the validation placement differs by tool: `runs.create`
validates inside `create_run` after the keyed replay resolves, while `systems.*`
validates in the handler before the replay lookup (kept there because
`AdmissionFailureReason` is closed and early validation guarantees no row/audit on
reject). The only observable effect is a keyed retry that changes the label to an
*invalid* value: `runs.create` replays the stored success (ignores the bad label),
`systems.*` returns `invalid_label`. A real retry resends the same valid label and is
identical on both; the divergence is accepted rather than forcing the systems replay
ahead of validation.

## Consequences

- An agent can tag a Run/System at create with a human handle and read it back from
  every `get`/`list` envelope, cutting reliance on bare-UUID matching.
- Pre-existing Runs/Systems and any call that omits `label` are unchanged
  (`label = null`); the surface change is purely additive.
- The label is non-unique and advisory: it is a display handle, not a lookup key. A
  later reference-resolution feature can layer on top without contradicting this
  decision.
- The label is fixed at mint; correcting a typo means creating a fresh Run/System (or a
  later edit tool). Accepted for the MVP — labels are cheap to re-create at this stage.

## Considered & rejected

- **Label-as-reference resolution ("the run from this investigation").** The issue's
  optional part 3. It needs uniqueness rules, a defined lookup scope, and an
  ambiguous-match error contract, and touches every id-taking tool — a separate, larger
  contract. Deferred to a follow-up; this ADR ships the tagging primitive it would build
  on.
- **A hard `max_length` pydantic `Field` bound.** Cleaner-looking, but it raises a
  binding `ValidationError` outside the `BindingErrorMiddleware` conversions for these
  tools, leaking a raw error instead of the uniform `configuration_error` envelope
  (ADR-0247, ADR-0259). Service-layer validation returns the clean envelope.
- **A unique index on `label`.** Would let the label serve as a stable reference, but
  the chosen scope is freeform tagging; uniqueness adds a create-time violation path and
  only pays off once reference resolution exists. Revisit with that feature.
- **Run the label through the secret redactor before echoing.** The redactor targets
  registered secret values in machine output (guest/console/gdb), not the caller's own
  request text; `investigations.title`/`description` are already echoed verbatim. Length
  and character validation, not redaction, is the right bound for request input.
- **Separate `name` and `label` fields.** The issue lists "name/label" as alternatives,
  not two fields. One freeform handle covers the ergonomic need without a second
  near-synonym field.
- **Also accept the label on `systems.reprovision` / `runs.bind` / an edit tool.**
  Those mutate an existing row; setting a label there is an edit path, separate from the
  mint-time tagging this change adds. Deferred until an edit need is shown.
- **Label only `systems.define` (the issue's literal example).** `systems.provision` is
  the sibling no-upload mint lane; labeling only `define` would leave the common path
  unable to tag a System. Both mint a System, so both accept the label; `reprovision`
  (in place) does not.
