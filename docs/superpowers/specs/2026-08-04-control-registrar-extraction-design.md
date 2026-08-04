# Control registrar extraction design

## Problem

`kdive.mcp.tools.lifecycle.control.registrar.register` defines all five `control.*` FastMCP
wrappers inside one 256-line function. Each wrapper has an independent signature, schema metadata,
agent-facing docstring, and handler delegation, so changing or reviewing one tool requires
navigating the complete registration block.

The operational handlers are already separate top-level functions. The remaining monolith is only
registration structure, and it differs from the established `runs` registrar, whose public
`register` function is an ordered dispatcher over one focused private helper per tool.

## Decision

Keep every control wrapper in the same module, but move each into a focused private registration
helper:

- `_register_control_power(app, pool)`
- `_register_control_force_crash(app, pool, resolver)`
- `_register_control_diagnostic_sysrq(app, pool, resolver)`
- `_register_control_watch_for_crash(app, pool, resolver)`
- `_register_control_capture_traffic(app, pool, resolver)`

The public `register(app, pool, *, resolver)` function becomes an ordered five-call dispatcher in
the existing power, force-crash, diagnostic-SysRq, crash-watch, traffic-capture order.

Each helper contains its original `@app.tool` decorator, wrapper name and signature,
`Annotated`/`Field` metadata, default and numeric constraints, agent-facing docstring, and handler
delegation without semantic edits. `control.force_crash` remains destructive; the other four remain
mutating. The same pool, current request context, and resolver identities reach the same handlers.

No generic wrapper factory or forwarding lambda is introduced because FastMCP derives the public
schema from the decorated function's signature, metadata, and docstring. No public API or generated
tool reference changes.

## Alternatives

### Stateful registrar object

An object would only store `app`, `pool`, and `resolver`. It adds lifecycle and indirection without
shared behavior, so private functions are clearer.

### Generic registration factory

A factory could reduce decorator repetition but would obscure individual signatures and risk
degrading FastMCP schema reflection. The apparent duplication is the public contract and remains
explicit.

### One module per wrapper

Separate files would fragment five closely related contracts from their directly delegated
handlers. The problem is function structure, not package ownership, so the wrappers stay together.

## Testing

Before extraction, characterization tests record the registered public surface and delegation:

- exact five-tool registration order, names, annotations, maturity metadata, parameters, required
  fields, defaults, capture bounds, descriptions, and wrapper docstrings;
- exact pool, request-context, resolver, and argument forwarding for all five wrappers.

A structural dispatcher test patches the five expected private helpers, calls `register`, and
asserts their order and dependency identities. It fails on the current nested implementation and
drives the extraction. The implementation then runs focused control handler, app registration,
schema/doc, generated-reference, lint, whole-tree type, and full CI checks.

## Scope

This refactor changes registration structure only. It does not change handler logic, authorization,
destructive gating, job payloads, idempotency, provider resolution, public schemas, tool order,
generated artifacts, agent-facing wording, or runtime behavior.
