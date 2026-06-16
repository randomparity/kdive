# Spec — `runs.build` reachability (#481, D1 blocker)

- **Issue:** #481
- **ADR:** [0136](../../adr/0136-runs-build-reachability.md)
- **Date:** 2026-06-16

## Problem

A first-time MCP caller cannot reach a successful `runs.build`. Both
`kernel_source_ref="git:/home/dave/src/linux#v7.0"` and a bare absolute path fail with the
generic builder error `KDIVE_KERNEL_SRC must be an absolute path to an existing kernel
source tree`. The server-build lane is the entry to the whole build→install→boot→debug
path, so this is the D1 blocker.

Root cause (confirmed against code, see ADR-0136 Context): a bare-string `kernel_source_ref`
is *warm-tree provenance metadata*, not a path; git-clone provenance requires the structured
`{"git": {"remote": ..., "ref": ...}}` object. A `git:`-looking bare string is silently
routed to the local warm-tree lane and only fails at the build boundary, where the builder
reads the operator-staged `KDIVE_KERNEL_SRC` (worker-process env, server-invisible).

## Goal / acceptance

Every `runs.build` failure on the server-build path is specific and actionably routed,
rather than collapsing to one generic `KDIVE_KERNEL_SRC` string. Concretely, two failure
classes are separated:

1. **Caller-resolvable provenance error.** A bare-string `kernel_source_ref` that looks like
   an intended git URI is rejected early at the caller's own boundary (`runs.create`) with a
   message naming the structured `{"git": {...}}` form and the remote-build-host
   requirement. The caller resolves this themselves by resubmitting the structured form.

2. **Operator-prerequisite error.** A warm-tree build with no staged source now names the
   exact operator step and the operator doc instead of an opaque failure. The caller cannot
   resolve this alone; the message routes them (or the operator) to the prerequisite.

**Residual stated honestly:** on a default deploy the seeded `worker-local` host exists but
`KDIVE_KERNEL_SRC` is unset and no *remote* build host is registered, so end-to-end success
still requires an operator to stage a warm tree (or register a remote host). This issue does
not automate that operator setup — by construction it cannot, because `KDIVE_KERNEL_SRC` is
worker-process env the server never reads (ADR-0136). The acceptance is "no generic
`KDIVE_KERNEL_SRC` failure as the first and only signal," not "an unprivileged caller alone
reaches a green build on a vanilla deploy."

## Design (three layers)

### L1 — Reject misleading git-URI bare strings at the parse boundary

Add a field validator to `ServerBuildProfile.kernel_source_ref` in `profiles/build.py`.
When the value is a **bare string** (not a `GitKernelSource`) and its shape signals an
intended-but-unstructured git provenance, raise so `BuildProfile.parse` maps it to a
`configuration_error`.

"Looks like an intended git URI" is defined narrowly:
- a leading `git:` scheme (e.g. `git:/home/dave/src/linux#v7.0`), or
- a leading `git+<transport>://` scheme (e.g. `git+ssh://`, `git+https://`), or
- any `://` substring (`ssh://`, `https://`, `http://`, …).

The `git+` rule is anchored on the `git+…://` URL shape, **not** a bare `git+` prefix, so a
hypothetical scheme-less label like `git+next` is left valid (the only `git+` form rejected
is an actual git-transport URL).

Explicitly **valid** (untouched) bare warm-tree labels:
- `git#v6.9` (a `#fragment`, no scheme, no `://`) — the existing test fixture form.
- `git+next` (a `git+` prefix with no `://`) — not a URL, stays a warm-tree label.
- `linux-6.9`, `mainline`, any scheme-less label.
- an absolute path `/home/dave/src/linux`.

The error message names the structured form and the remote-host requirement, e.g.:

> `kernel_source_ref "<…>" looks like a git URI but bare strings are warm-tree provenance,
> not git-clone provenance. For a git build, submit the structured form
> {"git": {"remote": "…", "ref": "…"}} and target a registered remote build host
> (build_hosts.*). For a warm-tree build, pass a plain label or path and stage the source
> on the worker.`

