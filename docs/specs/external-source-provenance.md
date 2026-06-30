# External-build source provenance (#893)

## Problem

`runs.create` rejects a top-level `kernel_source_ref` for an external-build Run, and that
rejection is correct: the external lane (`source="external"`) ingests a prebuilt artifact and
deliberately omits source-tree fields, because KDIVE neither builds from nor verifies that
tree. But an agent that builds a kernel locally and uploads the artifacts has no way to record
*which* local source tree/ref produced them. The server-build lane already captures and surfaces
this as `runs.get data.build_provenance`; the external lane has no input that records it.

## Goal

Let an agent attach a freeform **source-provenance claim** to an external-build Run, surfaced via
`runs.get data.build_provenance`, while keeping it unmistakably distinct from KDIVE-captured
server-build provenance: the claim must never imply that KDIVE built from or verified the named
tree.

## Non-goals

- KDIVE does not clone, fetch, resolve, or verify the named source. The fields are opaque labels.
- No new MCP tool, no DB schema/migration, no RBAC change.
- Provenance is bound at the first successful completion (like `cmdline`/`build_id`); there is no
  separate after-the-fact provenance mutation.

## Design

### Surface: two flat params on `runs.complete_build`

`runs.complete_build` gains two optional scalar params alongside the existing `cmdline`/`build_id`:

- `source_label: str | None` — a freeform human handle for the source tree (e.g. `"my-fix-branch
  worktree"`).
- `source_ref: str | None` — the ref/commit the agent claims produced the artifacts (e.g. a git
  SHA or `"v6.9-rc1+patch"`).

Flat `str | None` params (not a nested Pydantic model) for two reasons: they mirror the existing
`cmdline`/`build_id` shape of this tool, and `runs.complete_build` is **not** in
`BindingErrorMiddleware`'s conversion allowlist — a typed model param whose structural validation
failed would leak a raw `ValidationError` instead of the uniform envelope. Scalar string params
never fail binding on content, so all semantic validation happens in the service layer and always
produces a uniform `configuration_error` envelope (the same reasoning ADR-0264 used for `label`).

### Recorded shape: reuse `build_provenance` with a positive discriminator

The validated claim is recorded into the existing `BuildStepResult.build_provenance`
(`run_steps.result` jsonb) and surfaced unchanged by `runs.get data.build_provenance`. The recorded
map carries a positive boolean discriminator:

```json
{ "client_attested": true, "label": "<source_label>", "source_ref": "<source_ref>" }
```

- `client_attested: true` is present **only** on external/client-asserted provenance; the
  server-build lane never sets it. An agent reading `data.build_provenance.client_attested == true`
  knows KDIVE did not build or verify the source — this is the distinction the issue requires.
- `label` / `source_ref` are included only when the corresponding param was supplied non-empty.
- Reusing `build_provenance` (rather than a second `external_provenance` field) keeps one provenance
  surface agents already read; the discriminator carries the "unverified" semantics.

### Validation (service layer, pure)

A new pure helper `domain/external_provenance.py` mirrors `domain/labels.py`:

- Each supplied param is whitespace-stripped; an empty-after-strip value is treated as absent
  (matching `complete_build`'s existing `_normalize_cmdline`).
- A non-empty value must be `1..PROVENANCE_FIELD_MAX_LEN` (256) printable code points, else
  `configuration_error` with `details={"reason": "invalid_source_provenance", "field": <name>}` —
  the message and details name the rule and the field, never the rejected value (redaction posture,
  matching `validate_label`).
- When neither param yields a value, the helper returns `None` and `build_provenance` stays unset.
- The fields are the caller's own input echoed back to its own project read, so they are **not** run
  through the secret redactor (same posture as `label`, ADR-0264). They are documented as opaque
  labels, never cloned or resolved, so a credential-bearing URL pasted into `source_ref` is treated
  as an opaque string, not a fetch target.

### Threading

`runs.complete_build` handler validates the two params up front (alongside the existing cmdline
override-token check), returning a `configuration_error` envelope on rejection. The validated dict
(or `None`) flows `CompleteBuildHandlers.complete_build` → `CompleteBuildFinalizer.complete` →
`_finalize_external_build`, where it is set on the `BuildStepResult.build_provenance`. The
already-recorded idempotent replay path is unchanged: provenance is bound on the first completion,
and a replay returns the recorded result.

## Acceptance criteria

1. `runs.complete_build` with `source_label` and/or `source_ref` records the claim, and `runs.get`
   surfaces it as `data.build_provenance` with `client_attested: true` plus the supplied fields.
2. `runs.complete_build` with neither param behaves exactly as today (no `build_provenance`).
3. An invalid `source_label`/`source_ref` (over-cap or non-printable) is rejected as
   `configuration_error` with `reason=invalid_source_provenance` and the offending `field`, naming
   no value.
4. An empty-after-strip param is treated as absent, not an error.
5. The recorded provenance round-trips through `BuildStepResult.dump`/`load` and the
   `_optional_provenance_map` coercion (str/bool values only).
6. Provenance is bound on first completion; an idempotent replay returns the recorded result.

## Considered & rejected

- **Extend the external build profile with a provenance field.** The profile is a create-time
  *selection* document; mixing a non-selecting provenance label into it is exactly the
  selection/provenance conflation the issue warns against, and the agent may not know the ref at
  create time. Rejected.
- **Record provenance in upload declarations.** Provenance belongs with the build/run, not the
  artifact set; more plumbing for no benefit. Rejected.
- **A separate `data.external_provenance` field on `runs.get`.** Forks the provenance surface into
  two an agent must check; the `client_attested` discriminator on the existing field is enough.
  Rejected.
- **A nested Pydantic-model param.** Better generated schema, but `runs.complete_build` is not in
  `BindingErrorMiddleware`, so a structural error would leak a raw `ValidationError`. Rejected in
  favour of flat scalar params validated in the service layer.
- **`verified: false` discriminator.** A positive `client_attested: true` is unambiguous; a
  negative flag invites the inference that its *absence* means verified. Rejected.
- **A `remote` field.** The issue's need is a *local* tree/ref; a remote adds a speculative field
  and a userinfo-stripping obligation. Deferred until a concrete need appears.

See [ADR-0274](../adr/0274-external-source-provenance.md).
