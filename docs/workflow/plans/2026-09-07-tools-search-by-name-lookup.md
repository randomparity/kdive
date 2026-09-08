# Implementation plan — `tools.search` by-name lookup and diagnosable miss (#2305)

**Goal.** Give a gateway-mode agent a deterministic way to turn a tool name it was handed into that
tool's schema, make a zero-match query say why it missed, and put the recipe in the text the agent
reads before its first call.

**Architecture.** `tools.search` is a FastMCP wrapper in `src/kdive/mcp/tools/gateway.py` over
module-private helpers `_score` / `_rank` / `describe_tool`. It RBAC-filters the registry through
`tool_visible`, ranks the survivors, and returns a `ToolResponse` whose `data` carries `matches`,
`truncated`, and mode-specific keys. This change adds a third selection mode (`names`), threads a
miss reason out of `_rank`, and updates the two agent-facing texts: the wrapper docstring and
`Field` descriptions, and `_GATEWAY_ON_SURFACE` in `src/kdive/mcp/schema/tool_index.py`.

**Tech stack.** Python 3.14, `uv`, FastMCP, pydantic v2, pytest.

Expected implementation size: 300–420 changed lines (M) — from the file map below: ~125 lines in
`gateway.py`, ~8 in `tool_index.py`, ~215 in the two test files, and ~45 across the two
regenerated artifacts.

## Global Constraints

- Design of record: [ADR-0630](../../adr/0630-by-name-tool-lookup-and-diagnosable-search-miss.md),
  amending [ADR-0472](../../adr/0472-summary-first-tool-search.md) §2. ADR-0472 §2's guarantee —
  a query that is exactly a tool name outranks every other hit — must not regress.
- Spec of record: `docs/workflow/specs/2026-09-07-tools-search-by-name-lookup-design.md`.
- Every change is additive. A call that passes no `names` and whose `query` or `namespace` finds
  something must produce a byte-identical response to today's.
