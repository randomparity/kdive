# ADR 0469 — Every CLI verb's argparse shape is guarded against its tool schema

- **Status:** Accepted
- **Date:** 2026-07-27
- **Issue:** #1611
- **Epic:** #1576 (closing issue)
- **Amends:** [ADR-0089](0089-operator-cli-mcp-client.md)'s curated `Verb` registry, which declares
  a hand-written argparse shape and a target tool but never reconciled the two, and
  [ADR-0423](0423-generic-generated-verb-dispatch.md)'s generator, whose drift check compares the
  committed descriptors against a fresh generation and never against a schema.

## Context

A curated `Verb` in `src/kdive/cli/commands/registry.py` **overrides** the generated argparse shape
at its derived path. Its payload is then assembled by hand-written Python in `reads.py`,
`mutations.py`, or `images.py`. Nothing compared either half — the flags or the payload — to the
JSON schema of the tool the verb calls.

Three guards already existed and none of them closes that gap:

| guard | proves | blind to |
|-------|--------|----------|
| `test_every_live_tool_is_covered` | each registered tool has exactly one descriptor | any parameter |
| `cli-verbs-check` / `test_committed_module_is_in_sync` | the committed artifact equals a fresh generation | whether the generation is *right* |
| `tests/mcp/test_read_tools_annotated.py` | a curated `read_only` verb targets a read-only tool | the payload |

All three are artifact-vs-artifact or annotation-only. The consequence is that reshaping a tool
leaves the curated verb dispatching its old payload with CI green — which happened three times
inside epic #1576 alone:

- **#1589** — `images build` and `images publish` sent `{"request": {...}}` to tools
  [ADR-0372](0372-flat-params-for-mutation-tools.md) had flattened. Both verbs were broken, and
  `tests/cli/test_images_verbs.py` *pinned the broken nested shape* against a fake client, so the
  suite actively protected the bug.
- **#1590** — deleting the curated `fixtures list` verb left the documented replacement path
  (`images list --scope public_baseline`) non-existent, because `images list` is itself curated and
  had no `--scope` flag. Found by a manual run, not a guard.
- **#1588** — a `request`-wrapped discriminated union made the generator emit a verb with zero
  flags and zero JSON escapes. `cli-verbs-check` passed because it compared an empty generation
  against an equally empty commit. (The specific instance is fixed; the class was not guarded.)

The common shape of all three: a fact about a *schema* was needed, and every existing check read
only artifacts.

## Decision

### 1. Curated verbs are guarded by payload capture, not by static analysis

A curated verb's payload is arbitrary Python — `ledger_get` builds a discriminated `target` object
from two mutually exclusive flags; `ledger_report` composes `--since`/`--until` into a `window`
pair. No static declaration predicts that, and adding one would just be a second hand-maintained
copy to drift.

So each curated verb is **driven over the real parser** (`build_parser()`, the same tree
`kdivectl` uses) with a capturing fake client substituted at the `_session_factory` seam, and every
payload it emits is validated against the tool's live JSON schema with `jsonschema`.

Because every tool schema is `additionalProperties: false`, that single validation covers the
whole class the issue enumerates — a payload key the tool no longer has, a required property sent
as `null`, a flat payload sent to a `request`-wrapped tool, and a wrapped payload sent to a
flattened one — without a separate assertion per rule, and without a name-level flag-to-property
comparison that would false-positive on the composed parameters (`--since` is a real flag that is
correctly *not* a payload key).

### 2. The invocation matrix is derived from each verb's own descriptor

The rows are the minimal command line the parser accepts (positionals, required options, and every
`store_true` flag, since those gate the call), plus one row adding every optional option at once and
one row per optional option alone. Values come from the schema: a member value for an enum or
`const`, a parsable number for a numeric parameter, an opaque identifier otherwise.

Nothing is written per verb, so a new curated verb is covered the day it is added and there is
nothing to keep in sync. The rule this encodes is stronger than "the shape is declarable":
**every argv the parser accepts must produce a schema-valid payload.**

A verb that refuses a row up front — `accounting usage` demands exactly one of `--project` /
`--investigation-id`, so both the none row and the both row are usage errors — emits no payload for
it and is judged on the rows it does emit. A verb that emits none at all is itself a failure.

### 3. Generated verbs are guarded structurally

