# Warm-tree build provenance: dirty flag + content digest (#861)

- Issue: #861
- ADR: [ADR-0265](../adr/0265-warm-tree-dirty-provenance.md)
- Status: Accepted

## Problem

A warm-tree build (`KDIVE_KERNEL_SRC` lane) mirrors the worker's **working tree** into the
build with `rsync -a --delete` — including uncommitted edits and untracked files — but
`runs.get` reports provenance only as `resolved_commit` (`git rev-parse HEAD`). When the
staged tree is dirty, `resolved_commit` does not describe what was compiled, so an agent
cannot tell whether the change it made is the change that was tested.

Scope is the **warm-tree (LOCAL, non-git) lane only**. The git/remote lanes clone a pinned
ref and already report `{remote, ref, resolved_commit, build_host}`.

## Requirements

1. A warm-tree build over a **git** staged tree reports `build_provenance.dirty: true|false`,
   true iff `git status --porcelain` on `$KDIVE_KERNEL_SRC` is non-empty (tracked
   modifications or untracked files).
2. When `dirty` is true and there are tracked modifications, provenance carries
   `tree_sha`: a content-deterministic git tree-object SHA of the tracked working-tree state,
   so a dirty build is uniquely identifiable independent of the decorative `resolved_commit`.
3. `dirty` is a native JSON boolean in `runs.get` `data.build_provenance` (ADR-0263), not a
   string.
4. A **non-git** warm tree (no HEAD) keeps `{label}` — no `dirty`, no `tree_sha`,
   no `resolved_commit`.
5. Provenance capture remains best-effort: any git/OS failure omits the affected key and
   never fails the build.
6. Docs state the warm-tree lane builds working-tree state (not HEAD) and define
   `resolved_commit`/`dirty`/`tree_sha`.

## Provenance shapes (after this change)

| Staged tree | `build_provenance` |
|-------------|--------------------|
| git, clean | `{label, resolved_commit, dirty: false}` |
| git, dirty (tracked changes) | `{label, resolved_commit, dirty: true, tree_sha}` |
| git, dirty (untracked only) | `{label, resolved_commit, dirty: true}` |
| non-git | `{label}` |
| git probe failed (`rev-parse` errored) | `{label}` |

`tree_sha` is the `^{tree}` of `git stash create` — the tracked working-tree content. It does
**not** include untracked files; `dirty` still flags them (the untracked-only row above).

## Success criteria (falsifiable)

- A staged git tree with an uncommitted edit to a tracked file yields
  `dirty=true` and a `tree_sha` that differs from the `resolved_commit`'s own tree and is
  stable across two builds of identical content.
- The same tree committed (clean) yields `dirty=false` and no `tree_sha`.
- A staged git tree with only an untracked new file yields `dirty=true` and no `tree_sha`.
- A non-git staged tree yields `{label}` only.
- `git` absent / a corrupt repo / a timeout yields `{label}` (or `{label, resolved_commit}`
  if only the later probes failed) — never an exception out of the build.
- `runs.get` surfaces `dirty` as a JSON boolean (`isinstance(..., bool)`), and the
  `test_no_stringified_flags` AST guard stays green.

## Edges & failure modes

- `rev-parse HEAD` fails (non-git, unborn HEAD, git missing) → omit `resolved_commit`,
  `dirty`, `tree_sha`.
- `status --porcelain` fails after `rev-parse` succeeded → omit `dirty` and `tree_sha`
  (keep `resolved_commit`).
- `stash create` returns empty (no tracked changes) or fails → omit `tree_sha`.
- Persisted provenance with a non-`str`/`bool` value → coerces to `None` (degraded), same as
  today's str-only coercion.

## Out of scope

- Hashing untracked or the full rsync workspace content (see ADR rejected alternatives).
- Any change to the git/remote-lane provenance shape.
- Schema, migration, RBAC, config, or `outputSchema` changes.

## Affected code

- `src/kdive/build_artifacts/provenance.py` — add `working_tree_dirty`, `staged_tree_sha`.
- `src/kdive/providers/shared/build_host/dispatch.py` — `_with_warm_tree_provenance` records
  `dirty`/`tree_sha`.
- `src/kdive/build_artifacts/results.py` — `BuildOutput.build_provenance` type + docstring.
- `src/kdive/services/runs/steps.py` — `BuildStepResult.build_provenance` type + `str|bool`
  persistence coercion.
- `src/kdive/mcp/tools/lifecycle/runs/common.py` — provenance surfacing type.
- `src/kdive/mcp/tools/lifecycle/runs/registrar.py` — `build_profile` description.
- `docs/operating/build-source-staging.md` — warm-tree provenance prose.
- Regenerate `docs/guide/reference/runs.md` via `just docs`.
