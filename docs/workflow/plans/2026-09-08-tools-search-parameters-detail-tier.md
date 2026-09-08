# Implementation plan — `tools.search` `parameters` detail tier

**Goal.** Give `tools.search` a middle `detail` tier that returns each parameter's name, type, and
required flag, so an agent that has chosen its tool can build a call without fetching the whole
projected schema.

**Architecture.** One module changes: `src/kdive/mcp/tools/gateway.py`. `SearchDetail` gains a
third member; two module-level helpers render a schema property's type and build the ordered
argument list; `describe_tool` gains one branch. The `tools_search` wrapper docstring and the
`detail` `Field` description are the agent-facing contract and change with the behaviour
(AGENTS.md). Nothing else in the request path moves.

**Tech stack.** Python 3.14, `uv`, `pytest`, `ruff`, `ty`, `just`. FastMCP tool registration.

**Expected implementation size: 190–240 changed lines (M) — derived from the file map below:
one source module (~80), two test modules (~140 combined), plus regenerated artifacts.**

## Global Constraints

- Ruff line length 100; lint set `E,F,I,UP,B,SIM`. `ty` runs whole-tree (`src` + `tests`) with
  strict defaults.
- Guardrails: `just lint`, `just type`, and the full gate `just ci`. Run gates **bare** — never
  through `| tail`/`| head`, never with `>/dev/null` or `|| true`, and never with a trailing
  `; echo $?`. Capture with `just ci > FILE 2>&1 < /dev/null` and read FILE.
- `just format` before committing a Python-only change, so the mutating ruff hooks do not abort
  the commit.
- A fresh worktree needs `just install-mermaid-deps` before `check-mermaid` will pass.
- Doc-style guard, project-wide: use **Milestone**, never "Sprint"; avoid "critical", "robust",
  "comprehensive", "elegant". Applies to ADRs, specs, code comments, and commit messages.
- Never invent `ErrorCategory` strings; this change adds no error path.
- Never pass a PR or issue body as a shell string — write a file and use `--body-file`, scanned
  with `just check-pr-body FILE`.
- Generated artifacts are regenerated with the documented recipe, never hand-edited.
- ADR-0632 is the accepted decision record for this change and is already written; do not
  re-decide its contract here.

## File map

| File | Answerable for |
|---|---|
| `src/kdive/mcp/tools/gateway.py` (modify) | the `SearchDetail` member, the two helpers, the `describe_tool` branch, and the agent-facing docstring/`Field` text |
| `tests/mcp/tools/test_gateway_search.py` (modify) | tier behaviour end to end through `app.call_tool` |
| `tests/mcp/test_gateway_projection.py` (modify) | that the tier reads the projected schema |
| `docs/guide/reference/tools.md`, `src/kdive/cli/commands/_generated_verbs.py`, packaged doc-resource snapshots (regenerate) | generated artifacts derived from the live tool schemas |

## Task 1 — the `parameters` tier in the gateway

Creates nothing. Modifies `src/kdive/mcp/tools/gateway.py`. Tested by
`tests/mcp/tools/test_gateway_search.py` and `tests/mcp/test_gateway_projection.py`.

**Interfaces.** Consumes, already present in this module with these signatures:
`SearchDetail` (a `StrEnum`, currently `SUMMARY = "summary"` and `FULL = "full"`);
`describe_tool(tool: Tool, kinds: frozenset[ResourceKind] | None, *, detail: SearchDetail) ->
dict[str, JsonValue]`; `_project_or_passthrough(tool: Tool, kinds: frozenset[ResourceKind] | None)
-> Tool`; `_summarize(description: str | None) -> str`; `JsonValue` from `kdive.serialization`;
`Tool` from `fastmcp.tools.base`. Provides to nothing outside this module — both new helpers are
module-private. `SearchDetail.PARAMETERS` becomes part of the public tool schema.

**Where it fits.** This is the whole behavioural change; Task 2 only regenerates artifacts.

### Verification

