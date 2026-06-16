# ADR 0136 — `runs.build` reachability: reject misleading git-URI provenance + name the warm-tree gap

- **Status:** Proposed
- **Date:** 2026-06-16
- **Deciders:** kdive maintainers

## Context

Black-box MCP evaluation (defect D1, #481) found the server-build path effectively
unreachable for a first-time caller. `runs.build` fails late with a generic builder error:

> `KDIVE_KERNEL_SRC must be an absolute path to an existing kernel source tree`

for **both** `kernel_source_ref="git:/home/dave/src/linux#v7.0"` and a bare absolute path
`kernel_source_ref="/home/dave/src/linux"`. This blocks the entire
build → install → boot → crash → vmcore → postmortem → debug happy path.

The root cause is a three-part **provenance + validation + docs** gap, not a wiring bug:

1. **A bare-string `kernel_source_ref` is warm-tree provenance metadata, not a path.**
   Git-clone provenance requires the *structured* object
   `{"git": {"remote": ..., "ref": ...}}` (`GitKernelSource`,
   `src/kdive/profiles/build.py:74`); a bare string — *including* one that looks like
   `git:/...#v7.0` — parses as a `NonEmptyStr` and is treated as warm-tree provenance
   (`is_git_source` returns `False`). The caller who *intends* git provenance but writes
   the bare URI form is silently misrouted to the local warm-tree lane.

2. **Warm-tree builds read the operator-set `KDIVE_KERNEL_SRC` env, not the caller's value.**
   A bare-string profile routes to the local `worker-local` build host
   (`src/kdive/services/runs/build_host_selection.py:61,77`). The value the caller passed
   is provenance metadata only; the local builder materializes the workspace from the
   operator-staged `KDIVE_KERNEL_SRC` (`src/kdive/providers/local_libvirt/build.py:127`,
   default empty at `src/kdive/config/core_settings.py:254`). The failing validation is
   `src/kdive/providers/shared/build_host/workspace.py:150`.

3. **No early validation and no surfaced operator step.** `kernel_source_ref` is accepted
   at `runs.create`/`runs.build` with no check that the provenance form is even coherent,
   and the only signal is a late, generic build-time failure that does not distinguish
   "operator never staged a warm tree" from "the path is set but invalid".

A hard constraint shapes the fix: `KDIVE_KERNEL_SRC` is **worker-process** config
(`processes=_WORKER`), read lazily inside `build()`. The server process that runs
`runs.create`/`runs.build` cannot see it, so the server **cannot** validate at admission
time whether a usable warm tree is staged. That check can only sharpen at the build
boundary, where the env is in scope.

## Decision

Three changes, each at the layer that owns the failure:

**1. Reject the misleading git-URI bare string early (parse boundary).** Add a
field-level validator to `ServerBuildProfile.kernel_source_ref` that rejects a *bare
string* whose shape signals an intended-but-unstructured git provenance — a leading
`git:` scheme, a `git+<transport>://` scheme, or any `://` — with a `configuration_error`
whose message points at the structured `{"git": {"remote": ..., "ref": ...}}` form **and**
the need for a registered remote build host. This fires at `BuildProfile.parse`, so both
`runs.create` (early, on the caller's submitted document) and `runs.build` (on the
persisted document) surface it before any host selection or job enqueue. A bare warm-tree
*label* with no scheme and no `://` (e.g. `git#v6.9`, `git+next`, `linux-6.9`, an absolute
path) stays valid — it is legitimate warm-tree provenance metadata and the validator does
not touch it. The `git+` rule is anchored on the `git+…://` URL shape, not a bare `git+`
prefix, so a scheme-less `git+next` label is not over-rejected.

The check lives in `profiles/build.py` (the parse boundary both tool handlers already
call) rather than being duplicated into `create.py` and `build.py`, so there is exactly
one definition of "looks like a git URI" and no drift between the two entry points.

**2. Sharpen the build-time warm-tree error (build boundary).** In
`workspace.py:sync_tree`, split the single generic `CONFIGURATION_ERROR` into two
distinguishable cases, both still `configuration_error`:
- **`KDIVE_KERNEL_SRC` unset/empty:** state that a local (`worker-local`) build requires
  the operator to pre-stage a warm kernel source tree and point at the operator doc.
- **set but not an absolute existing directory:** state the configured value is not a
  usable absolute path to an existing tree.

The configured path value itself is **not** interpolated into the unset/invalid messages
(it is operator host state, and the empty case has nothing to show); the message names the
env var and the remedy, not the value.

**3. Document the operator step and the two reachable lanes (docs).** Add an operator doc
covering (a) how `KDIVE_KERNEL_SRC` is staged for the local `worker-local` lane and (b)
the git-build lane (structured `{"git": {...}}` ref + a registered remote build host via
`build_hosts.*`). Cross-link it from the error messages and the build-plane reference.

## Consequences

- A caller who writes the intuitive-but-wrong `kernel_source_ref: "git:…#…"` now gets an
  immediate, actionable `configuration_error` at `runs.create` naming the structured form
  and the remote-host requirement — not a late generic builder failure. This is the
  primary acceptance path: the MCP surface and its error messages walk the caller to a
  valid git profile.
- A caller who legitimately wants the warm-tree lane, on a deploy where no warm tree is
  staged, still only learns this at build time (the server cannot see worker env) — but
  the build-time error now names the operator step and the doc, instead of an opaque
  generic string. Combined with the operator doc, the warm-tree lane is reachable.
- No new MCP tool, schema field, DB column, migration, or auth-model change. The advertised
  flat tool schema (ADR-0113) is unchanged: the validator runs inside profile parsing, not
  via a new typed parameter. `is_git_source`, the host-selection compatibility checks, and
  the build dispatch are untouched.
- One persisted-document edge: a Run created **before** this change carrying a
  `git:`-style bare string would now be rejected at `runs.build` parse time rather than at
  the builder. This is strictly better (earlier, clearer failure for a profile that could
  never have built) and there is no migration of stored profiles — the rejection is a
  read-time validation, the row is untouched.

## Considered & rejected

- **Auto-coerce a `git:`/`git+`/`://` bare string into a `GitKernelSource`.** Rejected:
  the parse from a bare URI to `{remote, ref}` is ambiguous (where does the remote end and
  the ref begin in `git:/home/dave/src/linux#v7.0`?), and silently rewriting a caller's
  provenance is exactly the class of surprise this issue is about. Reject with guidance and
  let the caller submit the unambiguous structured form.
- **Validate "warm tree is staged" at `runs.create`/`runs.build`.** Rejected: impossible
  without a layer violation — `KDIVE_KERNEL_SRC` is worker-process env the server does not
  read. Pushing it to the server would require shipping worker host state into the server
  process or a round-trip to the worker at admission time; both are out of proportion to
  the fix. The build-boundary error sharpening (change 2) is where this check can honestly
  live.
- **A read-only `runs.build_profile_examples` discovery tool** (mirroring
  `systems.profile_examples`, ADR-0124). Rejected for this blocker as scope creep: it is a
  real onboarding improvement but orthogonal to D1 reachability and overlaps the D2 schema
  work (#482). Left as a follow-up.
- **Make `kernel_source_ref` a typed union parameter on the tool** (advertise the
  structured shape in the input schema). Rejected here: the flat-schema sweep (ADR-0113)
  and the typed-profile-param work are the D2 lane (#482); doing it here would collide with
  that agent's file scope and re-litigate a settled schema-advertisement approach.
