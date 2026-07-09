# ADR-0265: report warm-tree build dirtiness and a content digest (#861)

> **Superseded by [ADR-0316](0316-remove-server-build-lane.md)** (2026-07-08) — warm-tree
> server builds were removed; kdive builds only from uploaded artifacts. The decision below is
> retained as history.

- Status: Superseded by [ADR-0316](0316-remove-server-build-lane.md)
- Date: 2026-06-27

## Context

A `source='server'` build on a `LOCAL` build host uses the **warm-tree** lane: the
worker mirrors the operator-staged `KDIVE_KERNEL_SRC` directory into the build workspace
with `rsync -a --delete` (`workspaces/workspace.py`, `sync_tree`). rsync copies the
**working tree as-is**, including uncommitted edits and untracked files. Provenance,
however, is captured only as a git ref: `_with_warm_tree_provenance`
(`providers/shared/build_host/dispatch.py`) records `{label, resolved_commit?}`, where
`resolved_commit` is `git -C $KDIVE_KERNEL_SRC rev-parse HEAD` (`build_artifacts/provenance.py`,
`rev_parse_head`).

When the staged tree is dirty, `resolved_commit` does not describe what was compiled — it
is the HEAD the working tree is *based on*, not the content built. An agent whose value
proposition is "I verified the fix" cannot tell from `runs.get` whether the edit it made
is the edit that was tested. This was the highest-leverage finding in the black-box review
(§1, 🔴).

The git/remote lanes are unaffected: a git build clones a pinned remote ref, so its
`{remote, ref, resolved_commit, build_host}` provenance already names exactly what was
built. Only the warm-tree lane mirrors mutable working-tree state.

The provenance value is persisted as JSON in `run_steps(step='build').result` and surfaced
verbatim by `runs.get` (`mcp/tools/lifecycle/runs/common.py`). No column or schema pins
its shape, so extending it needs no migration. The whole pipeline is typed `dict[str, str]`
today; ADR-0263 (merged same day) retired the stringified-flag convention and added an AST
guard (`tests/mcp/test_no_stringified_flags.py`) that fails on a `"true"`/`"false"` flag in
`mcp/tools/`, so a new boolean flag must be a native JSON `bool`, not a string.

## Decision

Extend warm-tree provenance for a **git** staged tree from `{label, resolved_commit?}` to
`{label, resolved_commit, dirty, tree_sha?}`:

- **`dirty: bool`** — `git -C $KDIVE_KERNEL_SRC status --porcelain` is non-empty. Counts
  both tracked modifications and untracked files (rsync mirrors both), so `dirty=true` means
  "the staged tree differs from `resolved_commit`." Emitted as a native JSON boolean
  (ADR-0263). Present only when `resolved_commit` was resolved (i.e. the staged tree is a git
  work tree with a HEAD); a non-git warm tree has no HEAD to be dirty against and stays
  `{label}`.
- **`tree_sha: str`** (optional) — a content-deterministic git **tree** object SHA of the
  tracked working-tree state, captured read-only via `git stash create` resolved to its
  `^{tree}`. It uniquely identifies a dirty build's tracked content: two builds with
  identical tracked content produce the same `tree_sha` regardless of HEAD or wall-clock.
  Present only when `dirty` is true **and** the capture yields a sha (there are tracked
  modifications). It is **not** present for a clean tree (`resolved_commit`'s own tree
  already identifies it) and does **not** include untracked files (see Consequences).

Capture stays best-effort and never fails the build: each probe (`rev_parse_head`,
`working_tree_dirty`, `staged_tree_sha`) returns `None` on any git/OS failure and that key
is simply omitted. The probes run in order so a cheaper failure short-circuits the rest:
no `resolved_commit` ⇒ no `dirty`/`tree_sha`; `dirty=false` ⇒ no `tree_sha`.

