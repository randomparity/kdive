# Aggregate quota + budget admission denials (#833)

- **Issue:** [#833](https://github.com/randomparity/kdive/issues/833) (Black-box review follow-up; the
  "single-denial collapse" ADR-0245 deferred as needs-design).
- **ADR:** [ADR-0255](../../adr/0255-aggregate-funding-gate-denials.md).
- **Status:** Accepted.
- **Builds on:** [ADR-0245](../../adr/0245-alloc-denial-remedy-breadcrumbs.md) (remedy breadcrumbs),
  #838 (budget denial echoes its figures), #841 (role-aware remedies), ADR-0069 (the
  queueable-vs-terminate routing the promotion sweep shares).

## Problem

A fresh project hits **two sequential admission brick walls** before the first allocation can
succeed. `admission_gate`
(`src/kdive/services/allocation/admission/core.py`) is a short-circuit chain — affinity →
quota → budget → host-cap → PCIe — each `if not <check>: return <denial>`:

- The first `allocations.request` fails `quota_exceeded` (no quota row → `_within_alloc_quota`
  reads `None` as `False`, ADR-0007 §4).
- After `accounting.set_quota`, the next identical request fails `allocation_denied /
  budget_exceeded` (no budget row → `within_budget` reads `None` as `False`).
- Only the third attempt (after `accounting.set_budget`) succeeds.

ADR-0245 + #838 + #841 made each single denial *actionable* (it names the remedy tool and, for
budget, the shortfall figures). But the caller still learns of the **second** unmet gate only
*after* fixing the first: the quota denial says nothing about budget. Two avoidable
round-trips remain.

## Goal

When the **synchronous** `allocations.request` is denied on a project-funding gate, surface
**every** unmet funding gate (quota *and* budget) in one denial, each with its current and
required values and its remedy tool, so the caller provisions both at once.

## Scope

Project-**funding** gates only: the per-project concurrency **quota** and the spend
**budget**. These are onboarding gates — a fresh project must provision both before any
request can succeed, and neither is resolved by waiting.

Host-cap and PCIe denials stay the existing **runtime** short-circuit (queueable capacity
denials, not onboarding); they are unchanged.

### In scope

