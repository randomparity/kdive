# ADR 0136 — `runs.build` reachability: sharpen the warm-tree build error + name both lanes

- **Status:** Accepted
- **Date:** 2026-06-16
- **Deciders:** kdive maintainers

## Context

Black-box MCP evaluation (defect D1, #481) found the server-build path effectively
unreachable for a first-time caller. `runs.build` fails late with a generic builder error:

> `KDIVE_KERNEL_SRC must be an absolute path to an existing kernel source tree`

for **both** `kernel_source_ref="git:/home/dave/src/linux#v7.0"` and a bare absolute path
`kernel_source_ref="/home/dave/src/linux"`. This blocks the entire
build → install → boot → crash → vmcore → postmortem → debug happy path.

The root cause is a **provenance + error-quality + docs** gap, not a wiring bug:

1. **A bare-string `kernel_source_ref` is warm-tree provenance metadata, not a path.**
   Git-clone provenance requires the *structured* object
   `{"git": {"remote": ..., "ref": ...}}` (`GitKernelSource`,
   `src/kdive/profiles/build.py:74`); a bare string — *including* one that looks like
   `git:/...#v7.0` — parses as a `NonEmptyStr` and is treated as warm-tree provenance
   (`is_git_source` returns `False`). The caller who *intends* git provenance but writes
   the bare URI form is routed to the local warm-tree lane, where (absent a staged tree)
   the build fails.

2. **Warm-tree builds read the operator-set `KDIVE_KERNEL_SRC` env, not the caller's value.**
   A bare-string profile routes to the local `worker-local` build host
   (`src/kdive/services/runs/build_host_selection.py:61,77`). The value the caller passed
   is provenance metadata only; the local builder materializes the workspace from the
   operator-staged `KDIVE_KERNEL_SRC` (`src/kdive/providers/local_libvirt/build.py:127`,
   default empty at `src/kdive/config/core_settings.py:254`). The failing validation is
   `src/kdive/providers/shared/build_host/workspace.py:150`.

3. **The build-time error is generic and lane-blind.** The single
   `KDIVE_KERNEL_SRC must be an absolute path…` string does not distinguish "operator
   never staged a warm tree" from "the path is set but invalid", and it names neither the
   operator pre-stage step nor the *alternative* git-build lane (structured ref + a
   registered remote build host). So a caller who hit it had no actionable next step.

Two constraints shape the fix:

- `KDIVE_KERNEL_SRC` is **worker-process** config (`processes=_WORKER`), read lazily
  inside `build()`. The server process that runs `runs.create`/`runs.build` cannot see it,
  so the server **cannot** validate at admission time whether a usable warm tree is staged.
  That check can only sharpen at the build boundary, where the env is in scope.
- A bare-string `kernel_source_ref` with a URI-looking shape is the project's **established
  convention** for a warm-tree provenance *label*: `git+https://…#v6.9`, `git:abc123`, and
  `file:///src/linux` appear as valid bare-string labels across ~30 existing fixtures and
  in live build profiles (e.g. `tests/mcp/lifecycle/test_runs_tools.py`). So a create-time
  rejection keyed on "looks like a git URI" would reject the codebase's own valid labels
  and is wrong (see Considered & rejected).

## Decision

Two changes, at the layers that own the failure. No create-time rejection is added (see
Considered & rejected — it would break the established bare-string-label convention).

**1. Sharpen the build-time warm-tree error (build boundary).** In
`workspace.py:sync_tree`, split the single generic `CONFIGURATION_ERROR` into two
distinguishable cases, both still `configuration_error`, and have **both** messages name
the two reachable lanes — the operator warm-tree staging step *and* the git-build lane
(structured `{"git": {...}}` ref + a registered remote build host):
- **`KDIVE_KERNEL_SRC` empty or whitespace-only (`not kernel_src.strip()`):** state that a
  local (`worker-local`) build requires the operator to pre-stage a warm kernel source tree
  (`KDIVE_KERNEL_SRC`); point at the operator doc; and note the git lane as the alternative.
  A whitespace-by-accident value routes here, not to the invalid-path case.
- **non-blank but not an absolute existing directory:** state the configured
  `KDIVE_KERNEL_SRC` is not a usable absolute path to an existing tree; point at the same
  doc and lane guidance.

The configured path value itself is **not** interpolated into the messages (it is operator
host state, and the empty case has nothing to show); the message names the env var, the
operator doc, the git-lane alternative, and the remedy — not the value.

**2. Document the operator step and the two reachable lanes (docs).** Add an operator doc
covering (a) how `KDIVE_KERNEL_SRC` is staged for the local `worker-local` lane and (b)
the git-build lane (structured `{"git": {...}}` ref + a registered remote build host via
`build_hosts.*`). Cross-link it from the error messages and the operating index.

Because the build-time error now names the structured git form **and** the remote-host
requirement **and** the operator pre-stage step, the first and only signal a caller sees is
no longer a bare generic string: it is an actionable message that, with the linked doc,
walks the caller (or operator) to a reachable lane — which is the acceptance criterion.

## Consequences

- A caller who hits the warm-tree build failure (whether they wrote a bare `git:…` label
  intending a clone, or genuinely want warm-tree but no tree is staged) now gets an
  actionable `configuration_error` that names the structured git form, the remote-host
  requirement, and the operator pre-stage step, with a linked operator doc — instead of an
  opaque generic string. That is the acceptance: the first/only signal is no longer a bare
  `KDIVE_KERNEL_SRC` failure.
- The error is surfaced at **build time** (worker), not at `runs.create`, because the
  server cannot see worker env and a create-time "looks like git" rejection would break the
  established bare-label convention. The trade-off is a later signal; the gain is correctness
  (no false rejection of valid labels) and a self-correcting message + doc.
- No new MCP tool, schema field, DB column, migration, parse-boundary validator, or
  auth-model change. The advertised flat tool schema (ADR-0113), `is_git_source`, the
  host-selection compatibility checks, the build dispatch, and `BuildProfile.parse` are all
  untouched. The change is confined to the `sync_tree` error strings plus docs.
- No persisted-document or fixture impact: every existing bare-string `kernel_source_ref`
  (including `git+https://…`, `git:abc123`, `file://…`) parses exactly as before.

## Considered & rejected

- **Reject a "looks like a git URI" bare string at `runs.create`/`runs.build` (the original
  L1 in this ADR's first draft).** Rejected after grounding it against the codebase: a
  bare-string `kernel_source_ref` with a URI-looking shape is the project's **established
  convention** for a warm-tree provenance label. `git+https://git.kernel.org/…#v6.9`,
  `git:abc123`, and `file:///src/linux` are used as valid bare-string labels across ~30
  fixtures and in live build profiles. A `git:`/`git+`/`://` rejection rule would reject the
  codebase's own valid labels and break ~15 build-profile tests; narrowing it to the
  issue's literal `git:/<absolute-path>` form would catch one example while still admitting
  every other misleading bare string, so it buys little for the convention risk it courts.
  The honest signal lives at the build boundary (change 1), where the message can name both
  lanes without guessing the caller's intent from a string shape. The user chose this
  (Option B) over both the broad and the narrow create-time rejection.
- **Auto-coerce a `git:`/`git+`/`://` bare string into a `GitKernelSource`.** Rejected: the
  parse from a bare URI to `{remote, ref}` is ambiguous, and silently rewriting a caller's
  provenance is exactly the class of surprise this issue is about.
- **Validate "warm tree is staged" at `runs.create`/`runs.build`.** Rejected: impossible
  without a layer violation — `KDIVE_KERNEL_SRC` is worker-process env the server does not
  read. The build-boundary error sharpening (change 1) is where this check can honestly
  live.
- **A read-only `runs.build_profile_examples` discovery tool** (mirroring
  `systems.profile_examples`, ADR-0124). Rejected for this blocker as scope creep: a real
  onboarding improvement but orthogonal to D1 and overlapping the D2 schema work (#482).
- **Make `kernel_source_ref` a typed union parameter on the tool.** Rejected here: the
  flat-schema sweep (ADR-0113) and the typed-profile-param work are the D2 lane (#482).