Their payload assembly is mechanical (`dispatch._assemble_generated_payload`), so the descriptor is
compared to the schema directly: every required property is reachable through a required flag or a
JSON escape, no flag or JSON param names a property the schema lacks, and no required property sits
behind an optional flag. Payload capture buys nothing here and would require synthesizing valid
nested objects for the 16 verbs with required JSON params.

### 4. A curated enum parameter reads its `choices` off the generated verb at the same path

A curated `Verb` has nowhere to spell an enum, so `resources list --kind`, `systems list --state`,
and `images list --scope` all accepted anything and deferred to the server. Restating the members
by hand would be a second copy that goes stale the moment an enum grows a member.

Instead `_curated_choices` reads them from the *generated* verb at the same path, whose flags the
generator derives from the live schema and `cli-verbs-check` keeps in sync. This is derivation, not
duplication: a parameter with no generated counterpart, or one whose property carries no `enum`,
contributes nothing.

### 5. The generator resolves `#/$defs` references and refuses an uninvokable verb

A reused model — a `StrEnum` in particular — can render as a bare `$ref`. Unresolved it looks
typeless, so it loses `enum` → `choices` and falls through to `--<param>-json`, which then rejects
the bare string the enum actually accepts (#1584). `_resolve_ref` follows local `#/$defs` targets;
a dangling or cyclic reference degrades to the JSON escape rather than raising. No tool schema
carries a `$ref` today — fastmcp inlines them — so this changes no committed descriptor; it removes
a latent trap.

Separately, a tool with required parameters that derives neither a flag nor a JSON escape is now a
`ValueError` at generation. This is the one check that cannot live in a drift comparison, because
the drift comparison is exactly what an empty generation satisfies.

### 6. No exclusion lists

A mismatch the guard reports is a broken verb. Every mismatch found on the current tree was fixed
in the verb (see Consequences); none was waived.

## Consequences

- **Eight schema-required parameters move from `options` to `required_options`.** `ops
  force-teardown --reason`, `ops force-release --reason`, `images upload
  --project/--name/--arch/--quarantine-key`, `images prune-expired --reason`, and `images extend
  --seconds/--reason` were all optional on the command line while required by the tool, so omitting
  one sent `null` — and `images extend` without `--seconds` raised an uncaught `TypeError` from
  `int(None)` rather than any error a caller could act on. These are now usage errors (exit `2`)
  up front, which is what `required_options` was introduced for.
- **`kdivectl inventory list --project` was broken and is fixed.** `inventory.list` takes its
  filters inside a `request` wrapper like the other list tools; the verb sent them flat, which the
  schema rejects. This is the #1589 class, still live on `main`, found by the guard on its first
  run.
- **Three curated flags gain `choices`**, so a misspelled value is a usage error instead of a
  server round trip, and `--help` and the completion tree now list the legal values.
- **A test that pinned a wrong shape is corrected.**
  `test_inventory_show_json_emits_whole_envelope_and_passes_project_filter` asserted the flat
  payload the tool rejects — the same anti-pattern #1589 hit. The guard makes shape-pinning tests
  safe to keep: they now sit behind a check that the pinned shape is the schema's.
- **The registry is unchanged at 123 tools.** This issue adds a guard and removes nothing.
- **One more app build in the test suite.** The guard reads the live schemas from a
  module-scoped fixture, as `tests/scripts/test_gen_cli_verbs.py` already does; its 432
  parametrized cases run in about four seconds.
- **`_verb_parser`'s dead `packages` special case is removed.** No curated verb declares an array
  option, and if one ever does the guard now catches the resulting payload type mismatch rather
  than relying on a pre-emptive branch.

## Alternatives considered

- **Declare each curated verb's payload shape as data and check the declaration.** Rejected: the
  declaration is a third hand-maintained copy that can drift from the handler, which is the failure
  mode being fixed. Capturing the payload the handler actually sends has no such gap.
- **Compare curated flag names to schema property names.** Rejected: it false-positives on every
  composed parameter (`--since`/`--until` feed `window`; `--project`/`--investigation-id` feed
  `target`) and would need an exemption list to go green — the one thing this campaign forbids.
- **Validate payloads by calling the real tools.** Rejected: it needs a live server and a database,
  turns a 4-second unit guard into an integration suite, and would exercise authorization rather
  than shape.
- **Restate the enum members on the curated `Verb`.** Rejected per decision 4; deriving them from
  the generated sibling cannot go stale.
- **Put the flagless-but-required check only in the test.** Rejected: `just cli-verbs` would still
  write an uninvokable descriptor, and the developer running the generator is the one who should
  see the failure.
