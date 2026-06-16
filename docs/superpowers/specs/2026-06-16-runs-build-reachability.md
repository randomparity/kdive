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
`{"git": {"remote": ..., "ref": ...}}` object. A bare string is routed to the local
warm-tree lane and fails at the build boundary, where the builder reads the operator-staged
`KDIVE_KERNEL_SRC` (worker-process env, server-invisible). The single generic error names
neither the operator pre-stage step nor the alternative git-build lane, so the caller has no
actionable next step.

## Goal / acceptance

Every `runs.build` failure on the server-build path is specific and actionably routed,
rather than collapsing to one generic `KDIVE_KERNEL_SRC` string. The first and only signal a
caller sees on the warm-tree path is no longer a bare generic failure: it names both
reachable lanes (operator warm-tree staging + the git-build lane) and links the operator
doc, so a caller can self-correct — or route the operator to the prerequisite — from the
error text + docs alone.

**Why no create-time rejection.** An earlier draft added an L1 that rejected a bare-string
`kernel_source_ref` "looking like a git URI" at `runs.create`. Grounding it against the
codebase killed it: a URI-looking bare string is the project's **established convention** for
a warm-tree provenance *label* — `git+https://…#v6.9`, `git:abc123`, `file:///src/linux`
appear as valid bare-string labels in ~30 fixtures and live build profiles
(`tests/mcp/lifecycle/test_runs_tools.py`, `tests/mcp/_seed.py`, …). A `git:`/`git+`/`://`
rejection would reject the codebase's own valid labels and break ~15 build-profile tests;
the narrow `git:/<path>` variant catches the one literal example while admitting every other
misleading string. The user chose **Option B** (drop L1 entirely); the honest signal lives at
the build boundary where the message can name both lanes without guessing intent from a
string shape.

**Residual stated honestly:** on a default deploy the seeded `worker-local` host exists but
`KDIVE_KERNEL_SRC` is unset and no *remote* build host is registered, so end-to-end success
still requires an operator to stage a warm tree (or register a remote host). This issue does
not automate that operator setup — by construction it cannot, because `KDIVE_KERNEL_SRC` is
worker-process env the server never reads (ADR-0136). The acceptance is "no generic
`KDIVE_KERNEL_SRC` failure as the first and only signal," not "an unprivileged caller alone
reaches a green build on a vanilla deploy."

## Design (two layers)

### L1 — Sharpen the build-time warm-tree error

In `providers/shared/build_host/workspace.py:sync_tree`, split the single generic error into
two distinguishable `configuration_error` cases, and have **both** messages name the two
reachable lanes — the operator warm-tree staging step *and* the git-build lane (structured
`{"git": {...}}` ref + a registered remote build host):

- `kernel_src` is empty or whitespace-only (`not kernel_src.strip()`) →
  `configuration_error`: a local `worker-local` build requires the operator to pre-stage a
  warm kernel source tree (`KDIVE_KERNEL_SRC`); point at the operator doc; name the git lane
  as the alternative. A value set to whitespace by a deploy-template accident routes here
  (the "you haven't staged a tree" message), not to the invalid-path case.
- `kernel_src` is non-blank but not an absolute existing directory → `configuration_error`:
  the configured `KDIVE_KERNEL_SRC` is not a usable absolute path to an existing tree; same
  doc + lane guidance.

Neither message interpolates the configured value (operator host state; the empty case has
nothing to show). Messages name the env var, the operator doc, the git-lane alternative, and
the remedy. `BuildProfile.parse`, the schema, and `is_git_source` are untouched — this is an
error-string change confined to `sync_tree`.

### L2 — Operator documentation

`docs/operating/build-source-staging.md` covers:
- The warm-tree lane: stage `KDIVE_KERNEL_SRC` for the `worker-local` host; what a valid
  tree is; how the worker reads it.
- The git-build lane: structured `{"git": {...}}` `kernel_source_ref` + register a remote
  build host (`build_hosts.register_ssh` / `…_ephemeral_libvirt`, ADR-0099/0100/0101).
- Cross-linked from the operating index and referenced by the L1 error remedy text.

## Test plan (behavior, at the prescribed boundary)

`tests/providers/local_libvirt/test_build.py` (the existing `sync_tree` test home):
- empty `kernel_src=""` → `CONFIGURATION_ERROR`; message names the operator pre-stage step
  AND the git-lane alternative AND the operator doc path.
- whitespace-only `kernel_src="   "` → same `CONFIGURATION_ERROR` / same message as empty
  (the pre-stage message, not the invalid-path one).
- `kernel_src="linux"` (relative) and a nonexistent absolute dir → `CONFIGURATION_ERROR`
  with the distinct set-but-invalid message (names the doc + lanes, not the pre-stage text).
- `kernel_src="/"` (filesystem root) → still rejected (existing guard preserved).
- the configured path value is NOT interpolated into either message.

No new test asserts a create-time rejection (Option B adds none); existing
build-profile fixtures using `git+https://…`/`git:abc123`/`file://…` continue to parse
unchanged, which the full suite already exercises.

## Out of scope / follow-ups

- A `runs.build_profile_examples` discovery tool (mirrors ADR-0124) — overlaps D2 (#482).
- Typed/advertised `kernel_source_ref` schema — D2 lane (#482), ADR-0113 sweep.
- Any create-time provenance rejection — rejected (Option B); see ADR-0136 rejected
  alternatives (would break the established bare-string-label convention).
- Server-side "is a warm tree staged" admission check — impossible without a layer
  violation (worker-process env), see ADR-0136 rejected alternatives.
