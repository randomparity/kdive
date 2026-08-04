# Generated CLI argument migration design

## Goal

Complete the generated CLI migration by removing the compatibility copy that exposes
descriptor-owned `genarg_*` values under their historical unprefixed names. Generic dispatch and
specialized handlers must consume one protected argument boundary.

## Current problem

Generated parsers deliberately store MCP parameter values under `genarg_*` destinations so a tool
parameter cannot overwrite routing state such as `command`, `subcommand`, or `json`. Generic
dispatch already rebuilds tool payloads from that protected namespace. Specialized handlers still
read unprefixed attributes, so `registry._adapt_handler_args` copies every generated value back onto
the parsed namespace immediately before dispatch.

The adapter keeps the old namespace alive, makes collision protection conditional on which handler
runs, and leaves two argument conventions active in one command surface. `GeneratedVerb` also
retains a nullable `confirm_destructive` compatibility default even though every generated
descriptor supplies the value explicitly.

## Design

Add `kdive.cli.commands.generated_args` as the sole owner of generated argument access. It provides:

- `GENERATED_ARG_PREFIX`, used by parser construction and tests;
- raw protected lookup for payload iteration;
- typed required and optional projections at the dynamic `argparse.Namespace` boundary;
- parser-local acknowledgement lookup for flags such as `force` and `expired`; and
- descriptor-driven payload assembly, moved from `dispatch` without changing its rules.

The typed projections validate the parser contract instead of returning `Any`. Required values
must be present and have the requested type. Optional values may be absent but, when present, must
have the requested type. Exact integer checks reject booleans. An impossible namespace shape fails
with an actionable error naming the generated argument.

`registry` continues to own parser and route construction, but imports the prefix and passes the
original parsed namespace directly to handler overrides. `dispatch.invoke_generated_verb` calls the
shared payload assembler. `reads`, `images`, and `mutations` replace direct MCP-parameter attribute
reads with the typed generated accessors.

Routing and local control fields remain separate:

- `command` and `subcommand` are route selectors owned by `registry`;
- `json` selects rendering;
- `yes` discharges generic destructive confirmation; and
- descriptor local flags such as `force` and `expired` are read only through the local
  acknowledgement helper.

None of those fields enters generated payload assembly. The existing refusal messages and local
acknowledgement behavior remain unchanged.

Payload assembly preserves all existing behavior: omit `None`, omit an unset `store_true`, retain
an explicit false `bool_optional`, preserve appended values, parse validated JSON containers, and
wrap request bodies only when a non-empty body exists.

Make `GeneratedVerb.confirm_destructive` a required `bool`. The generator and committed descriptors
already provide it. Update the two test builders that construct descriptors manually, remove the
nullable fallback from parser construction, and keep the generated artifact unchanged.

## Alternatives considered

Passing a new `GeneratedInvocation` object to all specialized handlers would encapsulate the
namespace more completely, but it would change 23 handler and test call signatures for no needed
behavior. Retiring handler overrides in favor of renderer and payload-transform callbacks would be
a broader CLI architecture change. A neutral accessor and payload module completes the current
migration without either expansion.

## Verification

Start with real-parser tests that pass the untouched protected namespace into representative read,
image, and mutation handlers; they must fail while the compatibility adapter is required. Add unit
coverage for typed projections, payload omission and wrapping rules, local-flag isolation, and the
required `confirm_destructive` field. Keep the all-override schema/payload guard as the subsystem
proof, regenerate the CLI artifact in check mode, and run repository lint, type, focused CLI tests,
and full CI.

The migration is complete when no production path copies `genarg_*` values onto unprefixed names,
specialized handlers do not read descriptor-owned parameters directly from `Namespace`, generated
artifacts remain in sync, and the public CLI paths and MCP payloads are unchanged.