- **Contract: `detail="parameters"` returns the four summary keys plus `parameters`.**
  Mode: focused-test. Test `tests/mcp/tools/test_gateway_search.py::
  test_parameters_tier_returns_the_argument_list`. Expected red before the enum member exists:
  the call fails schema validation on the `detail` value, so `_search`'s `status == "ok"`
  assertion fails. Green: `uv run python -m pytest tests/mcp/tools/test_gateway_search.py -k
  parameters_tier -q`.
- **Contract: entries follow declaration order and mark required properties.**
  Mode: focused-test. Test `test_parameters_entries_follow_declaration_order_and_required`.
  Expected red: `KeyError: 'parameters'`. Green:
  `uv run python -m pytest tests/mcp/tools/test_gateway_search.py -k declaration_order -q`.
- **Contract: each documented schema shape renders its stated type string.**
  Mode: focused-test. Parametrized test `test_type_rendering_covers_each_schema_shape`, driving
  `describe_tool` with a stub tool carrying a crafted schema — one case per documented shape
  (plain, union, array, `$ref`, and the `unknown` fallback). It exercises the rendering through
  the public function rather than importing the private helper, so it does not fail at collection
  and does not pin the helper's name. Expected red: `KeyError: 'parameters'`. Green:
  `uv run python -m pytest tests/mcp/tools/test_gateway_search.py -k type_rendering -q`.
- **Contract: `full` still carries `parameters`, so the tiers stay monotone.**
  Mode: focused-test. Test `test_full_tier_still_carries_parameters`, plus the updated key-set
  assertion in the existing `test_detail_full_adds_schema_and_complete_description`.
  Expected red: the new key is absent from a `full` match. Green:
  `uv run python -m pytest tests/mcp/tools/test_gateway_search.py -k "full_tier or detail_full" -q`.
- **Contract: the tier is cheaper than `full` and dearer than `summary`.**
  Mode: focused-test. Test `test_parameters_tier_is_cheaper_than_full_and_dearer_than_summary`,
  asserting on `len(json.dumps(...))`. Expected red: `KeyError: 'parameters'`. Green:
  `uv run python -m pytest tests/mcp/tools/test_gateway_search.py -k cheaper_than_full -q`.
- **Contract: the tier reads the projected schema, not the raw one.**
  Mode: focused-test. Test `tests/mcp/test_gateway_projection.py::
  test_parameters_tier_uses_the_projected_schema`. Expected red: `AttributeError` on
  `SearchDetail.PARAMETERS`. Green:
  `uv run python -m pytest tests/mcp/test_gateway_projection.py -k parameters_tier -q`.
- **Contract: `names` mode still overrides the tier.**
  Mode: focused-test. The existing `test_names_mode_ignores_summary_detail`, extended to also
  pass `detail="parameters"` and assert the match still carries `input_schema`. Expected red:
  before the enum member exists that value fails schema validation, so the call's
  `status == "ok"` assertion fails. Green:
  `uv run python -m pytest tests/mcp/tools/test_gateway_search.py -k names_mode_ignores -q`.
- **Contract: the wrapper docstring and `detail` `Field` text describe three tiers.**
  Mode: task-test-not-applicable. The changed surface is agent-facing prose inside a docstring
  and a `Field(description=...)`. No executable consumer validates its wording, and a
  prose-snapshot assertion is disallowed by the plan rules. Its downstream generated artifacts
  are covered by Task 2's gates.

### Steps

1. Write the failing tests named in the Verification inventory above, in the two test files
   named there. Run
   `uv run python -m pytest tests/mcp/tools/test_gateway_search.py tests/mcp/test_gateway_projection.py -q`
   and confirm each new test fails with the stated red observation.
2. In `SearchDetail`, add `PARAMETERS = "parameters"` between `SUMMARY` and `FULL`, and update
   the class docstring to cite ADR-0472 and ADR-0632.
