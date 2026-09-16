# Pre-gateway tool-catalog assertion survey (#2521)

- **Surveyed base commit:** `64b41e6724c93cb851cc5e2546b9fc31a53da82a` (`origin/main`,
  "fix: keep the proof-record doc-path citation on one line")
- **Surveyed on:** 2026-09-15
- **Current contract:** `CORE_TOOLS` (`src/kdive/mcp/exposure.py:264`), nine names, applied to
  an agent-profile caller's `tools/list` by `src/kdive/mcp/middleware/exposure.py:114-115` when
  `KDIVE_MCP_TOOL_GATEWAY` is on. `62fb4fa4d` made it on by default (ADR-0268, ADR-0456).
- **Deliverable of:** #2521 (survey-only). Sizes #2522 (`live_stack`) and #2523 (`agent_smoke`).

This is a point-in-time record. Every file:line below is read at the base commit above; a later
change to a surveyed file may invalidate a row, and the base commit is what makes that
detectable. `scripts/check-doc-paths.sh` excludes `docs/design/**`, so no guardrail re-checks
these citations.

## Headline counts

| Tier | `fix-needed` | `fix-coupled` | `keep` | Entries | Files carrying an entry |
|---|---|---|---|---|---|
| `live_stack` | 0 | 0 | 2 | 2 | 2 |
| `agent_smoke` | 3 | 3 | 1 | 7 | 4 |
| **Total** | **3** | **3** | **3** | **9** | **6** |

**The `live_stack` tier carries no fix-needed hit.** #2522's own file scope
(`tests/integration/live_stack/*.py`, `test_live_stack.py`, `test_remote_live_stack.py`,
`test_console_parts_live.py`) contains exactly one assertion touching `tools/list`, and it is a
`keep`. #2522 should be sized as no-work-found, not as a fix.

All three `fix-needed` hits and all three `fix-coupled` hits are in `agent_smoke`, and all six
belong to #2523.

## In-flight movers of surveyed files

A change merged after the base commit can invalidate a row. Known at survey time:

- **PR #2530** (open, issue #2500) changes `tests/integration/live_stack/spine.py` — cited by
  entry L2.
- **#2511** (open) extracts a shared support module out of `tests/integration/live_stack/`,
  moving the files entries L1 and L2 cite. #2522 is already sequenced after it.

Neither touches a `fix-needed` entry, so neither blocks #2523.

## Surveyed set

Surveyed set = the files the two markers select, **plus** every test and support module under
the paths #2521 names. Neither source alone reaches both: the markers do not select the
`tests/integration/live_stack/` support package or `tests/smoke/agent_smoke/`'s walker modules,
and #2521's file list does not name six of the nine marker-selected tier files.

`marker` is the tier the file actually collects under; `default` means it is **not** tiered out
and runs inside `just test` and the PR gate (`justfile:100`).

