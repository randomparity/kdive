# ADR-0282: warm-tree dirty-file manifest and discoverable build provenance (#938)

> **Superseded by [ADR-0316](0316-remove-server-build-lane.md)** (2026-07-08) — warm-tree
> server builds and their build provenance were removed; kdive builds only from uploaded
> artifacts. The decision below is retained as history.

- Status: Superseded by [ADR-0316](0316-remove-server-build-lane.md)
- Date: 2026-06-30
- Extends: [ADR-0265](0265-warm-tree-dirty-provenance.md)

## Context

ADR-0265 added `dirty` and a content-deterministic `tree_sha` to warm-tree build provenance
(`source='server'` on a `LOCAL` build host, where the worker rsyncs the operator-staged
`KDIVE_KERNEL_SRC` **working tree**, uncommitted edits and untracked files included). An agent
can now prove two builds compiled identical tracked content by comparing `tree_sha`. But the
black-box review (§6, LOW) found `dirty: true` alone insufficient for audit-quality reports:
it does not name **which** files made the tree dirty, and untracked-only dirtiness is signalled
only implicitly (absence of `tree_sha`). The agent cannot tell *what* was built without
comparing whole trees out-of-band.

A second, related defect: the provenance fields are not **discoverable**. Per the project
contract, FastMCP serializes only the `@app.tool` wrapper docstring + `Field` text into the
agent-visible schema. `runs.get`'s wrapper docstring documents `data.steps` and
`data.required_cmdline` but never mentions `data.build_provenance`; the field documentation
lives only on `runs.create`, a different tool an agent calling `runs.get` would not read. The
field exists and is correct but is invisible at the call site that returns it.

The provenance value is persisted as JSON in `run_steps(step='build').result` (no column pins
its shape, so extending it needs no migration) and coerced on read by `_optional_provenance_map`,
which today admits only `str | bool` values (ADR-0265 deliberately excluded `int` so a stray
numeric value stays rejected; ADR-0263 requires native JSON booleans, not stringified flags).

## Decision

### 1. Extend the warm-tree manifest for a dirty git staged tree

Add two fields, assembled in the existing best-effort probe order (a probe failure omits only
its own key and never fails the build):

- **`untracked: bool`** — `git ls-files --others --exclude-standard` is non-empty (non-ignored
  untracked files present). Emitted **only when `dirty` is true**: when `dirty` is false the
  staged tree has no untracked non-ignored files by definition (they would make
  `status --porcelain` non-empty), so the clean-tree manifest stays `{label, resolved_commit,
  dirty: false}`, unchanged from ADR-0265. Makes the previously-implicit untracked signal
  explicit: `untracked` distinguishes tracked-edit dirtiness from untracked-file dirtiness.