The submitted value MAY appear in the message because it is the caller's own input echoed
back (not secret/guest-derived). But to stay consistent with the existing redaction
guarantee on `BuildProfile.parse` (which strips submitted values from `errors[].input`),
raise this as a `CategorizedError(CONFIGURATION_ERROR)` from the validator and let parse's
`ValidationError` handler scrub it — OR raise the `CategorizedError` directly with the
value in `message` only (never in `details.errors[].input`). **Decision: raise a Pydantic
`ValueError` inside the validator** so it flows through the existing `ValidationError`
redaction path; the actionable guidance is in the `msg`, and the offending value is **not**
echoed (consistent with every other field error from this parser). The `msg` is generic
enough to act on without the value.

This validator lives only on `ServerBuildProfile` (the server lane); `ExternalBuildProfile`
has no `kernel_source_ref`.

Because both `runs.create` (caller's submitted document) and `runs.build` (persisted
document) call `BuildProfile.parse`, the rejection fires at the earliest boundary the
caller touches.

### L2 — Sharpen the build-time warm-tree error

In `providers/shared/build_host/workspace.py:sync_tree`, split the single generic error:

- `kernel_src` is empty or whitespace-only (`not kernel_src.strip()`) →
  `configuration_error`: a local `worker-local` build requires the operator to pre-stage a
  warm kernel source tree (`KDIVE_KERNEL_SRC`); point at the operator doc. A value set to
  whitespace by a deploy-template accident routes here (the "you haven't staged a tree"
  message), not to the invalid-path case.
- `kernel_src` is non-blank but not an absolute existing directory → `configuration_error`:
  the configured `KDIVE_KERNEL_SRC` is not a usable absolute path to an existing tree.

Neither message interpolates the configured value (operator host state; the empty case has
nothing to show). Messages name the env var and the remedy.

### L3 — Operator documentation

Add `docs/operating/build-source-staging.md` (or extend the nearest existing build-plane
operator doc) covering:
- The warm-tree lane: stage `KDIVE_KERNEL_SRC` for the `worker-local` host; what a valid
  tree is; how the worker reads it.
- The git-build lane: structured `{"git": {...}}` `kernel_source_ref` + register a remote
  build host (`build_hosts.*`, ADR-0099/0100/0101).
- Cross-link from the build-plane reference and from the L2 error remedy text.

## Test plan (behavior, at the prescribed boundaries)

`tests/profiles/test_build.py` / `test_build_profile_source.py`:
- `git:/home/dave/src/linux#v7.0` (bare) → `CONFIGURATION_ERROR`.
- `git+ssh://host/linux` (bare) → `CONFIGURATION_ERROR`.
- `https://github.com/torvalds/linux` (bare) → `CONFIGURATION_ERROR`.
- `git#v6.9` (bare, existing fixture) → parses as warm-tree, `is_git_source` False (regression guard).
- `git+next` (bare `git+` prefix, no `://`) → parses as warm-tree (the narrowed `git+…://`
  rule does not over-reject a scheme-less label).
- `/home/dave/src/linux` (bare absolute path) → parses as warm-tree (still valid; the L2
  error, not L1, governs a missing tree).
- structured `{"git": {"remote": "…", "ref": "…"}}` → parses, `is_git_source` True.
- redaction: the submitted offending value does NOT appear in `details.errors[].input`.

`tests/providers/build_host/test_transport_seams.py` (or the sync_tree test home):
- empty `kernel_src` → `CONFIGURATION_ERROR` naming the operator pre-stage step.
- whitespace-only `kernel_src="   "` → same `CONFIGURATION_ERROR` as empty (the pre-stage
  message, not the invalid-path one).
- `kernel_src="relative/path"` or a nonexistent absolute dir → `CONFIGURATION_ERROR`
  distinct message (set-but-invalid).

`tests/mcp/lifecycle/test_runs_tools.py`:
- `runs.create` with `kernel_source_ref="git:…#…"` → failure envelope,
  `configuration_error` (the early reachability signal at the caller's boundary).

## Out of scope / follow-ups

- A `runs.build_profile_examples` discovery tool (mirrors ADR-0124) — overlaps D2 (#482).
- Typed/advertised `kernel_source_ref` schema — D2 lane (#482), ADR-0113 sweep.
- Server-side "is a warm tree staged" admission check — impossible without a layer
  violation (worker-process env), see ADR-0136 rejected alternatives.