- `src/kdive/services/allocation/admission/core.py`
  - Add `quota_status(conn, project) -> tuple[int | None, int]` — `(limit_or_None,
    occupying_count)`; `None` limit means no quota row (fail-closed).
  - Add `funding_unmet(conn, project, estimate) -> list[dict]` — the unmet funding gates
    (quota first, then budget), each a transport-neutral entry (gate discriminator +
    counters, **no** MCP tool name).
  - On the **synchronous** denial path only, enrich a funding denial's `details` with
    `details["unmet"]`. The promotion sweep's gate replay is untouched.
  - Remove `_budget_denial_details` (the #838 flat `estimate_kcu` / `limit_kcu` /
    `spent_kcu` / `budget_remaining_kcu` keys); the budget figures now ride the uniform
    `unmet` budget entry. (Replace, don't deprecate.)
- `src/kdive/mcp/tools/lifecycle/allocations/request.py`
  - Map each `unmet` entry's gate → its remedy tool (`accounting.set_quota` /
    `accounting.set_budget`) and surface it in `data["unmet"]`.
  - Lead `suggested_next_actions` with **every** unmet remedy (quota then budget) for an
    admin caller; a non-admin keeps `["allocations.list"]` (cannot call the admin tools,
    #841) — the prose still names what an admin must run.
  - Compose `detail` from the `unmet` entries (enumerate both shortfalls + remedies),
    role-aware.

### Out of scope (explicit)

- **The promotion-sweep routing** (`services/allocation/promotion.py`,
  `_is_budget_terminate`). Its terminate-vs-wait decision keys off the gate's single-category
  short-circuit denial (budget → terminate; quota / host-cap → wait). Aggregation is a
  synchronous-presentation concern; the gate's routing semantics are preserved bit-for-bit so
  promotion is unchanged. The sweep does not read `details["unmet"]`.
- **The queue path.** `on_capacity="queue"` enqueues a queueable (quota) denial rather than
  returning it; an enqueued request gets a queued allocation, not an onboarding diagnostic.
  Aggregation applies only to denials actually returned to the synchronous caller.
- Auto-seeding quota/budget rows; any RBAC, accounting-tool, schema, or migration change. No
  migration is needed — this is read-path only.

## Behaviour

Let `Q` = quota gate unmet, `B` = budget gate unmet (independent of the gate's short-circuit
order). The synchronous denial:

| state | top-level `error_category` / `reason` | `data["unmet"]` | admin `suggested_next_actions` |
|-------|----------------------------------------|-----------------|--------------------------------|
| Q only | `quota_exceeded` | `[quota]` | `[set_quota, allocations.list]` |
| B only | `allocation_denied` / `budget_exceeded` | `[budget]` | `[set_budget, allocations.list]` |
| Q and B | `quota_exceeded` (the gate's short-circuit primary) | `[quota, budget]` | `[set_quota, set_budget, allocations.list]` |
| host-cap | `allocation_denied` / `at_capacity` | absent | `[allocations.list]` |
| affinity | `allocation_denied` / `affinity_denied` | absent | `[allocations.list]` |

The top-level `error_category` stays the gate's primary (onboarding order: quota before
budget); the complete picture is in `data["unmet"]` + `suggested_next_actions` + `detail`. No
new error category is introduced.

### `unmet` entry shape

- quota: `{"gate": "quota", "current": <int>, "required": <int>, "limit": <int>?, "remedy":
  "accounting.set_quota"}` — `current` = occupying allocations; `required` = `current + 1`;
  `limit` present only when a quota row exists.
- budget: `{"gate": "budget", "required_kcu": <str>, "limit_kcu": <str>?, "spent_kcu":
  <str>?, "remaining_kcu": <str>?, "remedy": "accounting.set_budget"}` — `required_kcu` = the
  priced estimate; the limit/spent/remaining figures present only when a budget row exists
  (mirrors #838's omit-on-absent-row rule).

kcu figures are stringified `Decimal` (no float precision loss, matching `accounting.estimate`
and #838); allocation counts are JSON integers. `gate` is transport-neutral; `remedy` is
injected by the MCP layer that owns the tool names (ADR-0245).

A fresh project (no rows) → `[{gate:quota, current:0, required:1, remedy:…}, {gate:budget,
required_kcu:"…", remedy:…}]` — exactly the issue's example.

## Success criteria (falsifiable)

1. A both-unmet synchronous request returns one denial whose `data["unmet"]` lists **both**
   gates with their figures; `error_category == "quota_exceeded"`; admin
   `suggested_next_actions == ["accounting.set_quota", "accounting.set_budget",
   "allocations.list"]`.
2. A quota-only-unmet request lists only the quota gate; a budget-only-unmet request lists
   only the budget gate.
3. Host-cap, affinity, and PCIe denials carry **no** `unmet` and keep their existing envelope.
4. The promotion sweep still terminates a budget-recheck failure and waits on a quota/host-cap
   failure (unchanged): the gate's category/reason/queueable for each single-gate denial is
   byte-for-byte what it was, and no denial-path write occurs on any failing check
   (all-or-nothing, ADR-0023).
5. A non-admin both-unmet caller gets `["allocations.list"]` only, with prose naming both
   shortfalls and telling them to ask a project admin.
6. No new migration; `just ci` green.

## Edge cases

- No quota row **and** no budget row (fresh project): both entries, neither with limit
  figures.
- Quota row present at cap, budget row present and short: both entries **with** limit figures.
- Estimate pricing failure stays a `configuration_error` *before* the gate — no `unmet`
  (not a funding denial).
- `on_capacity="queue"` with both unmet: enqueues (quota is queueable); no `unmet` surfaced —
  consistent with the queue UX, the budget recheck terminates at promotion as today.
