# Plan: warm-tree dirty provenance (#861)

Derived from [the spec](../../specs/2026-06-27-warm-tree-dirty-provenance-861.md) and
[ADR-0265](../../adr/0265-warm-tree-dirty-provenance.md). TDD throughout: failing test first,
minimal implementation, then guardrails (`just lint`, `just type`, focused `pytest`).

The type widening (`dict[str, str]` → `dict[str, str | bool]`) crosses several files in
lockstep, so this is implemented directly in one session, not fanned out to independent
subagents. Steps are ordered so the tree stays green at each commit.

## Step 1 — git probes (`build_artifacts/provenance.py`)

Add two best-effort probes next to `rev_parse_head`, same failure posture (return `None` on
any `OSError`/`SubprocessError`/non-zero exit; `# noqa: S404` already on the module):

- `working_tree_dirty(tree, *, timeout=DEFAULT_GIT_READ_TIMEOUT) -> bool | None`
  — `git -C <tree> status --porcelain`; `None` on failure/empty-tree-path, else
  `bool(stdout.strip())` (non-empty ⇒ dirty, includes untracked `??`).
- `staged_tree_sha(tree, *, timeout=...) -> str | None`
  — `git -C <tree> stash create`; if stdout is empty (clean / nothing to stash) return `None`;
  else resolve to the content-deterministic tree object via
  `git -C <tree> rev-parse <commit>^{tree}` and return it, `None` on any failure.

Tests (`tests/.../test_build_provenance.py` or a probe-focused test): real `git init` tmp
tree — clean → dirty `False`, sha `None`; tracked edit → dirty `True`, sha non-`None` and
`== rev-parse HEAD^{tree}` only after the edit differs; untracked-only → dirty `True`,
sha `None`; non-git path → both `None`; empty string → both `None`.

Acceptance: probes never raise; tree_sha is a tree object SHA (verify it resolves via
`git cat-file -t`), stable for identical content.

## Step 2 — record dirty/tree_sha (`providers/shared/build_host/dispatch.py`)

In `_with_warm_tree_provenance`, after `resolved_commit` is added (git tree confirmed):

```
provenance: dict[str, str | bool] = {"label": label}
commit = rev_parse_head(kernel_src)
if commit is not None:
    provenance["resolved_commit"] = commit
    dirty = working_tree_dirty(kernel_src)
    if dirty is not None:
        provenance["dirty"] = dirty
        if dirty:
            sha = staged_tree_sha(kernel_src)
            if sha is not None:
                provenance["tree_sha"] = sha
return result._replace(build_provenance=provenance)
```

Update the function's return/var type and docstring (warm-tree builds working-tree state;
`resolved_commit` decorative when dirty; tracked-only scope). Import the two probes.

Tests (`tests/providers/build_host/test_build_provenance.py`): extend the existing warm-tree
tests — clean git tree asserts `{label, resolved_commit, dirty: False}`; a tracked edit asserts
`dirty True` + a `tree_sha` key; untracked-only asserts `dirty True`, no `tree_sha`; non-git
stays `{label}`. Update the existing `test_warm_tree_records_label_and_resolved_commit`
assertion (it currently pins `{label, resolved_commit}` exactly) to include `dirty`.

## Step 3 — widen the value type along the pipeline

Mechanical, must compile together:

- `build_artifacts/results.py` — `BuildOutput.build_provenance: dict[str, str | bool] | None`;
  update the docstring to list `dirty`/`tree_sha` and the warm-tree semantics.
- `services/runs/steps.py` — `BuildStepResult.build_provenance: dict[str, str | bool] | None`;
  replace the str-only `_optional_str_map` use for provenance with a coercion that admits
  `str | bool` values (add `_optional_str_bool_map`, or generalize), keep the `None`-on-malformed
  posture; `dump()` return type widens to include `dict[str, str | bool]`.
- `mcp/tools/lifecycle/runs/common.py` — `_build_provenance_data` / `envelope_for_run`
  `build_provenance` param type widens; it already `cast(JsonValue, ...)`, so no logic change.
- `mcp/tools/lifecycle/runs/view.py` — pass-through type only if annotated.

Tests: `tests/services/runs/test_steps.py` round-trip — a provenance dict with a `bool` survives
`load`/`dump`; a malformed value (e.g. `{"dirty": ["x"]}`) coerces to `None`.

## Step 4 — docs

- `mcp/tools/lifecycle/runs/registrar.py` — extend the `build_profile` description: warm-tree
  builds working-tree state (not HEAD); `runs.get` reports `label`, `resolved_commit`, `dirty`,
  and `tree_sha` (when dirty), tracked-state only.
- `docs/operating/build-source-staging.md` — in the warm-tree section, add the provenance shape
  and the tracked-only/probe-timing caveats from the spec.
- `just docs` to regenerate `docs/guide/reference/runs.md`; verify `just docs-check`.

## Step 5 — full guardrails + AST guard

`just lint`, `just type`, `just test` (focused: provenance, steps, runs tools, and
`tests/mcp/test_no_stringified_flags.py` to confirm the native bool keeps the guard green).
No new stringified flag is introduced (`dirty` is a real `bool`).

## Rollback / cleanup

Pure additive provenance keys + a type widening; no migration, no state change. Reverting the
branch restores `{label, resolved_commit?}`. Persisted rows with the new keys degrade cleanly
on an older reader (extra keys ignored; `_optional_str_map` would have dropped a bool row to
`None` — acceptable, provenance is best-effort).
