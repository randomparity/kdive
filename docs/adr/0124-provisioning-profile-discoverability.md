# ADR 0124 — Provisioning-profile discoverability

- **Status:** Proposed
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
convert any `pydantic.ValidationError` raised during input binding into the standard
`configuration_error` envelope using ADR-0123's `detail` + `errors` surfacing, and we will add a
read-only discovery tool `systems.profile_examples` (modeled on `projects.list`, ADR-0117:
auth-only, no project gate, no audit) that returns one ready-to-edit example profile per
configured provider, populated with real reference names from the `systems.toml` inventory and
chained via `suggested_next_actions`. The typed-param half is gated on a spike confirming the
FastMCP 3.4.0 client renders the discriminated-union input schema; if it does not, the
typed-param half falls back to a hand-authored flattened input schema while the discovery tool
ships regardless.

## Consequences

- A new agent can discover a valid profile from the MCP surface alone — the discovery tool is the
  guaranteed-working path independent of client schema rendering.
- Boundary validation errors now produce the project's envelope (not a raw FastMCP error),
  unifying this with ADR-0123 at the input boundary.
- New obligations: the spike on client input-schema rendering; an example-round-trips-through-
  `systems.define` test so the advertised examples cannot rot; tool-docs/reference wiring for the
  new tool.
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
