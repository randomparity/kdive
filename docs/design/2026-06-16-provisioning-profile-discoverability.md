# Historical provisioning-profile schema spikes (#451)

> **Historical record.** This preserves the original decision or dated evidence.
> Commands, status, paths and capabilities below describe that context; they are not
> current operating guidance. Start with the [current documentation](../README.md).

Record date: 2026-06-16. Decision: [ADR-0124](../adr/0124-provisioning-profile-discoverability.md).

## Spike results (both gating spikes PASS — typed-param branch is taken)

Run against `fastmcp==3.4.0` with the real `ProvisioningProfile` model:

1. **Interception** — a binding-time `pydantic_core.ValidationError` (FastMCP validates the typed
   model *before* the tool body, because `ProvisioningProfile` is `extra="forbid"`) **is catchable
   in `Middleware.on_call_tool`** around `call_next`. The call's arguments (including
   `allocation_id`/`system_id`) are available on `context.message.arguments`, and the tool name on
   `context.message.name`. So a boundary `ValidationError` can be re-enveloped as our
   `configuration_error` envelope.
2. **Client rendering** — the advertised input schema for `profile: ProvisioningProfile` contains
   **no `$ref`/`$defs`/`discriminator`** keys: FastMCP inlines the discriminated `rootfs` union as
   inline `oneOf` objects, and the top-level object advertises `additionalProperties: false`. The
   FastMCP 3.4.0 client renders it and a valid call round-trips (the `#404`/ADR-0113 *output*-schema
   recursion problem does not arise on this input — `ProvisioningProfile` is not self-recursive).

Because both pass, **no `json_schema_extra` fallback is needed**; the param is typed and validation
moves to the boundary, re-enveloped by middleware.
