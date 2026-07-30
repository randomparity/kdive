# 0504 — No hand-maintained ADR index; the records gate replaces the status guard

## Status

Accepted (2026-07-29)

## Context

`docs/adr/README.md` carried a table with one row per ADR — 483 rows by the time of this
decision — and `scripts/check_adr_status.py` enforced it from inside `just ci` (`justfile:520`,
recipe at `:350`). That guard checked three things: every ADR file has a row, every row has an
ADR file, and the row's `Status` keyword matches the record's.

The table cost more than it returned.

**It serialized parallel work.** Git conflicts on *adjacent insertions*, not on duplicate
content, so two PRs appending rows for disjoint numbers still collide — rows 0499 and 0501 land
inside each other's three-line diff context. Up-front ADR-number assignment does not help; it
solves filename collisions and leaves the table untouched. On the campaign that produced
0499–0503, two of four ADR-carrying PRs conflicted on that one line.

**The enforcement made the split-ownership workaround unreachable.** The convention this
project's tooling ships (`/preflight`, `/campaign`, `/design`) tells a dispatched agent to write
only its ADR file and report `index row pending`, leaving the row to the orchestrator. With
`adr-status-check` in `just ci` that is a guaranteed-red PR, so it can never be merged for the
orchestrator to fix. Worse, the deferred variant — merge the ADRs, append the rows afterwards —
would leave **`main` itself red** for the window between the two, because the guard fails on a
file with no row. Four independent agents hit this wall on one campaign and each resolved it the
same way: by adding its own row, i.e. every agent deviated from the instruction identically.

**The stated limit was not observed.** The section required "a single concise sentence — two to
three lines at most". Measured on the rows added most recently: 1501, 1681, 2853 and 2928
characters — multi-sentence paragraphs, roughly ten times the budget, matching their neighbours
rather than the rule. A convention that every author ignores is not a convention.

**And every column was derived.** Number and title come from `NNNN-kebab-title.md`; status comes
from the record's own status line. Nothing read the table except the guard that required it.

Meanwhile the guard did **not** check the things worth checking. Nothing in this repo failed a
change that deleted an ADR, moved it into a subdirectory, replaced it with a symlink, or gutted
its body in place while leaving the file and its row present. That last one is the cheapest
erasure and the one every path-based rule misses. In a repo where `/review-loop` runs unattended
inside `/work-issue` and `/campaign`, the agent that wrote a record is the agent that would
benefit from erasing it, and no human sees the intermediate state.

## Decision

Do not maintain an index of ADRs. The files in `docs/adr/` are the index. An ADR-producing
change touches exactly one file: its own record.

Record supersession in the superseded record, as a one-line banner beneath its existing status,
rather than in a shared table:

```text
> **Superseded by [NNNN](NNNN-slug.md)** (YYYY-MM-DD)
```

That banner is the only edit a merged ADR permits. It is necessary because an immutable record cannot
otherwise point at what replaced it, and a reviewer needs that signal to tell a governing
decision from a retired one.

Replace `scripts/check_adr_status.py` with the `decision-records` gate, adopted from the
`decision-records` skill as five files this repository now owns outright:
`.github/scripts/check-records.sh`, `.github/scripts/check-records-test.sh`,
`.github/scripts/profiles/{adr,debt}.sh`, and `.github/workflows/records.yml`. The old script and
its `just adr-status-check` recipe are deleted rather than left alongside.

Enable both record kinds, `RECORD_PROFILES: adr debt`, and create `docs/debt/` with a real first
record. Deferral records give `/review-loop`'s `deferred-tracked` disposition a durable owner
that lands in the diff, which a tracker issue never achieves.

## Consequences

- An ADR's status is read from the record rather than from a summary that can drift from it.
- GitHub renders `docs/adr/` as a file listing rather than a titled table. Filenames carry number
  and title so the listing stays browsable; status requires opening a record, or a `grep`.
- The gate fails a change that deletes, moves, symlinks, un-tracks or **guts in place** a record,
  and fails a malformed record, an unreadable status, a banner naming nothing or dated in the
  future, an H1 whose number disagrees with its filename, and a duplicate number the change
  introduced. It also fails if one of its own five files is deleted, symlinked away, or renamed
  without declaring the rename in `GATE_PREDECESSORS`.
- **The 483 pre-existing ADRs do not match the shape the gate checks** and are grandfathered to
  `W-LEGACY-SHAPE` warnings, because conformance is computed from the base ref. New ADRs are
  checked at full severity, so 0504 onward must use the shape *this* record uses: a `## Status`
  section reading `Accepted (YYYY-MM-DD)`, an H1 beginning `# NNNN `, and non-empty `## Context`,
  `## Decision`, `## Consequences`, `## Considered & rejected`. Copy `0000-template.md`, not a
  neighbouring ADR. Tracked as [debt 0001](../debt/0001-legacy-adr-shape-is-grandfathered.md).
- The gate is **advisory until the `records` job is a required status check** in branch
  protection: a PR may edit the checker, and a PR that deletes the workflow stops the job rather
  than failing it. The checker detects deletion of its own files; nothing detects an edit.
- `records.yml` runs on `pull_request` only, and needs `fetch-depth: 0` plus the base SHA to read
  the record set as it stood at the PR's base. It is separate from `ci.yml`, so ADR shape no
  longer gates the `lint · type · test` job.
- `profile_check_directory` reports `W-INDEX-TABLE` if `docs/adr/README.md` ever regains rows
  numbered like records, so the table cannot come back unnoticed.
- This repository owns its copy of the five files. They will not track fixes made in the skill.

## Considered & rejected

- **Keep the table, keep the guard, and fix only the campaign tooling.** The narrowest change:
  teach the orchestrator to let each agent own its row. It removes the red-PR wall but keeps the
  quadratic conflict, keeps 483 rows of derived data, and still buys none of the anti-erasure
  protection. It treats the symptom.
- **Generate the table from the ADR files.** Two committed regenerations conflict exactly as two
  hand-edits do, so it removes the conflict only if the artifact is uncommitted or
  auto-resolved. It adds a script to produce data already on disk.
- **`.gitattributes merge=union` on the README.** Auto-resolves the row conflict but applies to
  the whole file, so a prose edit beside a row edit duplicates prose. It also silently keeps both
  rows when two PRs claim one number, converting a loud conflict into a quiet defect.
- **Have the merging actor append rows at merge time.** Structurally conflict-proof, but it
  forces a push and a fresh CI cycle on PRs that were already green and needed no rebase, and it
  reds `main` between the ADR merge and the row.
- **Migrate all 483 records into the new shape in this change.** Rejected as the highest-risk
  possible first use of an anti-erasure gate: reshaping every record is the exact operation the
  gate exists to catch, performed 483 times, before the gate has once run green over the
  untouched corpus. Deferred to [debt 0001](../debt/0001-legacy-adr-shape-is-grandfathered.md)
  with the migration path recorded there.
- **Adopt the gate but enable only the `adr` profile.** Leaves `/review-loop`'s
  `deferred-tracked` disposition with no durable owner, and both profile files must be carried
  anyway — the gate's self-protection derives from the `profiles/` listing, not from which names
  are enabled — so the saving is one directory and one record.
