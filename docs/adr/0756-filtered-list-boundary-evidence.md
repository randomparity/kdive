# 0756 — Named filtered lists prove denied visibility with a positive control

## Status

Accepted (2026-10-12)

## Context

Issue #3108 identifies three intentional filtering boundaries which cannot satisfy
ADR-0722's failure-envelope rule. `list_investigation_rows` selects viewer projects;
`list_redacted_system_artifacts` returns an empty page for a non-visible System.
Changing these list APIs is outside this issue. Twelve required cells are affected:
three tool/boundary pairs, two configurations and two exposures.

The current `Rejection.filtered_by` path blocks an empty response without proving
that any protected row exists or is visible to an authorized caller. Its `_filtered`
check does not require `status=ok` or inspect cursors and metadata. The artifacts
carrier's rejection snapshot covers a fresh project rather than its borrowed
session System, so it cannot prove that System's protected row stayed unchanged.

## Decision

Approved by the operator on 2026-10-12, this amends ADR-0722 decision 3 only for:

- `investigations.list` / `authorization`;
- `investigations.list` / `project-isolation`;
- `artifacts.list` / `project-isolation`.

The coverage contract retains all twelve cells, kind `rejection`, their existing
boundary/unchanged-state/cleanup assertions, and outcome `rejection`. Here rejection
means the requested visibility was denied, not that the wire envelope failed.
Name this observation in the contract and evidence artifact. No new outcome,
evidence schema, product response, generic list exception or dropped boundary.
All other boundaries retain their existing failure-envelope or authentication rule.

For each named pair, the evidence must establish this sequence:

1. Identify a real protected item independently of the list call: the investigation
   created in the cell's fresh project, or a fixed redacted console part of the
   carrier's real session System. Absence is a failed precondition, never a pass.
2. With authorized viewer grants, call the same tool and arguments in the cell's
   same exposure and observe that exact item in an `ok`, error-free page. An empty
   database, permanently empty implementation or unrelated visible row cannot pass.
3. With the existing denied grants, call the unchanged arguments and require the
   canonical empty envelope: `ok`, no category, no items or refs, no detail,
   `truncated=false`, `next_cursor=null`, and only the tool's known static actions
   and its fixed collection ID or echoed input System ID. Reject unexpected data
   keys and altered fields; an error envelope is also not this filtering proof.
4. Compare protected state before and after both calls, then prove owned setup
   cleanup. Store safe shape facts and digests rather than raw token or payload data.

For investigations, reuse the carrier's live-project snapshot and setup/close
lifecycle. For artifacts, freeze the selected console-part row plus the System's
identity/project mapping. Require both to remain present and equal; unrelated
new console parts are outside this read-only snapshot. Borrowed session state is
not deleted per cell: its existing session teardown must pass before the live run
is reported as complete, including domain/storage/capacity cleanup.

The named set has one owner in the existing coverage contract and is checked by
its harness consumer. A caller cannot enable this exception for a different tool
or boundary. Missing positive-control configuration fails closed. Replace the
obsolete `filtered_by` blocker and migrate its sole carrier; retain absent-owner
twin behavior for the unrelated ordinary rejection cells.

### Amendment (2026-10-14): five more list tools prove denied visibility (#2813)

This is an amendment because operator decision R3 (2026-10-13) widens the named set
above, and with it the ADR-0722 decision-3 exception that ADR-0722's 2026-10-12
amendment records for the original three pairs. It adds ten pairs:

- `runs.list` / `authorization` and `project-isolation`;
- `systems.list` / `authorization` and `project-isolation`;
- `jobs.list` / `authorization` and `project-isolation`;
- `allocations.list` / `authorization` and `project-isolation`;
- `debug.list_sessions` / `authorization` and `project-isolation`.

Each tool scopes its rows to the caller's viewer projects and answers a denied
caller with an empty page, never a failure: a member with no role sees no project,
and another project's admin sees none of the target project's rows. None rejects a
`project` argument that names a project the caller cannot read; it filters it.
The four-step sequence above applies unchanged; a carrier names the protected item
independently, for example the session it started for `debug.list_sessions`. Two
facts qualify step 3's canonical empty page:

- The page is per tool. Its collection ID is `runs`, `systems`, `jobs`, `allocations`
  or `debug_sessions`, with the tool's own fixed next actions and
  `data` `{truncated: false, next_cursor: null, count: 0}`.
- `allocations.list` filters its next actions by the caller's roles, and echoes a
  `project` argument in `data.project`. Its canonical page is the one the denied
  caller's grants produce, which its carrier states exactly; any other difference
  still fails.

The contract's named set grows to these thirteen pairs and remains the single owner of
the exception. A carrier adds a tool's page to the harness when it binds that tool's
cells, so a named pair without a configured page or positive control still fails
closed. No API, outcome or evidence schema changes.

## Consequences

Existing evidence remains bound to its original candidate. Rerun all twelve cells
on the changed candidate through real HTTP with real backing services, the real
issuer and proven default/recovery catalogs; direct and gateway remain distinct.
A unit stub proves the evidence predicate, not the live authorization boundary.
The read-only projection deliberately does not prove absence of every possible
write outside the protected target. Background console additions remain allowed;
selected-row deletion or mutation fails. A race moving the chosen item beyond the
control page fails the proof rather than accepting another item.

No API, database or persisted artifact migration is needed. ADR-0722's other rules
remain accepted. Downstream carrier expansion belongs to #3097/#3098; native POWER
and final release qualification remain separate obligations.

## Considered & rejected

- **Keep the failure-only rule.** Verified: the current harness raises `ScenarioStop`
  for the product's canonical empty response; the three product branches filter.
  This leaves the required cells permanently blocked without changing the API.
- **Remove the cells.** Judgment: it discards a useful role/project boundary which
  can be observed without changing product behavior.
- **Accept any empty list.** Verified from `_filtered`: empty items alone do not
  distinguish absent fixtures, a permanently empty tool, cursors or metadata leaks.
- **Change list APIs to reject.** Judgment: unnecessary public behavior change,
  expressly excluded by this issue's approved scope.
- **Add a filtered outcome or general evidence plugin system.** Judgment: existing
  boundary assertions and referenced artifacts can hold this narrow observation.