Widen the provenance value type from `dict[str, str]` to `dict[str, str | bool]` along the
whole path — `BuildOutput.build_provenance`, the warm-tree/git dispatch helpers,
`BuildStepResult.build_provenance` and its persistence coercion (a new coercion that admits
`str | bool` values instead of `_optional_str_map`'s str-only filter), and the `runs.get`
surfacing helper. `JsonValue` already admits `bool`, so the wire envelope is unchanged.

Document, in the warm-tree section of the build-source-staging guide (removed by ADR-0316) and the
`runs.create` `build_profile` description, that the warm-tree lane builds **working-tree
state, not HEAD**, that `resolved_commit` is the HEAD the working tree is based on
(decorative when `dirty`), and what `dirty`/`tree_sha` mean. Regenerate the committed tool
reference (`just docs`).

No schema, migration, RBAC, or config change.

## Consequences

- `runs.get` `data.build_provenance` on a warm-tree git build now carries `dirty` (always)
  and `tree_sha` (when dirty with tracked changes). An agent reads `data["build_provenance"]
  ["dirty"]` as a native boolean and, on a dirty build, compares `tree_sha` across runs to
  confirm two builds compiled the same tracked source — without trusting the decorative
  `resolved_commit`.
- **Untracked-only dirtiness is flagged but not digested.** `git status --porcelain` reports
  untracked files (`??`), so an untracked-only change yields `dirty=true`; but `git stash
  create` captures only tracked content, so `tree_sha` is absent in that case. This is the
  deliberate cost/honesty trade (see rejected alternatives): the common agent edit is to a
  tracked source file, `dirty` still tells the agent the build is not `resolved_commit`, and
  capturing untracked content would require walking and hashing the whole tree.
- **`dirty`/`tree_sha` cover git-tracked state only; gitignored content is invisible.** The
  warm-tree rsync (`rsync -a --delete`, no excludes) mirrors the whole directory, but
  `git status --porcelain` and `git stash create` ignore gitignored paths. So a staged tree
  diverging from `resolved_commit` only in gitignored files — a modified `.config`, or stale
  `.o` objects an incremental `make` could reuse — reports `dirty=false`. `dirty=false` means
  "no **tracked** changes," not "byte-identical to a clean `resolved_commit` checkout." Using
  `--ignored` was rejected (any tree carrying build output would read `dirty=true` always); the
  full mirrored-content digest was rejected for cost (below). The limit is documented in the
  spec and the warm-tree prose so an agent does not over-trust `dirty=false`.
- **Provenance is probed at build-completion from the live staged tree**, not from the rsync
  snapshot taken at build start — the same timing `resolved_commit` already has. For the
  single-actor agent flow (edit → build, nothing else touches the tree) this equals what was
  built; a concurrent mutation of `$KDIVE_KERNEL_SRC` during a build would make provenance
  describe the post-build tree. Capturing at sync time is out of scope here.
- The provenance value type widens to `dict[str, str | bool]`; a persisted provenance with a
  malformed (non-`str`/`bool`) value degrades to `None`, same failure posture as the prior
  str-only coercion.
- Two extra short-lived `git` subprocesses run per warm-tree build (`status --porcelain`,
  and `stash create` only when dirty). `git status` is the same order of cost as the existing
  `rev-parse`; both are bounded by `DEFAULT_GIT_READ_TIMEOUT`. Negligible against a kernel
  build. `git stash create` is read-only — it writes loose objects but never mutates the
  index, working tree, or stash ref/reflog.
- No in-repo consumer reads `build_provenance` as a fixed `{label, resolved_commit?}` shape;
  tests asserting the exact dict are updated alongside.

## Considered & rejected

- **Hash the full staged workspace content (the literal rsync bytes).** Most faithful to
  "what was compiled," but a buildable kernel tree is hundreds of MB to GBs across tens of
  thousands of files; hashing it on every build is the cost the issue explicitly flags.
  Rejected for cost.
- **Capture untracked files too via a temporary index** (`GIT_INDEX_FILE=tmp git add -A &&
  write-tree`). Includes untracked content in the digest, but a cold temp index must stat the
  entire tree (no stat cache), making it markedly costlier than `git stash create`, which
  diffs only tracked paths against HEAD. The untracked-only edit is an uncommon agent path
  and `dirty` already flags it. Rejected for cost/complexity; the gap is documented.
- **Store the `git stash create` commit SHA directly.** The stash commit embeds the committer
  date, so identical content built twice yields different SHAs — useless for the
  "same source ⇒ same digest" comparison `tree_sha` exists for. Resolving to `^{tree}` gives a
  content-deterministic identifier. Rejected.
- **Emit `dirty` as the string `"true"`/`"false"`** to keep the `dict[str, str]` type. Violates
  the ADR-0263 native-scalar convention and trips its AST guard. Rejected; widen the type
  instead.
- **Report `dirty` for a non-git warm tree.** A non-git staged tree has no HEAD to diff
  against; `dirty` would be meaningless. Provenance stays `{label}`, unchanged. Rejected.
- **Rsync `--checksum` / a build-time content manifest.** Changes the build (slower rsync) or
  adds a new artifact for a provenance-only need. Rejected; the digest is captured from the
  source tree, not the build.
