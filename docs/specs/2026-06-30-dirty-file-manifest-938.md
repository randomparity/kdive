# Spec: warm-tree dirty-file manifest + discoverable provenance (#938)

- Issue: #938
- ADR: [ADR-0282](../adr/0282-warm-tree-dirty-file-manifest.md)
- Status: Draft
- Date: 2026-06-30

## Problem

`runs.get` `data.build_provenance` on a warm-tree (`source='server'`, `LOCAL` build host)
build reports `dirty: true` and an optional `tree_sha` content digest, but does **not** name
which files made the tree dirty, nor whether the dirtiness was tracked edits, untracked
files, or both. An agent whose value proposition is "I verified the fix" can prove
reproducibility (compare `tree_sha` across runs) but cannot tell, from the surface alone,
*what* was built without comparing whole trees out-of-band. From the black-box review (§6,
LOW): *"`dirty: true` is useful but insufficient for audit-quality reports."*

A second defect (issue addendum): the provenance fields are not **discoverable** from the
`runs.get` surface. The `runs.get` wrapper docstring documents only `data.steps` and
`data.required_cmdline` — it never mentions `data.build_provenance`. The field documentation
lives on a *different* tool (`runs.create`), which an agent calling `runs.get` would never
read. Per the project contract (AGENTS.md), the agent acts only on the wrapper docstring +
`Field` text it can see, so an undocumented field is an undiscoverable field.

## Current behavior (verified)

- `_with_warm_tree_provenance` (`src/kdive/providers/shared/build_host/dispatch.py:147`)
  records `{label, resolved_commit?, dirty?, tree_sha?}` via best-effort git probes in
  `src/kdive/build_artifacts/provenance.py`.
- `dirty` = `git status --porcelain` non-empty (tracked **or** untracked); `tree_sha` =
  content-deterministic `^{tree}` of tracked working-tree state, present only when dirty
  with tracked changes (ADR-0265).
- The value is persisted as JSON in `run_steps(step='build').result`, coerced on read by
  `_optional_provenance_map` (`src/kdive/services/runs/steps.py:44`) which admits only
  `str | bool` values, and surfaced verbatim by `runs.get`
  (`src/kdive/mcp/tools/lifecycle/runs/common.py:218`).
- `runs.get`'s wrapper docstring (`registrar.py:223`) omits `build_provenance` entirely.

## Goals

1. Extend the warm-tree provenance manifest, for a **dirty git** staged tree, with:
   - `dirty_files: list[str]` — the tracked paths that differ from `resolved_commit`
     (`git diff --name-only -z HEAD`), bounded; `dirty_files_truncated: true` when the
     list was capped.
   - `untracked: bool` — whether non-ignored untracked files were present in the staged
     tree (`git ls-files --others --exclude-standard`), making the previously-implicit
     untracked signal explicit.
2. Make `data.build_provenance` discoverable from `runs.get`: document its shape (all
   fields, including the new ones) directly on the `runs.get` wrapper docstring, and extend
   the `runs.create` enumeration and the `build-source-staging.md` warm-tree prose.

## Non-goals

- No patch-diff artifact. The issue lists it as *optional*; capturing, storing, redacting,
  and size-bounding a diff artifact is a larger, separable change. The file list + `untracked`
  flag + `tree_sha` meet the audit need ("know what was built"). Deferred (see ADR rejected
  alternatives); a follow-up issue may add it if agents need the byte-level diff.
- No untracked-file digest or untracked file **list**. Capturing untracked content was
  rejected in ADR-0265 for cost; `untracked: bool` only flags presence, consistent with
  `tree_sha` covering tracked content only.
- No change to git/remote-clone lanes — they already name exactly what was built
  (`{remote, ref, resolved_commit, build_host}`).
- No schema, migration, RBAC, or config change.

## Design

### Field semantics (warm-tree git staged tree)

The manifest is assembled in dependency order; each probe is best-effort and a failure omits
only its own key (never fails the build), exactly as ADR-0265 established:

| field | when present | source |
|-------|-------------|--------|
| `label` | always | `kernel_source_ref` (decorative) |
| `resolved_commit` | git tree with a HEAD | `git rev-parse HEAD` |
| `dirty` | `resolved_commit` resolved | `git status --porcelain` non-empty |
| `untracked` | `dirty` is **true** | `git ls-files --others --exclude-standard` non-empty |
| `tree_sha` | `dirty` true + tracked changes captured | `git stash create` → `^{tree}` |
| `dirty_files` | `dirty` true + ≥1 tracked change | `git diff --name-only -z HEAD` (capped) |
| `dirty_files_truncated` | `dirty_files` was capped | derived |

`untracked` is emitted **only when `dirty` is true**: when `dirty` is false the staged tree
has no untracked non-ignored files by definition (untracked files make `status --porcelain`
non-empty), so the flag would be a constant `false`. This keeps the clean-tree manifest
unchanged at `{label, resolved_commit, dirty: false}` (preserving the ADR-0265 contract).

`dirty_files` lists **tracked** paths only (the same scope as `tree_sha`); untracked files
are signalled by `untracked: true` but not listed (consistent with the ADR-0265 cost trade).
`dirty_files` non-empty therefore co-occurs with `tree_sha` (both derive from tracked
changes), but the two probes are independent and best-effort: a probe failure can leave one
present and the other absent.

### Bounding

`dirty_files` is capped at `DIRTY_FILES_MANIFEST_MAX = 100` tracked paths to bound the
`runs.get` envelope size (the same posture as the console manifest, ARTIFACT_GET window, and
search-text caps). When the tracked-change count exceeds the cap, the manifest carries the
first `MAX` paths (git's sorted order) and `dirty_files_truncated: true`. A truncated list is
a summary; an agent needing exact identity compares `tree_sha`, which is unaffected by the
cap. No integer total is emitted (see ADR rejected alternatives — it would reopen ADR-0265's
deliberate exclusion of non-`str`/`bool` provenance values).

### Type widening

The provenance value type widens from `dict[str, str | bool]` to
`dict[str, str | bool | list[str]]` along the whole path: `BuildOutput.build_provenance`,
the warm-tree dispatch helper, `BuildStepResult.build_provenance` and its `_optional_provenance_map`
coercion, `_finalize_external_build`'s `source_provenance` parameter,
`external_source_provenance`'s return, and the `runs.get` surfacing helpers. `JsonValue`
already admits a list of strings, so the wire envelope is unchanged.

Dict types are invariant in Python's type system, so every **producer** of a provenance map
must be widened to the new union, not only the consumers — otherwise `ty` rejects assigning a
`dict[str, str | bool]` to a `dict[str, str | bool | list[str]]` field. `external_source_provenance`
never emits a list but its return type is widened for assignability.

`_optional_provenance_map` admits a value that is a `str`, a `bool`, or a `list` whose every
element is a `str`; any other value type (a `dict`, a number, a mixed list) makes the whole
map degrade to `None`, the same defensive failure posture as today.

### Discoverability

- `runs.get` wrapper docstring: add a paragraph naming `data.build_provenance` and its shape,
  including `dirty_files` / `untracked` and the "tracked git state only / `resolved_commit`
  decorative when dirty" caveats.
- `runs.create` `build_profile` description: extend the existing warm-tree enumeration to
  list `dirty_files` and `untracked`.
- `docs/operating/build-source-staging.md`: extend the warm-tree provenance section.
- Regenerate the committed tool reference (`just docs` → `docs/guide/reference/runs.md`).

## Acceptance criteria

1. A warm-tree build of a git tree with tracked edits records `dirty: true`, `untracked`
   (bool), `dirty_files` (the tracked paths), and `tree_sha`.
2. A warm-tree build dirty with untracked files only records `dirty: true`, `untracked: true`,
   and **no** `dirty_files` / `tree_sha`.
3. A clean warm-tree build records `{label, resolved_commit, dirty: false}` — unchanged.
4. A non-git warm tree degrades to `{label}` — unchanged.
5. `dirty_files` with more than `DIRTY_FILES_MANIFEST_MAX` tracked changes is capped and
   carries `dirty_files_truncated: true`.
6. The widened value round-trips through persistence: a persisted `dirty_files` list reaches
   `runs.get` `data.build_provenance` as a real JSON array; a malformed provenance degrades
   to `None`.
7. The `runs.get` wrapper docstring documents `data.build_provenance` (asserted by a doc test).
8. `just ci` is green; the committed tool reference is regenerated.