- **`dirty_files: list[str]`** — the tracked paths that differ from `resolved_commit`, from
  `git diff --name-only -z HEAD` (NUL-separated, so paths with unusual characters need no
  quote parsing). Present only when `dirty` is true **and** at least one tracked path changed.
  Bounded at `DIRTY_FILES_MANIFEST_MAX = 100`; when the tracked-change count exceeds the cap,
  the first `MAX` paths (git's sorted order) are kept and **`dirty_files_truncated: true`** is
  added. `dirty_files` lists tracked content only — the same scope as `tree_sha`; untracked
  files are flagged by `untracked`, not listed.

`dirty_files` non-empty co-occurs with `tree_sha` (both derive from tracked changes), but the
probes are independent and best-effort, so a failure can leave one present and the other absent.

### 2. Widen the provenance value type to admit a string list

Widen `dict[str, str | bool]` to `dict[str, str | bool | list[str]]` along the whole path:
`BuildOutput.build_provenance`, the warm-tree dispatch helper, `BuildStepResult.build_provenance`
and `_optional_provenance_map`, `_finalize_external_build`'s `source_provenance` parameter,
`external_source_provenance`'s return, and the `runs.get` surfacing helpers. Python dict types
are invariant, so every producer is widened, not only consumers (`external_source_provenance`
never emits a list but its return type widens for assignability). `_optional_provenance_map`
admits a value that is a `str`, a `bool`, or a `list` whose elements are all `str`; any other
value degrades the whole map to `None` (unchanged failure posture). `JsonValue` already admits
a string list, so the wire envelope is unchanged. No `int` is admitted — the cap is signalled
by the `dirty_files_truncated` boolean, not a count (keeping ADR-0265's numeric exclusion).

### 3. Make `data.build_provenance` discoverable from `runs.get`

Document the `data.build_provenance` shape — all fields including `dirty_files` / `untracked`,
the "tracked git state only" scope, and "`resolved_commit` decorative when dirty" — directly on
the `runs.get` wrapper docstring. Extend the `runs.create` `build_profile` enumeration and the
build-source-staging guide's warm-tree section (removed by ADR-0316) with the new fields. Regenerate the
committed tool reference (`just docs`).

No schema, migration, RBAC, or config change.

## Consequences

- `runs.get` `data.build_provenance` on a dirty warm-tree git build now names the changed
  tracked files (`dirty_files`) and whether untracked files were present (`untracked`). An
  agent reads the manifest and knows what was built without comparing whole trees out-of-band.
- An agent reading `runs.get` can now **discover** `data.build_provenance` and every field from
  the tool's own description, not only from `runs.create`.
- **Untracked content is flagged, never listed or digested.** `untracked: true` says "some
  untracked non-ignored file was in the staged tree"; the file names and content are not
  captured (ADR-0265 cost trade). `tree_sha` and `dirty_files` cover tracked state only.
- **`dirty_files` paths are not redacted.** They come from `git diff` of the operator-controlled
  `KDIVE_KERNEL_SRC` tree — the same trust posture as the existing unredacted `label` /
  `resolved_commit`, not the guest/console/gdb output the redaction invariant governs. Surfaced
  verbatim; the `runs.get` read is already project-scoped.
- **`dirty: true` with `dirty_files` absent is valid.** `dirty` (`git status --porcelain`) and
  `dirty_files` (`git diff --name-only HEAD`) are different probes: absence means untracked-only
  dirtiness or a tracked change `git diff HEAD` does not name (e.g. a file-mode-only change), not
  a contract violation.
- **`dirty_files` is git-tracked, gitignored-blind, and bounded.** Like `dirty`/`tree_sha`,
  it ignores gitignored paths (a modified `.config` or stale `.o` is invisible). A list longer
  than `DIRTY_FILES_MANIFEST_MAX` is truncated with `dirty_files_truncated: true`; the agent
  uses `tree_sha` for exact identity when truncated.
- Up to two extra short-lived `git` subprocesses per **dirty** warm-tree build
  (`ls-files --others`, `diff --name-only`), both bounded by `DEFAULT_GIT_READ_TIMEOUT` and
  negligible against a kernel build. Both are read-only.
- The provenance value type widens to admit a string list; a persisted provenance with a
  malformed value (a number, a dict, a mixed list) degrades to `None`, same as before.
- Probes run at build-completion from the live staged tree (the existing `resolved_commit`
  timing). For the single-actor agent flow this equals what was built; a concurrent mutation of
  `$KDIVE_KERNEL_SRC` mid-build would make provenance describe the post-build tree (unchanged
  from ADR-0265).
- Tests asserting the exact warm-tree provenance dict are updated alongside.

## Considered & rejected

- **A patch-diff artifact** (`git diff HEAD` stored as an object-store artifact, referenced from
  the manifest). The issue lists it as optional. It needs redaction of diff content, size
  bounding, and a new artifact lifecycle — a separable, larger change. The file list +
  `untracked` + `tree_sha` meet the stated audit need. Deferred to a follow-up if agents need
  the byte-level diff.
- **Listing or digesting untracked files.** Rejected in ADR-0265 for cost (a cold temp index
  must stat the whole tree); unchanged here. `untracked: bool` flags presence only.
- **An integer `dirty_files_total` count alongside the truncation flag.** Would reopen
  ADR-0265's deliberate exclusion of non-`str`/`bool` provenance values (admitting `int` lets a
  stray numeric value through the coercion guard). The `dirty_files_truncated` boolean signals
  the list is a sample; `tree_sha` gives exact identity. Rejected; no `int` in the map.
- **Parsing `git status --porcelain` for the file list.** Its `XY <path>` lines mix tracked and
  untracked entries and quote unusual paths, needing status-code and quote parsing. `git diff
  --name-only -z HEAD` (tracked vs HEAD) and `git ls-files --others --exclude-standard -z`
  (untracked) each give a clean NUL-separated path list with no parsing. Rejected.
- **Emitting `untracked` on a clean tree (always `false`).** Redundant noise — a clean tree
  cannot have untracked non-ignored files. Omitted when `dirty` is false, keeping the
  ADR-0265 clean-tree manifest byte-identical. Rejected.
- **Documenting `build_provenance` only on `runs.create` (status quo).** An agent calling
  `runs.get` reads only `runs.get`'s description, so the field stays undiscoverable. Rejected;
  documented on `runs.get` directly.