- FastMCP serialises only the `@app.tool` wrapper's docstring and its `Field(description=...)`
  text into the agent-visible schema (`AGENTS.md`, "The wrapper docstring is the agent-facing
  contract"), so contract text goes there and not only on a helper, and a parameter whose meaning
  another parameter changes says so in its own description.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM`. `ty` runs whole-tree with strict defaults.
  Doc-style guard: no "critical", "robust", "comprehensive", "elegant"; "Milestone" not "Sprint".
- Guardrails: `just lint`, `just type`, `just test`, and before any doc check, `just docs`
  (regenerates `docs/guide/reference/tools.md` from the live registry) then `just docs-check`.
  `just ci` is the full gate; run it as `just ci > FILE 2>&1 < /dev/null`, never through a pipe
  and never with a trailing `; echo $?`.
- `just adr-status-check` fails any ADR whose Status keyword is `Proposed` while cited from `src/`
  or `tests/`. ADR-0630 is written `Accepted`, so its citation from `gateway.py` is safe in any
  commit. There is no ADR index table to update (`docs/adr/README.md`).
- Branch `feat/tools-search-by-name-2305`, base `main`. Deferrals carried in: none.

## File map

| Path | Created / changed | Answerable for |
|---|---|---|
| `src/kdive/mcp/tools/gateway.py` | changed | `_NAMES_MAX`, `_select_named`, the `names` parameter, response assembly, the `SearchMiss` enum, `_rank`'s reason return, and the agent-facing docstring / `Field` text |
| `src/kdive/mcp/schema/tool_index.py` | changed | `_GATEWAY_ON_SURFACE`: the recipe a gateway-mode agent reads first |
| `tests/mcp/tools/test_gateway_search.py` | changed | `names` mode, its edges, and `reason` behaviour |
| `tests/mcp/test_tool_index.py` | changed | The gateway-on instructions naming the parameters |
| `docs/guide/reference/tools.md` | regenerated | CI-gated snapshot of the live registry (`just docs`) |
| `src/kdive/cli/commands/_generated_verbs.py` | regenerated | CI-gated `kdivectl` verb descriptors, which embed `tools.search`'s first docstring line and its `Field` help text (`just cli-verbs`) |

## Task 1 — `names` mode on `tools.search`

**Creates/modifies:** `src/kdive/mcp/tools/gateway.py`.
**Tests:** `tests/mcp/tools/test_gateway_search.py`.

**Interfaces.** Consumes, all confirmed present in `src/kdive/mcp/tools/gateway.py` or its
imports with these signatures: `SearchDetail` (a `StrEnum` with `SUMMARY` / `FULL`),
`describe_tool(tool: Tool, kinds: frozenset[ResourceKind] | None, *, detail: SearchDetail) ->
dict[str, JsonValue]`, `_namespace_signal(all_tools: list[Tool], namespace: str, *, any_visible:
bool) -> tuple[NamespaceStatus, list[str]]`, `tool_visible(name: str, ctx) -> bool` from
`kdive.mcp.exposure`, `registered_tools(app)` from `kdive.mcp.schema.schema_advertising`,
`_SEARCH_LIMIT_MAX = 50`, and `ToolResponse.success(object_id, status, *, data=...)`. Provides to
Task 2 the `if names is not None: … else: …` selection block and the mode-ordered `data` assembly
Task 2 extends with the `reason` branch, and to Task 3 the names `names` and `unknown_names`.

**Where it fits.** The first of the issue's three parts, the one the other two describe.

### Verification

Every entry is `Mode: focused-test`, in `tests/mcp/tools/test_gateway_search.py`. All seven share
one expected red before this task — `fastmcp.exceptions.ValidationError`, because `tools.search`
has no `names` parameter and binding the argument fails — and one green command:
`uv run python -m pytest tests/mcp/tools/test_gateway_search.py -k names -q`.

- **Contract:** `names` selects exactly the named RBAC-visible tools, in caller order, at full
  detail, with `truncated: false`. Test
  `::test_names_returns_exactly_those_tools_at_full_detail`.
- **Contract:** an unresolved requested name appears in `data.unknown_names`, sorted and
  de-duplicated, and never in `matches`. Test `::test_names_reports_unknown_and_hidden_names`.
- **Contract:** a name whose tool `tool_visible` excludes for the calling context is not returned.
  Test `::test_names_mode_is_rbac_filtered`, calling with `_viewer_ctx` for a tool only
  `_operator_ctx` can see.
- **Contract:** `names` overrides `detail="summary"`. Test
  `::test_names_mode_ignores_summary_detail`, asserting `input_schema` present.
- **Contract:** `names` ignores `limit` and carries no `reason`. Test
  `::test_names_mode_ignores_limit_and_carries_no_reason`, asserting `len(matches) == 2` and
  `truncated is False` for `names=[two names], limit=1`.
- **Contract:** `names` takes precedence over `namespace` and `query`. Test
  `::test_names_takes_precedence_over_namespace_and_query`, asserting the response carries no
  `namespace_status`.
- **Contract:** entries are stripped and lower-cased, duplicates collapse into first position,
  and the 1–10 bound is enforced by schema validation. Tests
  `::test_names_normalises_and_deduplicates` (`names=[" Runs.Boot ", "runs.boot"]` yields one
  match), `::test_names_cardinality_rejects_out_of_bounds` (`[]` and an 11-element list both
  raise), and `::test_names_cardinality_accepts_the_ceiling`, which pins the ten-name boundary as
  accepted so the rejection test cannot pass merely because the parameter is unknown.
- **Contract:** a names-mode miss is logged with counts, never the names. Test
  `::test_names_miss_is_logged_with_counts_only`, using `caplog` at `INFO` on
  `kdive.mcp.tools.gateway`; its expected red is instead that no `tool_search_names_miss` record
  exists.

### Steps

1. Add the cardinality bound beside `_SEARCH_LIMIT_MAX` in `src/kdive/mcp/tools/gateway.py`:

   ```python
   # `names` forces full detail and ignores `limit`, so its own bound is the only one left.
   # Ten is `limit`'s default, and ADR-0472 measured a full match at roughly 2.5 KB (ADR-0630 §2).
   _NAMES_MAX = 10
   ```

2. Add the by-name selector immediately after `_rank`:

   ```python
   def _select_named(candidates: list[Tool], names: list[str]) -> tuple[list[Tool], list[str]]:
       """Return the visible tools for ``names`` in caller order, plus the unresolved names.

       Each name is stripped and lower-cased, matching what query mode already does to its
       tokens, and a name repeated after normalisation is returned once in its first position.
       A name absent from ``candidates`` is unresolved whether no tool carries it or the
       caller's grants hide it; the two are deliberately not distinguished (ADR-0630 §3).
       """
       by_name = {t.name.lower(): t for t in candidates}
       selected: list[Tool] = []
       seen: set[str] = set()
       unresolved: set[str] = set()
       for raw in names:
           name = raw.strip().lower()
           tool = by_name.get(name)
           if tool is None:
               unresolved.add(name)
           elif name not in seen:
               seen.add(name)
               selected.append(tool)
       return selected, sorted(unresolved)
   ```

3. Add the `names` parameter to `tools_search`, between the `namespace` and `limit` parameters:

   ```text
        names: Annotated[
            list[str] | None,
            Field(
                min_length=1,
                max_length=_NAMES_MAX,
                description=(
                    "Exact tool names to fetch (1-10), e.g. ['runs.install']. Skips ranking and "
                    "returns those tools with their complete description and input_schema, in "
                    "the order given: it overrides 'detail' and ignores 'limit', so expect a "
                    "few KB per name. Names no visible tool carries come back in "
                    "data.unknown_names. Takes precedence over 'namespace' and 'query'."
                ),
            ),
        ] = None,
   ```

4. Amend the two descriptions `names` overrides, in the same signature. `limit`'s becomes
   `"Maximum matches to return (1-50). Not used in 'names' mode, which returns every name you "
   "list."`, and the `detail` description gains a final sentence:
   `"'names' mode always returns full detail whatever you pass here."`

5. Replace the three lines `candidates = …` / `ranked = _rank(...)` / `matches = ranked[:limit]`
   in the `tools_search` body with the mode split:

   ```text
           candidates = [t for t in all_tools if tool_visible(t.name, ctx)]
           unresolved: list[str] = []
           if names is not None:
               matches, unresolved = _select_named(candidates, names)
               truncated = False
               match_detail = SearchDetail.FULL
           else:
               ranked = _rank(candidates, query=query, namespace=namespace)
               matches = ranked[:limit]
               truncated = len(ranked) > limit
               match_detail = detail
   ```

   The preceding `ctx = current_context()` and `all_tools = list(registered_tools(app))` lines are
   unchanged.

6. Replace the existing `if not matches and query is not None:` miss-log block and the `data`
   assembly that follows it with a mode-ordered chain. `matches` is `ranked[:limit]` in every
   non-names mode and `limit` has `ge=1`, so `bool(matches)` equals the previous
   `bool(ranked)` for the `any_visible` argument. `describe_tool` is called with `match_detail`
   rather than `detail`, and `"truncated"` reads the new local:

   ```text
           data: dict[str, JsonValue] = {
               "matches": cast(
                   "JsonValue", [describe_tool(t, kinds, detail=match_detail) for t in matches]
               ),
               "truncated": truncated,
           }
           if names is not None:
               if unresolved:
                   data["unknown_names"] = cast("JsonValue", unresolved)
                   _log.info(
                       "tool_search_names_miss",
                       extra={"requested": len(names), "unresolved": len(unresolved)},
                   )
           elif namespace is not None:
               ...  # the existing five-statement namespace block, unchanged except that its
               ...  # _namespace_signal call takes any_visible=bool(matches)
           elif not matches and query is not None:
               _log.info("tool_search_miss", extra={"query": query, "count": 0})
           return ToolResponse.success("tools.search", "ok", data=data)
   ```

   The namespace block keeps its `data["namespace_status"]` assignment, its
   `NamespaceStatus.UNAUTHORIZED` grants assignment, and its `tool_search_namespace_miss` log
   exactly as they read today; only its indentation under the new `elif` and the `any_visible`
   argument change. The `kinds = resolver.registered_kinds()` block stays above this assembly.

7. Extend the `tools_search` docstring. Change the opening line to
   `"""Find tools by exact name, capability phrase, or namespace; compact summaries by default.`
   and replace the `Two modes:` list with three, `names` first:

   ```text
        Three modes, most specific first — ``names`` wins over ``namespace``, which wins over
        ``query``:
        - ``names``: fetch the tools you name, e.g. ``names=["runs.install"]`` (1-10 per call).
          No ranking; each match carries its complete description and ``input_schema`` whatever
          ``detail`` says, in the order you gave, and ``limit`` does not apply. Use this for any
          name you were handed — a ``suggested_next_actions`` entry, a name from a summary
          result, a name from the guides. Names no visible tool carries come back in
          ``data.unknown_names``; call ``tools.invoke`` on one to learn whether it is unregistered
          or outside your grants.
        - ``query``: lexical ranking over name, description, curated keywords, and bounded schema
          text (property names/descriptions, enum values, and discriminators); returns tools
          matching the query, highest-scoring first. A query that is exactly a tool name ranks
          that tool first.
        - ``namespace``: enumerate all tools in one plane (e.g. ``"debug"``); returns them
          sorted by name. Use this as a safety net when a query misses.
   ```

8. Run `uv run ruff format src/kdive/mcp/tools/gateway.py`, then `just lint`, then `just type`.
   Expect no findings and exit 0 from both. Before every `just type` run, move the two
   host-libguestfs symlinks aside (Task 4, step 1).

**Acceptance criteria.** `tools.search(names=["runs.boot"])` returns one match carrying
`input_schema`; `names=["nope.nope"]` returns `matches: []` and `unknown_names: ["nope.nope"]`;
a call passing no `names` behaves exactly as before.

## Task 2 — `reason` on a zero-match query

**Creates/modifies:** `src/kdive/mcp/tools/gateway.py`.
**Tests:** `tests/mcp/tools/test_gateway_search.py`.

**Interfaces.** Consumes Task 1's `if names is not None: … else: …` selection block and its
mode-ordered `data` chain, whose final `elif not matches and query is not None:` branch this task
replaces. Provides to Task 3 the response key name `reason`. `_rank` keeps its parameters and
changes its return type from `list[Tool]` to `tuple[list[Tool], SearchMiss | None]`; no test and
no other module calls `_rank`, so the only caller to update is `tools_search`.

**Where it fits.** The second of the issue's three parts.

### Verification

- **Contract:** `data.reason` is `"no_usable_tokens"` when no token clears the two-character
  floor.
  **Mode:** `focused-test`. Test
  `tests/mcp/tools/test_gateway_search.py::test_short_token_query_reports_no_usable_tokens`,
  querying `"a b"`. Expected red before this task: `KeyError: 'reason'` — Task 1 leaves the
  zero-match query response without the key. Green:
  `uv run python -m pytest tests/mcp/tools/test_gateway_search.py -k reason -q`.
- **Contract:** `data.reason` is `"no_token_matched"` for the issue's operator form.
  **Mode:** `focused-test`. Test `…::test_unsupported_operator_query_reports_no_token_matched`,
  querying `"select:images.describe,allocations.request"`. Expected red: `KeyError: 'reason'`.
  Green: the command above.
- **Contract:** `reason` is absent when `matches` is non-empty. **Mode:** `focused-test`. Test
  `…::test_successful_query_carries_no_reason`. Expected red: none — it passes before and after,
  guarding the additive claim. Green: the command above.
- **Contract:** `tool_search_miss` fires only for a query-mode miss and carries `reason`; a call
  passing both `namespace` and `query` no longer emits it, since `namespace` wins and no query
  ran. **Mode:** `focused-test`. Test
  `…::test_query_miss_log_carries_the_reason_and_skips_namespace_calls`, using `caplog` at `INFO`
  on `kdive.mcp.tools.gateway`. Expected red: the record exists but has no `reason` attribute, and
  fires for the namespace+query call. Green: the command above.
- **Contract:** ADR-0472 §2's exact-name ranking still holds.
  **Mode:** `focused-test`. The existing, unmodified
  `…::test_exact_tool_name_query_ranks_that_tool_first` and
  `…::test_exact_name_first_does_not_drop_the_other_hits`. Expected red: none — they must stay
  green throughout, which is the point. Green:
  `uv run python -m pytest tests/mcp/tools/test_gateway_search.py -k exact -q`.

### Steps

1. Add the miss enum beside `NamespaceStatus` in `src/kdive/mcp/tools/gateway.py`:

   ```python
   class SearchMiss(StrEnum):
       """Why a ``query`` returned no matches (ADR-0630 §4)."""

       NO_USABLE_TOKENS = "no_usable_tokens"
       NO_TOKEN_MATCHED = "no_token_matched"
   ```

2. Change `_rank`'s signature to `) -> tuple[list[Tool], SearchMiss | None]:` and make each of
   its four exits return a pair. The namespace `return sorted(...)`, the trailing
   `return sorted(candidates, ...)`, and the query branch's final `return [t for t, _ in hits]`
   each gain `, None`; `if not tokens: return []` becomes
   `if not tokens: return [], SearchMiss.NO_USABLE_TOKENS`; and a new
   `if not hits: return [], SearchMiss.NO_TOKEN_MATCHED` goes between the `hits` comprehension
   and `hits.sort(...)`. The scoring, the `exact` tie-break, and the sort key are untouched —
   ADR-0472 §2 rides on that sort key. Add one docstring paragraph: the second element is a
   `SearchMiss` only for an empty query-mode result, and says whether the query had no usable
   tokens or none of its tokens matched (ADR-0630 §4).

3. In `tools_search`, declare `miss: SearchMiss | None = None` beside Task 1's
   `unresolved: list[str] = []`, and change the `else` branch's rank call to
   `ranked, miss = _rank(candidates, query=query, namespace=namespace)`.

4. Replace Task 1's final `elif not matches and query is not None:` branch with:

   ```text
           elif miss is not None:
               data["reason"] = miss.value
               _log.info(
                   "tool_search_miss",
                   extra={"query": query, "count": 0, "reason": miss.value},
               )
   ```

5. In the `tools_search` docstring, after the `truncated: true` paragraph, add the miss contract:

   ```text
        ``query`` takes plain words, not operators: there is no ``select:`` or ``+`` syntax, and
        an unrecognised token simply matches nothing. When a query returns no matches,
        ``data.reason`` says which: ``"no_usable_tokens"`` (nothing in the query was long enough
        to search on — every word was under two characters, or the query was blank) or
        ``"no_token_matched"`` (the words ran and none of them occurs in any tool you can see —
        try fewer, plainer words, ``namespace`` mode, or ``names`` if you already have one). The
        key is absent whenever there are matches.
   ```

6. Run `uv run ruff format src/kdive/mcp/tools/gateway.py`, then `just lint` and `just type`.
   Expect exit 0 from both.

**Acceptance criteria.** `tools.search(query="select:images.describe")` returns
`data.reason == "no_token_matched"`; `tools.search(query="boot a built kernel")` carries no
`reason` key.

## Task 3 — Gateway-on instructions that carry the recipe

**Creates/modifies:** `src/kdive/mcp/schema/tool_index.py`.
**Tests:** `tests/mcp/test_tool_index.py`.

**Interfaces.** Consumes only the parameter and key names Tasks 1 and 2 establish: `names`,
`query`, `namespace`, `limit`, `detail`, and `data.reason`. Provides nothing to later tasks.
`build_instructions(gateway_enabled: bool) -> str` keeps its signature.

**Where it fits.** The third of the issue's three parts. The gateway-*off* surface is unchanged.

### Verification

- **Contract:** `build_instructions(gateway_enabled=True)` names `names=`, `limit`, `detail`,
  `namespace`, and `data.reason`.
  **Mode:** `focused-test`. Test
  `tests/mcp/test_tool_index.py::test_gateway_on_instructions_name_the_search_parameters`.
  Expected red: `AssertionError: gateway-on instructions do not name 'names='` — the current text
  names none of the five. Green: `uv run python -m pytest tests/mcp/test_tool_index.py -q`.
- **Contract:** the existing instruction invariants (#1034, #1248, #1621) still hold.
  **Mode:** `focused-test`. The existing, unmodified tests in that file, which assert
  `_GATEWAY_PRIMARY_CLAIM`, `_MISSCOPED_CLAUSE`, the namespace table of contents, and the
  agent-index resource URI. Expected red: none. Green: the command above.

### Steps

1. Replace `_GATEWAY_ON_SURFACE` in `src/kdive/mcp/schema/tool_index.py` with:

   ```python
   _GATEWAY_ON_SURFACE = """\
   This server uses a tool gateway. Only a small set of core tools are listed directly
   (tools.search and tools.invoke plus a few essentials). All other capabilities are
   discoverable via tools.search — pass a short description of what you want to do and
   it returns the best matching tool names and descriptions. Once you have a name, call
   tools.invoke(name, arguments) to execute it.
   tools.search takes plain words, not operators: query="..." to search, names=["runs.boot"]
   (1-10) to fetch named tools' full schemas with no ranking — use this for any name you were
   already handed, such as a suggested_next_actions entry — namespace="runs" to list one plane,
   limit=N (default 10, not used with names), and detail="summary" (the default) or "full" for
   the complete description and input_schema, which names always returns and which costs a few
   KB per match. A query that matches nothing returns data.reason saying why."""
   ```

   Keep the `Only a small set of core tools are listed directly` sentence verbatim: it is the
   literal `_GATEWAY_PRIMARY_CLAIM` asserted by three tests in `tests/mcp/test_tool_index.py`.

2. Add the new test to `tests/mcp/test_tool_index.py`, beside
   `test_instructions_point_at_the_agent_index`:

   ```python
   def test_gateway_on_instructions_name_the_search_parameters() -> None:
       """Gateway-on instructions carry the tools.search recipe (#2305, ADR-0630 §5).

       A gateway-mode agent has not read the tools.search docstring when it makes its first
       call, so a parameter named only there is a parameter it will not use.
       """
       from kdive.mcp.schema.tool_index import build_instructions

       text = build_instructions(gateway_enabled=True)
       for fragment in ("names=", "limit=", "detail=", "namespace=", "data.reason"):
           assert fragment in text, f"gateway-on instructions do not name {fragment!r}"
   ```

3. Run `uv run ruff format src/kdive/mcp/schema/tool_index.py tests/mcp/test_tool_index.py`, then
   `just lint`. Expect exit 0.

**Acceptance criteria.** `tests/mcp/test_tool_index.py` is green, including the pre-existing
`_GATEWAY_PRIMARY_CLAIM` and `_MISSCOPED_CLAUSE` assertions.

## Task 4 — Regenerate both artifacts and run the full gate

**Creates/modifies:** `docs/guide/reference/tools.md` and
`src/kdive/cli/commands/_generated_verbs.py`, both generated.
**Tests:** the repository guardrail suite.

**Interfaces.** Consumes the finished docstring and `Field` text from Tasks 1–3. Provides nothing.
Runs last: `docs-check` and `cli-verbs-check` each diff a committed artifact against a fresh
generation from the live registry, so both need the agent-facing text final.

### Verification

- **Contract:** the committed tool reference matches a fresh generation. **Mode:**
  `focused-test`. `just docs` then `just docs-check`; expect no diff and exit 0. Expected red
  before `just docs`: a unified diff of `docs/guide/reference/tools.md` and exit 1 with
  `tool reference is stale — run 'just docs' and commit`.
- **Contract:** the committed `kdivectl` verb descriptors match a fresh generation. **Mode:**
  `focused-test`. `just cli-verbs` then `just cli-verbs-check`; expect exit 0. Expected red before
  `just cli-verbs`: a non-zero exit naming `src/kdive/cli/commands/_generated_verbs.py` as stale,
  because the file embeds `tools.search`'s first docstring line and the `names`, `limit`, and
  `detail` help text this change rewrites.
- **Contract:** ADR-0630's status keyword is valid and consistent with its citation from `src/`.
  **Mode:** `focused-test`. `just adr-status-check`; expect
  `ADR status guard: <n> ADRs, no shipped-but-Proposed drift.` and exit 0.
- **Contract:** the whole gate. **Mode:** `focused-test`.
  `just ci > /tmp/ci-2305.log 2>&1 < /dev/null`; expect exit 0.

### Steps

1. Move the two host-libguestfs symlinks aside immediately before any type-checking run:
   `scripts/check-setup-deps.sh` re-links them during a run, which makes the *next* `just type`
   report `unused-ignore-comment` on
   `src/kdive/providers/local_libvirt/lifecycle/boot/session_mechanisms.py:564` and
   `.../retrieve/guestfs.py:121`. Neither file is in this change; do not edit them or
   `pyproject.toml`.
2. `just docs`, then `just cli-verbs`. Expect `docs/guide/reference/tools.md` to gain the `names`
   parameter and the revised `tools.search`, `limit`, and `detail` descriptions, and
   `src/kdive/cli/commands/_generated_verbs.py` to gain the matching help text and a
   `kdivectl tools search --names` append flag.
3. `just docs-check`, then `just cli-verbs-check`. Expect no diff and exit 0 from both.
4. `just lint`, then `just type`, then
   `uv run python -m pytest tests/mcp/tools/test_gateway_search.py tests/mcp/test_tool_index.py -q`.
   Expect all green.
5. Stage the change; run `just format` for Python-only paths, otherwise `prek run` and re-add
   exactly the staged paths. Commit, then `just ci > /tmp/ci-2305.log 2>&1 < /dev/null`, expecting
   exit 0; read the log on failure rather than piping the recipe.

**Acceptance criteria.** `just ci` exits 0 with the branch committed.

**Rollback.** Every change is additive and confined to the file map; reverting the branch restores
the prior behaviour with no data or schema migration.

## Self-review against the spec

- Success 1 → Task 1 verification entry 1. Success 2 → entry 2. Success 3 → entry 3. Success 4 →
  entries 4–7. Success 5 → Task 2 entries 1–3. Success 6 → Task 3 entry 1. Success 7 → Task 2
  entry 5 plus the additive-only Global Constraint. Success 8 → Task 4 entries 1 and 2. The
  spec's names-miss row → Task 1 entry 8; its `tool_search_miss` row → Task 2 entry 4. No task
  serves no requirement.
- Every name used across tasks is defined in this plan (`_NAMES_MAX`, `_select_named`,
  `SearchMiss` and its members) or confirmed at base `main` with the signature the task assumes —
  the list is in each task's Interfaces block. `Annotated[list[str] | None,
  Field(min_length=1, max_length=10)]` was checked to accept `None` and a one-element list,
  reject `[]` and an over-long list, and render `minItems` / `maxItems` into the JSON schema.
