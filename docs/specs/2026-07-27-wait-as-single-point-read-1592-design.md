# Design — `wait` as the single point-read and polling contract (#1592)

- **Issue:** #1592 (epic #1576)
- **ADR:** [ADR-0468](../adr/0468-wait-as-the-single-point-read.md)
- **Migration:** none

## Requirement

Remove `jobs.get` and `allocations.get`. `jobs.wait` and `allocations.wait` become the single
read/poll contract for their object, with `timeout_s=0` as the point read. Registry: 125 → 123.

## Why this is safe

`wait_job` (`src/kdive/mcp/tools/jobs.py`) and `wait_allocation`
(`src/kdive/mcp/tools/lifecycle/allocations/view.py`) both compute the deadline *before* the
loop and test `now >= deadline` *after* the first read, so `timeout_s <= 0` is one query and a
return of the same envelope the getter built. The getters add nothing else: same exposure
scope, same annotations, same no-leak `not_found`-before-role ordering, same renderer.

## Blast radius

`visible_next_actions()` raises `ValueError` on an unregistered breadcrumb, so **every**
`suggested_next_actions` entry naming a removed tool must move in the same commit.

### Registration and exposure

| file | change |
| --- | --- |
| `src/kdive/mcp/exposure.py` | drop the `jobs.get` and `allocations.get` `_TOOL_SCOPES` rows |
| `src/kdive/mcp/tools/jobs.py` | delete the `jobs.get` `@app.tool` block and the now-dead `get_job` handler |
| `src/kdive/mcp/tools/lifecycle/allocations/registrar.py` | delete the `allocations.get` block and its handler import |
| `src/kdive/mcp/tools/lifecycle/allocations/view.py` | delete the now-dead `get_allocation` handler |

`CORE_TOOLS` is untouched (`allocations.wait` was already a member; neither removed name was).

### Breadcrumbs that feed `visible_next_actions`

| file | change |
| --- | --- |
| `src/kdive/mcp/responses.py` | `_NEXT_ACTIONS[SUCCEEDED]` and `[FAILED]` → `[]` |
| `src/kdive/mcp/tools/jobs.py` (`jobs.list` collection) | drop `jobs.get` |
| `src/kdive/mcp/tools/ops/images/build_publish.py` | drop `jobs.get`, keep `jobs.wait` |
| `src/kdive/mcp/tools/lifecycle/systems/view.py` (`FAILED_SYSTEM_NEXT_ACTIONS`) | `jobs.get` → `jobs.wait` |
| `src/kdive/mcp/tools/lifecycle/allocations/common.py` (`allocation_next_actions`) | `allocations.get` → `allocations.wait` |
| `src/kdive/mcp/tools/lifecycle/allocations/view.py` (`_ALLOCATIONS_COLLECTION_ACTIONS`) | `allocations.get` → `allocations.wait` |
| `src/kdive/mcp/tools/lifecycle/allocations/lifecycle.py` (two `stale_handle` paths) | `allocations.get` → `allocations.wait` |
| `src/kdive/mcp/tools/ops/security/breakglass.py` | `allocations.get` → `allocations.wait` |

`systems/view.py`, `common.py`, `view.py`, `lifecycle.py` and `breakglass.py` keep a breadcrumb
because there the caller genuinely does not hold the object it is being pointed at (a
`failing_job_id` it has not read; a lease whose real state contradicts the `stale_handle` it
just got). `responses.py` and `build_publish.py` drop theirs because the caller was handed that
exact envelope.

### Vocabulary (`src/kdive/mcp/schema/tool_index.py`)

- `TOOL_KEYWORDS["jobs.get"]` is deleted and its words merged into `TOOL_KEYWORDS["jobs.wait"]`.
- `TOOL_KEYWORDS["allocations.wait"]` gains `get`, `status`, `fetch`, `lookup`, `id`.
- `RETIRED_TOOL_NAMES` gains `jobs.get → jobs.wait` and `allocations.get → allocations.wait`.
  No change to `_invert_retired_names` / `retired_names_for` / the gateway `_score`.

### Curated CLI verbs

`add_subparsers()` only emits a curated `Verb` at a path a generated verb already occupies, so
`Verb("jobs", "get", …)` and `Verb("allocations", "get", …)` become unreachable. Both, plus
`reads.jobs_get` / `reads.allocations_get`, are deleted. The generated
`kdivectl jobs wait --job-id <id> --timeout-s 0` serves the point read (a generated verb takes
its tool parameters as required flags, not positionals).

### Agent-facing prose

Living docs only — served `src/kdive/mcp/resources/_content/*.md` and their canonical
`docs/guide/` sources, plus handler/wrapper docstrings that name a tool an agent may call
(`prompts/registrar.py`, `resources/registrar.py`, `vmcore/registrar.py`,
`jobs/handlers/artifacts/vmcore.py`, `jobs/handlers/connectivity/ssh_authorize.py`,
`services/allocation/release.py`, `scripts/live-debug.py`). Historical ADRs, dated
`docs/design/` records, `docs/archive/`, `CHANGELOG.md` and applied SQL migrations are records
of what was true then and are not rewritten.

## Tests

- `jobs.wait(timeout_s=0)` on a terminal job returns byte-identical output to the removed
  `jobs.get`, and issues exactly one DB read (counted, so a regression to a polling loop is
  caught).
- The same for `allocations.wait(timeout_s=0)` on a settled allocation, and on a `requested`
  one (returns immediately with `queue_position`, does not block).
- Terminal `SUCCEEDED`/`FAILED` envelopes carry no `jobs.get`, and a `capture_vmcore` job still
  carries its kind-specific `artifacts.get` steer.
- Both retired names resolve through search to their replacement, and the intent phrases the
  removed tools served ("get job status", "look up an allocation by id") find the survivor.
- Existing `get_job` / `get_allocation` handler tests migrate onto `wait_*(…, timeout_s=0)`,
  preserving the not-found / ungranted / bad-uuid / role-denied coverage.
- Completeness guards: `tests/mcp/core/test_app.py`, `tests/mcp/core/test_tool_docs.py`
  (`_BEHAVIOR_TESTS_BY_TOOL`), `tests/mcp/test_tool_index.py`.

## Regeneration order

`just doc-constants` → `config-docs` → `docs` → `rbac-matrix` → `cli-verbs` → `resources-docs`,
then `just ci` bare.
