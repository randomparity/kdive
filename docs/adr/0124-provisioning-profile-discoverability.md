# ADR 0124 — Provisioning-profile discoverability

- **Status:** Accepted
- **Date:** 2026-06-15
- **Deciders:** kdive maintainers

## Context

The provisioning profile is fully modeled as a Pydantic `ProvisioningProfile`
(`src/kdive/profiles/provisioning.py`, refining ADR-0011/0024/0065): required fields, a
discriminated `rootfs`, per-provider sections, and a `boot_method`↔provider pairing rule. But
`systems.define`/`systems.provision` type the parameter as `ProvisioningProfileInput =
Mapping[str, object]` (`src/kdive/profiles/types.py:9`), so FastMCP advertises a freeform
`additionalProperties: true` blob. A new agent has no way to learn the schema from the MCP
surface — MCP-surface testing showed ten evidence-based profile attempts all rejected
identically, with no schema and (pre-ADR-0123) no message. See
`../design/mcp-onboarding-error-ergonomics.md`.

A typed parameter would advertise the schema, but FastMCP validates a typed model **at the
boundary**, before the tool body — so a bad profile would raise FastMCP's own `ValidationError`
and bypass the `configuration_error` envelope. ADR-0113 also flattened *output* schemas because
the FastMCP 3.4.0 client's per-call `TypeAdapter` choked on recursive `$ref`; the client's
handling of a `$defs`/`discriminator` *input* schema is unverified.

## Decision

We will type the `profile` parameter as `ProvisioningProfile` (advertising its JSON schema) and
convert the `pydantic.ValidationError` it raises during input binding into the standard
`configuration_error` envelope using ADR-0123's `detail` + `errors` surfacing, and we will add a
read-only discovery tool `systems.profile_examples` (auth-only, no audit) returning one
ready-to-edit example profile per configured provider — using real inventory references where
available and a marked placeholder otherwise — chained via `suggested_next_actions`. Unlike
`projects.list` (which returns the caller's own token claims), this tool projects the shared
inventory, so its data contract is restricted: it surfaces only non-sensitive catalog identifiers
(provider name, a public image/volume name) and never the `[[remote_libvirt]]` `uri`, `gdb_addr`,
or secret-ref names; catalog images are filtered to `PUBLIC_VISIBILITY` and a private,
project-owned image is shown only to a caller in its owning project. The typed-param half is gated on **two** spikes: (1) that a
binding-time `ValidationError` can be intercepted and re-enveloped (the candidate seam is FastMCP
middleware/error-hook; load-bearing because `ProvisioningProfile` is `extra="forbid"`, so FastMCP
rejects bad input *before* the tool body and before `_runtime_resolution`'s catch), and (2) that
the FastMCP 3.4.0 client renders the `$defs`/discriminator input schema usably. If either fails,
the typed-param half falls back to keeping `profile` as `Mapping[str, object]` (validation stays
in our `parse()`→envelope path, unchanged) with the schema advertised via
`json_schema_extra`. The discovery tool ships regardless, so finding 1 is closed on every branch.

## Consequences

- A new agent can discover a valid profile from the MCP surface alone — the discovery tool is the
  guaranteed-working path independent of client schema rendering.
- On the typed-param branch, boundary validation errors produce the project's envelope (not a raw
  FastMCP error), unifying this with ADR-0123 at the input boundary; on the fallback branch the
  existing `parse()`→envelope path is untouched. Either way malformed input never regresses to a
  raw framework error.
- Typing the param tightens the contract: extra keys previously tolerated under
  `additionalProperties: true` are now rejected (`extra="forbid"`) — an intended change, flagged
  so it is not a surprise.
- New obligations: the two gating spikes (interception, client rendering); an example-validity
  test driving `ProvisioningProfile.parse()` + `validate_profile_for_provider()` directly (not the
  allocation-scoped admission path) so the advertised examples cannot rot; a leak test asserting
  the tool never emits a `uri`/`gdb_addr`/secret-ref and never surfaces another tenant's private
  image; tool-docs/reference wiring for the new tool.
- This refines ADR-0011/0024 (the schema is now advertised, not just enforced) and depends on
  ADR-0123 landing first.

## Alternatives considered

- **Typed param only** (no discovery tool): relies entirely on the FastMCP client rendering a
  discriminated-union input schema usefully; rejected as the sole mechanism because the #404
  history (ADR-0113) makes that risk real, and a worked example is more legible than a schema.
- **Discovery tool only** (keep freeform param): zero schema-rendering risk and most agent-native,
  but the parameter stays untyped so the schema is never machine-advertised; rejected as
  insufficient on its own — chosen as the safety net alongside the typed param.
- **Stored, versioned profile templates**: a heavier catalog surface; rejected as speculative —
  examples answer "what is valid here?" without persistence.