| File | Marker | Named by | Hits |
|---|---|---|---|
| `tests/compose/test_compose_volume_persistence_live.py` | `live_stack` | marker | 0 |
| `tests/compose/test_compose_worker_lifecycle_live.py` | `live_stack` | marker | 0 |
| `tests/integration/live_stack/__init__.py` | default | issue | 0 |
| `tests/integration/live_stack/conftest.py` | default | issue | 0 |
| `tests/integration/live_stack/measurement.py` | default | issue | 0 |
| `tests/integration/live_stack/skew.py` | default | issue | 0 |
| `tests/integration/live_stack/spine.py` | default | issue | 1 (L2, second sweep) |
| `tests/integration/live_stack/test_client_inmemory.py` | default | issue | 1 (L1) |
| `tests/integration/live_stack/test_expected_accel.py` | default | issue | 0 |
| `tests/integration/live_stack/test_harness_tool_error.py` | default | issue | 0 |
| `tests/integration/live_stack/test_harness_unit.py` | default | issue | 0 |
| `tests/integration/live_stack/test_measurement_readout.py` | default | issue | 0 |
| `tests/integration/live_stack/test_require_guest_arch.py` | default | issue | 0 |
| `tests/integration/live_stack/test_require_issuer.py` | default | issue | 0 |
| `tests/integration/live_stack/test_skew.py` | default | issue | 0 |
| `tests/integration/live_stack/test_spine.py` | default | issue | 0 |
| `tests/integration/test_console_parts_live.py` | **`live_vm`** | issue | 0 |
| `tests/integration/test_finalization_measurement.py` | `live_stack` | marker | 0 |
| `tests/integration/test_kdivectl_boundary.py` | `live_stack` | marker | 0 |
| `tests/integration/test_kdivectl_generated_lifecycle.py` | `live_stack` | marker | 0 |
| `tests/integration/test_live_stack.py` | `live_stack` | issue + marker | 0 |
| `tests/integration/test_remote_live_stack.py` | `live_stack` | issue + marker | 0 |
| `tests/integration/test_wire_harness.py` | `live_stack` | marker | excluded (2) |
| `tests/smoke/agent_smoke/__init__.py` | default | issue | 0 |
| `tests/smoke/agent_smoke/surface.py` | default | issue | 1 (A1) |
| `tests/smoke/agent_smoke/test_agent_golden_path.py` | `agent_smoke` | issue + marker | 1 (A4) |
| `tests/smoke/agent_smoke/test_walker.py` | default | issue | 2 (A5, A6) |
| `tests/smoke/agent_smoke/walker.py` | default | issue | 3 (A2, A3, A7) |

28 files, 8,667 lines. `tests/integration/test_wire_harness.py` reads `excluded (2)`, never `0`:
its two tests were re-aimed at the gateway contract by PR #2491 and #2521 excludes them, so it
is not an examined-clean file.

### Scope corrections

1. **`tests/integration/test_console_parts_live.py` is `live_vm`, not `live_stack`**
   (`pytestmark = pytest.mark.live_vm`, line 61). #2521 and #2522 both list it under the
   `live_stack` file scope. It carries no hit either way, so nothing is owed — but #2522's
   scope line is wrong as written.
2. **`tests/integration/live_stack/` carries no tier marker.** It is the support package plus
   unmarked unit tests that run in the default suite and the PR gate. `-m live_stack` selects
   nothing from it.
3. **Three of the four files holding `agent_smoke` hits are default-tier**
   (`surface.py`, `walker.py`, `test_walker.py`). Only `test_agent_golden_path.py` is tiered
   out. #2523 already names all four in its file scope, so ownership is correct — but its
   edits land in PR-gated tests, not only in a non-gated smoke tier. Size and risk-label it
   accordingly.

## Reproducing the counts

From a checkout at the base commit:

```sh
# Tier membership (8 files / 28 tests, and 1 file / 1 test)
uv run python -m pytest -m live_stack  --collect-only -q --strict-markers
uv run python -m pytest -m agent_smoke --collect-only -q --strict-markers

# Catalog-shaped code across the surveyed set (26 lines, in 4 files outside test_wire_harness.py)
rg -n --sort path \
  'list_tools|tools/list|tool_names|CORE_TOOLS|GATEWAY_TOOLS|WIND_DOWN_TOOLS|_HEALTHY_TOOLS|tools\.(search|invoke)' \
  tests/compose/test_compose_volume_persistence_live.py \
  tests/compose/test_compose_worker_lifecycle_live.py \
  tests/integration/live_stack/ \
  tests/integration/test_console_parts_live.py \
  tests/integration/test_finalization_measurement.py \
  tests/integration/test_kdivectl_boundary.py \
  tests/integration/test_kdivectl_generated_lifecycle.py \
  tests/integration/test_live_stack.py \
  tests/integration/test_remote_live_stack.py \
  tests/smoke/agent_smoke/

# The direct-call class L2 dispositions (3 sites); the sweep above does not match it,
# because a direct call names no catalog symbol
rg -n 'call_tool\(' tests/integration/live_stack/spine.py

# The nine-name core set, and the registry it is clipped from
uv run python -c "from kdive.mcp.exposure import CORE_TOOLS, CLASSIFIED_TOOLS, PUBLIC_TOOLS; \
print(len(CORE_TOOLS), len(CLASSIFIED_TOOLS | PUBLIC_TOOLS))"   # -> 9 126
```

