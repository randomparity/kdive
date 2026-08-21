# Architecture Decision Records

This directory records the load-bearing architecture decisions for the KDIVE
production rewrite. The top-level design (`../specs/top-level-design.md`) lists
nine core decisions and states that each "should become an ADR before
implementation"; those ADRs live here.

## Process

- One decision per file, named `NNNN-kebab-title.md` with a zero-padded,
  monotonic number (`0001`, `0002`, …). Numbers are never reused.
- Copy `0000-template.md` to start a new ADR.
- Open it as **Proposed**. An ADR moves to **Accepted** when the pull request
  that implements its decision merges — the implementing PR *is* the
  ratification. (A directional ADR with no single implementing PR is Accepted
  once the architecture it governs has landed.) Update the ADR's `Status` in that
  same PR, so status never drifts from reality. An ADR that ships only partially
  stays **Proposed** until its decision is fully realized.
- When a decision's implementation is staged across several PRs, cite the
  tracking issue number(s) in `src/` and `tests/` for the intermediate PRs
  instead of the ADR number — the `adr-status-check` guard rejects a
  **Proposed** ADR cited from either tree, since a citation there asserts the
  decision is implemented. The PR that flips `Status` to **Accepted** adds the
  ADR's citations across `src/` and `tests/` in that same change.
- Move an ADR to **Superseded by NNNN** when a later ADR fully replaces it.
  Do not rewrite the accepted decision — write a new ADR that supersedes it.
  Record the supersession in the superseded record as a one-line banner beneath
  its existing status, and set the status itself to name the superseding ADR in
  the same change (both forms below). The records CI gate checks the banner's
  form, its date, and that each link on a single line — the banner's and the
  status line's own — resolves to a sibling record, in the `## Status` section
  or in the preamble for a pre-0504 record that keeps its status as a bullet
  there ([ADR-0564](0564-the-status-region-is-where-a-record-keeps-its-status.md));
  a status bullet whose link wraps onto its own line is still kept in step by hand.
  A record's status value is the one part of it the gate does not hold
  immutable, in either shape, and a record may carry only one such line.
- A merged ADR is append-only outside `## Status`. When later evidence or a
  follow-up decision qualifies its reasoning, append a block to the level-2
  section it qualifies with the heading
  `### Amendment (YYYY-MM-DD): <claim> (#NNNN)`. Start the block by explaining
  why the addition is an amendment and state which earlier claim it qualifies.
  An amendment preserves the original record; it does not replace the need for a
  new ADR and supersession banner when a later decision fully replaces the
  accepted one.
- When a later ADR supersedes only *part* of an earlier one, append an amendment
  to the affected section that links to the later ADR and identifies the
  superseded claim. Do not strike through or otherwise rewrite the earlier prose:
  removing or changing a line reports `E-REWRITE` for records held to the current
  gate.

The supersession banner, verbatim — substitute the number and slug of the ADR that
replaces this one, and the date it was accepted:

```text
> **Superseded by [NNNN](NNNN-slug.md)** (YYYY-MM-DD)
```

A record from 0504 on keeps its status in a `## Status` section, and the banner
goes beneath the status line there. A pre-0504 record keeps its status as a
preamble bullet instead, above the first heading; set that bullet too:

```text
- **Status:** Superseded by [ADR-NNNN](NNNN-slug.md)
> **Superseded by [NNNN](NNNN-slug.md)** (YYYY-MM-DD)
```

## Status lifecycle

```
Proposed → Accepted → Superseded by NNNN
                   ↘ Rejected
```

## Style

The project doc-style guard applies here too: use **Milestone**, not "Sprint",
and keep prose plain and factual (no "critical", "robust", "comprehensive").
## Index

There is deliberately **no index table here — the directory listing is the index.**
`NNNN-kebab-title.md` already carries an ADR's number and title, and its status lives
in the record itself, so a table of those columns is derived data that has to be kept
in sync by hand.

It was removed because the sync never held: a table with one row per ADR conflicts on
every parallel PR that appends a row (git conflicts on *adjacent insertions*, so rows
0499 and 0501 collide even though the numbers are disjoint), the one-sentence limit
stated here was ignored in practice for most of the table's life, and every column was
recoverable from the filename or the record. See [ADR-0504](0504-no-hand-maintained-adr-index.md).

So an ADR-producing change touches **exactly one file: its own record** — plus, when it
supersedes an earlier decision, the one-line banner in that record's status (below).

Browse `docs/adr/` directly, or:

```sh
ls docs/adr/                                          # every ADR, numbered and titled
rg -Ul -e '^-?\s*\*{0,2}Status:?\*{0,2}\s*Accepted' \
      -e '## Status\n\nAccepted' docs/adr/*.md        # by status
grep -rl 'some-topic' docs/adr/                       # by topic
```

The status search matches both shapes the corpus carries: the pre-0504 inline
`- **Status:** Accepted` bullet (grandfathered, see
[docs/debt/0001-legacy-adr-shape-is-grandfathered.md](../debt/0001-legacy-adr-shape-is-grandfathered.md))
and the `## Status` section the records gate requires from 0504 on. A plain `grep -l` on the
bullet form alone misses every record in the newer shape without any signal that the result
is incomplete.
