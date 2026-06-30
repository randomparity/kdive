# ADR-0274: record client-attested source provenance for external builds (#893)

- Status: Accepted
- Date: 2026-06-29

## Context

The external-build lane (`source="external"`) ingests a prebuilt kernel artifact: an agent
builds locally, uploads the bytes, and calls `runs.complete_build` to finalize. KDIVE does
not build from, clone, or verify any source tree on this lane — `runs.create` deliberately
rejects a top-level `kernel_source_ref` for it (`ExternalBuildProfile` carries no source-tree
fields), and `runs.complete_build` accepts only build identity (`build_id`) and boot args
(`cmdline`).

The black-box review (`~/src/linux/BLACK_BOX_REVIEW.md`) found that an agent building locally
had no way to record *which* local source tree/ref produced the uploaded artifacts. The
server-build lane already captures this and surfaces it as `runs.get data.build_provenance`
(`{label, resolved_commit, dirty, tree_sha?}` for a warm tree; `{remote, ref, resolved_commit,
build_host}` for a git source — ADR-0265, ADR-0234 §4) — but that provenance is *captured* by
KDIVE running git in the staged tree. The external lane has no input that records provenance,
so an external Run's `runs.get` shows none.

`build_provenance` is persisted as JSON in `run_steps(step='build').result`
(`BuildStepResult.build_provenance`, value type `dict[str, str | bool]` since ADR-0265) and
surfaced verbatim by `runs.get` (`mcp/tools/lifecycle/runs/common.py`). No column pins its
shape, so feeding it from a new source needs no migration.

`runs.complete_build` is **not** in `BindingErrorMiddleware`'s conversion allowlist
(`mcp/middleware/binding_errors.py`), so a typed Pydantic-model parameter whose structural
validation failed would surface a raw `ValidationError`, not the uniform envelope. Its existing
`cmdline`/`build_id` parameters avoid this by being flat `str | None` validated in the service
layer — the same reason ADR-0264 validates `label` in the service rather than as a `Field` bound.

## Decision

Add two optional flat scalar parameters to `runs.complete_build`, recorded into the existing
`build_provenance` as a **client-attested** claim:

- **`source_label: str | None`** — a freeform human handle for the source tree.
- **`source_ref: str | None`** — the ref/commit the agent claims produced the artifacts.

Flat `str | None` (not a nested model) to mirror `cmdline`/`build_id` and to keep semantic
validation in the service layer so every rejection is a uniform `configuration_error`, never a
leaked binding `ValidationError`.

A new pure helper, `domain/external_provenance.py` (mirroring `domain/labels.py`), validates and
assembles the recorded map:

- Each parameter is whitespace-stripped; empty-after-strip is treated as **absent** (matching
  `complete_build`'s existing `_normalize_cmdline`), not an error.
- A non-empty value must be `1..PROVENANCE_FIELD_MAX_LEN` (256) printable code points, else
  `configuration_error` with `details={"reason": "invalid_source_provenance", "field": <name>}` —
  naming the rule and the field, never the rejected value (redaction posture, as `validate_label`).
- When neither parameter yields a value, the helper returns `None` and `build_provenance` stays
  unset (behaviour identical to today).
- When at least one yields a value, the recorded map is
  `{"client_attested": true, "label"?: ..., "source_ref"?: ...}`.

**`client_attested: true`** is the discriminator. It is present **only** on this client-asserted
provenance; the server-build lane never sets it. An agent reading
`data.build_provenance.client_attested == true` knows KDIVE did not build or verify the source —
the distinction the issue requires between source provenance and build selection/verification.
It is a native JSON boolean (ADR-0263), admitted by the existing `dict[str, str | bool]` coercion.

The validated dict (or `None`) is validated up front in the `runs.complete_build` handler
(alongside the existing cmdline override-token check) and threaded
`CompleteBuildHandlers.complete_build` → `CompleteBuildFinalizer.complete` →
`_finalize_external_build`, where it is set on the `BuildStepResult.build_provenance`. Provenance
is bound at the first successful completion; the already-recorded idempotent replay path returns
the recorded result unchanged.

The fields are the caller's own input echoed back to its own project read, so they are **not** run
through the secret redactor (same posture as `label`, ADR-0264). They are documented as opaque
provenance labels — never cloned, fetched, or resolved — so a credential-bearing URL pasted into
`source_ref` is an opaque string, not a fetch target.

Update the external-lane descriptions (`runs.complete_build`, the `runs.create` `build_profile`
external paragraph, `runs.profile_examples` external note) to mention the optional source
provenance and its client-attested, unverified nature; regenerate the committed tool reference
(`just docs`). No schema, migration, RBAC, or config change.

## Consequences

- An external Run completed with `source_label`/`source_ref` now carries
  `runs.get data.build_provenance` = `{client_attested: true, label?, source_ref?}`. An agent
  distinguishes it from a server build by the `client_attested` key, which the server lane omits.
- An external Run completed without either parameter behaves exactly as before (no
  `build_provenance`) — the capability is purely additive.
- Provenance is bound on first completion (like `cmdline`/`build_id`); a replay returns the
  recorded result and ignores a differing later claim. Validation runs up front on every call, so
  a replay carrying a now-invalid claim would be rejected before the idempotent short-circuit — the
  same up-front posture the existing cmdline override-token check already has.
- The claim is unverified by construction. KDIVE asserts nothing about the named tree; the
  `client_attested` flag and the prose make that explicit so a downstream reader does not mistake it
  for a KDIVE-verified build.
- No in-repo consumer reads `build_provenance` as a fixed server-build shape; the surfacing helper
  passes the map through verbatim, so the external shape rides the same path with no change.

## Considered & rejected

- **Extend `ExternalBuildProfile` with a provenance field.** The build profile is a create-time
  *selection* document; folding a non-selecting provenance label into it reintroduces exactly the
  selection/provenance conflation the issue warns against, and the agent often does not know the ref
  until completion. Rejected.
- **Record provenance in upload declarations.** Provenance belongs with the build/run, not the
  artifact set; routing it through `artifacts.*` adds plumbing for no benefit. Rejected.
- **A separate `data.external_provenance` field on `runs.get`.** Forks the provenance surface into
  two an agent must check; the `client_attested` discriminator on the existing `build_provenance`
  carries the unverified semantics in one place. Rejected.
- **A nested Pydantic-model parameter.** Gives a richer generated schema, but `runs.complete_build`
  is not in `BindingErrorMiddleware`, so a structural error would leak a raw `ValidationError`
  instead of the uniform envelope. Rejected for flat scalar params validated in the service layer.
- **A negative `verified: false` discriminator.** Invites the inference that *absence* of the key
  means verified. A positive `client_attested: true` is unambiguous. Rejected.
- **A `remote` field (and userinfo stripping).** The issue's need is a *local* tree/ref; a remote
  is a speculative field carrying a credential-stripping obligation. Deferred until a concrete need
  appears. Rejected for now.