Disposition rule, applied to every entry below: evaluate the assertion's expected catalog
against `CORE_TOOLS` and the agent-profile clip at the base commit. `fix-needed` = wrong against
that contract today. `fix-coupled` = correct only because a named `fix-needed` entry is wrong;
it changes with that entry and is not edited independently. `keep` = correct as written.

---

## `live_stack` tier — 0 fix-needed, 0 fix-coupled, 2 keep

Owner for any work found here: **#2522**.

### L1 — `keep`

- **File:line:** `tests/integration/live_stack/test_client_inmemory.py:82`
- **Assertion:** `assert {"scalar.one", "list.many"} <= set(names)`
- **Disposition:** `keep`. `names` comes from `LiveStackClient.list_tools()` against the
  module-local `_probe_app()` fixture (lines 18-29), whose only tools are `scalar.one` and
  `list.many`. That app installs no `ToolExposureMiddleware`, so `CORE_TOOLS` and the gateway
  default do not reach it; the file's own docstring scopes it to "the envelope-parsing seam and
  the `.data` shape pin". The assertion pins client plumbing, not the served catalog.
- **Owner:** none — no work.

### L2 — `keep`

- **File:line:** `tests/integration/live_stack/spine.py:172`, `:194`, `:315`, and the
  `call_tool` sites they serve in `tests/integration/test_live_stack.py` and
  `tests/integration/test_remote_live_stack.py`
- **Assertion class:** direct `await client.call_tool("<name>", ...)` on tools outside
  `CORE_TOOLS` — `jobs.wait`, `systems.get`, `systems.provision`'s siblings, and the rest of
  the spine's lifecycle calls.
- **Disposition:** `keep`. A direct call is not a `tools/list` assertion, and ADR-0268
  Consequences accepts that the server does not refuse calls to unadvertised tools — that is an
  explicit non-goal of #2521, #2522 and #2523. The clip changes what is advertised, not what is
  callable, so the spine is unaffected.
- **Owner:** none — no work. Recorded because it is the tier's largest catalog-adjacent
  surface; a reader who does not find it dispositioned here will re-derive it.

---

## `agent_smoke` tier — 3 fix-needed, 3 fix-coupled, 1 keep

Owner for every entry here: **#2523**.

### A1 — `fix-needed` (root)

- **File:line:** `tests/smoke/agent_smoke/surface.py:24-25`
- **Assertion:**
  ```python
  async def tool_names(self) -> frozenset[str]:
      return frozenset(tool.name for tool in await self._app.list_tools())
  ```
- **Disposition:** `fix-needed`. `FastMCP.list_tools()` runs middleware by default (fastmcp
  3.4.4), so `ToolExposureMiddleware.on_list_tools` is entered — but this call carries no
  verified token, `request_context()` raises `AuthError`, and the middleware returns the
  unfiltered catalog (`src/kdive/mcp/middleware/exposure.py:116-118`). Measured at the base
  commit: this surface serves **124** tools against a registry union of **126** and a
  `CORE_TOOLS` of **9**. The tier therefore walks the pre-`62fb4fa4d` flat catalog and has
  never exercised the surface an agent actually receives.
- **Why it is the root:** A2 and A3 are only reachable as defects once the surface is the agent
  surface. Fixing A2 and A3 against the flat catalog fixes nothing. The fix is to drive the
  surface with an agent-profile token so the gateway clip applies — a larger change than
  re-writing an assertion, and the main reason #2523 is not a one-line edit.
- **Owner:** #2523.

### A2 — `fix-needed`

- **File:line:** `tests/smoke/agent_smoke/walker.py:38-42` (the list) and `:142-144` (the check)
- **Assertion:**
  ```python
  WIND_DOWN_TOOLS: tuple[str, ...] = (
      "systems.teardown",
      "allocations.release",
      "investigations.close",
  )
  ...
  missing_wind_down = [t for t in WIND_DOWN_TOOLS if t not in tools]
  if missing_wind_down:
      stalls.append(Stall("wind-down", f"cannot reach wind-down tools: {missing_wind_down}"))
  ```
