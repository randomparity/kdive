# Gateway binding recovery guidance

## Problem

`tools.invoke` promises an exact schema lookup after caller binding failures even
when its existing visibility decision withholds that schema. Actual invocation
followed by discovery reproduces an empty result for a caller without grants.
Its wrapper also promises every field error and describes nested disclosure too narrowly.

## Scope and design

Reuse the existing binding provenance classifier and visibility result. Construct
one generic validation detail, then append an exact-name full-schema lookup for
visible binding failures or generic discovery guidance for hidden/missing-context
binding failures. Keep the existing discovery action for both. Bare body failures
retain generic detail and no action. Do not change schema metadata or its gates.

Correct the wrapper docstring and arguments Field description: field-error entries
are bounded per response by the existing constant, overflow is omitted, and recovery
is correcting the reported arguments/retrying or discovering the full schema.
The numeric cap is interpolated from `_FIELD_ERROR_LIMIT` in the Field description.
Metadata requires caller-binding provenance and visibility; nested accepted fields
resolve each referenced top-level property one level, when available.

Existing [visibility](../../adr/0148-rbac-scoped-tool-exposure.md) and
[provenance](../../adr/0844-mcp-validation-provenance-belongs-to-one-classifier.md)
decisions govern. This repair adds no interface, policy, dependency or abstraction.
Regenerate the existing tools.md reference from the same registry descriptions;
verify CLI descriptors stay unchanged. The null alternative retains an observed false recovery promise; removing discovery
for hidden callers discards their existing useful generic action. Keep both existing
behaviors and condition only the precision of the guidance. No new ADR is warranted.

## Failure model

- Actors/deployments: MCP agents invoking the registered gateway on supported hosts;
  proof uses actual in-process FastMCP dispatch and discovery on Linux x86_64.
- Assets: schema privacy, caller provenance, bounded output and actionable recovery.
- Accepted classes: a later request may see changed grants/catalog state; guidance
  reflects the invocation-time decision and does not guarantee future availability.
- Covered elsewhere: inner handler RBAC, catalog policy, telemetry and live transport
  retain existing implementations and tests. No hardware qualification follows.

## Success and evaluation

AI-SPEC: the user is an MCP agent, triggered by invalid invocation arguments. Input
is the registered name, arguments and existing context; output is bounded metadata
and recovery guidance derived only from the classifier, visibility and catalog.
Do not promise hidden schemas or attribute bare body failures to caller binding.
Fallback is generic discovery for hidden binding failures and generic error for
body failures. No model call, new IO or retry is added; success is observed agreement
between actual invocation guidance and discovery, with unchanged privacy and cap.

| Case / severity / gate | Input and observable contract |
| --- | --- |
| Visible happy path / 4 / block | Viewer, runs.get `{}`: exact lookup and discoverable schema. |
| Missing context / 4 / block | Same arguments: generic discovery, empty schema metadata. |
| Hidden regression / 5 / block | No grants: no exact lookup promise; actual search withholds schema. |
| Body provenance / 5 / block | Bare body ValidationError: no metadata or discovery action. |
| Stale source / 4 / block | Existing unknown tool: generic discovery, no fabricated schema. |
| Bounded output / 4 / block | Many invalid fields: existing entry cap, no new looping. |
| Nested shape / 4 / block | Existing required/optional wrappers: one-level metadata when visible. |

Code-based assertions use real dispatch/discovery; no model judge or prose snapshots.
The existing numeric interpolation guard verifies source-to-description derivation.

## Validation

One coupled unit: extend existing invoke tests with visible/no-grant/missing-context
invocation-plus-search cases for authenticated contexts and invocation-only for missing
context; observe two old-source guidance failures and the visible control,
then repair guidance and docs. Run invoke, search and tool-doc modules, deliberate
visibility/provenance faults with exact restoration, lint, whole-tree types and hooks.
Independent design/code/security reviews and a conventional local source commit
precede exact health resolution. Final campaign gates remain separate.
