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
- Move an ADR to **Superseded by NNNN** when a later ADR fully replaces it.
  Never edit an accepted decision in place — write a new ADR that supersedes it.
  Record the supersession in the superseded record, as a one-line banner beneath
  its existing status (form below). That banner is the only edit a merged ADR
  permits, and its link must resolve to a sibling record — the records CI gate
  checks both.
- When a later ADR supersedes only *part* of an earlier one, strike through the
  superseded prose (`~~…~~`) in the earlier ADR and add an italic
  *"Superseded by NNNN — …"* note next to it; the in-force sections stay plain.
  (ADR-0035 is the worked example.)

The supersession banner, verbatim — substitute the number and slug of the ADR that
replaces this one, and the date it was accepted:

```text
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
ls docs/adr/                                   # every ADR, numbered and titled
grep -l 'Status:\*\* Accepted' docs/adr/*.md   # by status
grep -rl 'some-topic' docs/adr/                # by topic
```