- **Disposition:** `fix-needed`. A fixed tool-name list required to be present in `tools/list`
  — the exact shape #2513 names. None of the three is in `CORE_TOOLS`, so an agent-profile
  catalog stalls here unconditionally. Under the clip, all three are reported missing.
- **Owner:** #2523.

### A3 — `fix-needed`

- **File:line:** `tests/smoke/agent_smoke/walker.py:90-101` (`_stage_tools`) and `:136-139`
  (the stage check)
- **Assertion:** `_stage_tools` keeps only backticked tokens that are `in tools`, then
  `:139` records a stall for any golden-path stage whose surviving list is empty.
- **Disposition:** `fix-needed`. This is a `tools/list` shape assumption: it presumes the served
  catalog contains the tools the agent-index names stage by stage. Measured against a
  `CORE_TOOLS`-clipped catalog, **5 of the agent index's 10 stages** name no live tool and stall
  — stages 5, 6, 8, 9 and 10. Clipped, the walk records 6 stalls in total (those 5 plus A2's).
- **Fix locus — size this before starting.** Two loci are viable and #2523's file scope reaches
  only one. (a) In-tier: re-aim `_stage_tools` at the gateway contract, so a stage is satisfied
  by reaching its tools through `tools.invoke` rather than by their presence in `tools/list`.
  (b) Out-of-tier: change the served golden path itself — the index is
  `docs/guide/agent-index.md` (`src/kdive/mcp/resources/registrar.py:166`), a doc resource
  outside #2523's declared `tests/smoke/agent_smoke/*.py` scope. This survey does not choose
  between them; (b) would need #2523's scope widened or a separate issue.
- **Owner:** #2523.

### A4 — `fix-coupled` → A1

- **File:line:** `tests/smoke/agent_smoke/test_agent_golden_path.py:42`
- **Assertion:** `assert result.ok, "agent-smoke stalls:\n" + ...`
- **Disposition:** `fix-coupled` with A1. The assertion names no tool and is correct as written;
  it is green today only because A1 feeds it the flat catalog. It is re-confirmed when A1
  changes, not edited on its own. The sibling assertion at `:46` checks stage names, not tools,
  and is unaffected.
- **Owner:** #2523.

### A5 — `fix-coupled` → A2

- **File:line:** `tests/smoke/agent_smoke/test_walker.py:45-54` (`_HEALTHY_TOOLS`) and
  `:37-38` (the wind-down stage of `_HEALTHY_INDEX`)
- **Assertion:** the synthetic "healthy" surface is defined as one whose tool set contains
  `*GATEWAY_TOOLS, *WIND_DOWN_TOOLS` and whose index names the three wind-down tools as a stage.
- **Disposition:** `fix-coupled` with A2. This is a fixture, not a server-catalog assertion, but
  it encodes the same premise: a healthy surface lists the wind-down tools directly. When A2's
  contract changes, this fixture's definition of healthy changes with it.
- **Owner:** #2523.

### A6 — `fix-coupled` → A2

- **File:line:** `tests/smoke/agent_smoke/test_walker.py:140-144`
  (`test_missing_wind_down_tool_stalls_at_wind_down`)
- **Assertion:** discards `WIND_DOWN_TOOLS[-1]` from the fake surface and asserts a `wind-down`
  stall fires.
- **Disposition:** `fix-coupled` with A2. It guards A2's check directly; it is correct for the
  check as it stands and must move with it.
- **Owner:** #2523.

### A7 — `keep`

- **File:line:** `tests/smoke/agent_smoke/walker.py:31` (`GATEWAY_TOOLS`) and `:147-149`
  (the gateway check)
- **Assertion:** `GATEWAY_TOOLS = ("tools.search", "tools.invoke")`, then a stall if either is
  absent from `tools`.
- **Disposition:** `keep`. Both names are in `CORE_TOOLS`, so this check is correct under the
  current contract and is the one part of the walk that an agent-profile catalog passes. It is
  also the check that expresses the gateway contract rather than contradicting it — the pattern
  A2 and A3 should be re-aimed toward.
- **Owner:** none — no work.