3. Add a module-level `_type_name(schema: object, *, depth: int = 0) -> str` above
   `describe_tool`. Return `"unknown"` for a non-`dict` or for `depth` past a small bound. For a
   `str` `$ref`, return the segment after the last `/`. For a `list` under `anyOf` or `oneOf`,
   render each member through `_type_name` at `depth + 1`, de-duplicate preserving order, and
   join with `|`. For a `list` `type`, join its entries with `|`. For a `str` `type`, return it,
   except `"array"`, which returns `f"array[{_type_name(schema.get('items'), depth=depth + 1)}]"`.
   Otherwise return `"unknown"`. Carry a comment naming the bound's purpose: the recursion is over
   server-authored schemas, and the bound keeps a cyclic or pathological one from recursing.
4. Add a module-level `_parameter_digest(parameters: object) -> list[JsonValue]` beside it. Read
   `properties` and `required` from the mapping, defaulting to an empty list and an empty set when
   either is absent or the wrong type, and return one `{"name", "type", "required"}` entry per
   property in mapping order.
5. In `describe_tool`, compute the projected tool once for any tier past `SUMMARY` and use it for
   both new keys: set `described["parameters"]` when `detail` is `PARAMETERS` or `FULL`, and keep
   `description` and `input_schema` on `FULL` alone. Update the function docstring to describe
   three tiers and cite ADR-0632.
6. Rewrite the `detail` `Field` description to name all three tiers, what each returns, the fact
   that `names` mode forces full whatever is passed, and the limit ADR-0632 §3 records — that the
   tier gives the argument list, not value constraints, so a constrained parameter still needs
   `full`. Keep every line within 100 characters.
7. Update the `tools_search` wrapper docstring where it currently says matches carry safety
   metadata "in both modes" and where it teaches the two-step flow, so both read for three tiers.
8. Run `just format`, then
   `uv run python -m pytest tests/mcp/tools/test_gateway_search.py tests/mcp/test_gateway_projection.py -q`
   and confirm every test passes.
9. Run `just lint` and `just type` bare and confirm both exit 0.

**Acceptance criteria.** `SearchDetail` has three members in increasing order of cost. A
`parameters` match carries exactly the five keys named in the spec's Success 1. A `full` match
carries those five plus `description` and `input_schema`. Both new helpers are module-private and
referenced only by `describe_tool`. The `detail` `Field` text and the wrapper docstring describe
three tiers. `just lint` and `just type` are green.

**Rollback.** The change is additive within one module and one enum; reverting the commit restores
the two-tier contract with no persisted state to unwind.

## Task 2 — regenerate the derived artifacts

Creates nothing. Modifies whatever the recipes below rewrite: `docs/guide/reference/tools.md`,
`src/kdive/cli/commands/_generated_verbs.py`, and the packaged doc-resource snapshots.

**Interfaces.** Consumes Task 1's changed tool schema. Provides the committed generated artifacts
the CI gates compare against.

**Where it fits.** Last: the generators read the live registry, so they must run after Task 1.

### Verification

- **Contract: the committed generated artifacts match a fresh generation.** Mode: focused-test —
  the repository's own check recipes are the executable observation. Expected red before
  regeneration: `just docs-check` reports "tool reference is stale — run 'just docs' and commit".
  Green: `just docs-check`, `just cli-verbs-check`, and `just resources-docs-check` each exit 0.

### Steps

1. Run `just docs-check` bare and confirm it fails, naming the stale reference.
2. Run `just docs`, `just cli-verbs`, and `just resources-docs`.
3. Run `just docs-check`, `just cli-verbs-check`, and `just resources-docs-check` bare and confirm
   each exits 0.
4. Review the regenerated diff and confirm it contains only the new `detail` enum value and the
   rewritten `detail`/docstring text — no unrelated churn. Unrelated churn means a generator input
   moved underneath the branch; stop and report it rather than committing it.
5. Run the full gate: `just ci > /tmp/ci-2341.log 2>&1 < /dev/null`, then read the log. The
   recipe's own exit status is the verdict.

**Acceptance criteria.** All three check recipes exit 0, the regenerated diff is confined to this
change's contract text and enum value, and `just ci` exits 0.

**Rollback.** Regenerated files are derived; re-running the recipes on the reverted source
restores them.

## Deferrals carried into implementation

None. No `$trial-loop` deferral was recorded during design.
