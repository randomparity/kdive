# ADR 0461 — Remove the duplicate image build/publish tool

- **Status:** Accepted
- **Date:** 2026-07-27
- **Issue:** #1589
- **Epic:** #1576
- **Implements:** [ADR-0456](0456-agent-operator-mcp-exposure-profiles.md) §3's retired-name search
  vocabulary, through the mechanism [ADR-0458](0458-fold-postmortem-triage-into-postmortem-crash.md)
  established.
- **Amends:** [ADR-0092](0092-image-rootfs-lifecycle.md)'s two-tool operator image surface.

## Context

`images.build` and `images.publish` were not two operations that happened to look alike. They were
one operation registered twice:

- identical wrapper parameters, with identical `Field` descriptions — `provider: str`, `name: str`,
  `packages: tuple[str, ...] = ()`.
- identical annotations: both `_docmeta.mutating()`, both maturity `implemented`, both classified
  `_PLAT_OP` in `kdive.mcp.exposure`.
- identical authorization: one `require_platform_role(ctx, PLATFORM_OPERATOR)` gate, reached
  through the same `_operator_image_build` helper.
- identical execution: both enqueued `JobKind.IMAGE_BUILD` with the same payload under the same
  dedup key, `image_build:{provider}:{name}`. Because the dedup key did not include the tool name,
  calling either one returned the *same job row* — the two tools could not produce different work
  even in principle.
- identical result: the same success envelope, the same `suggested_next_actions`
  `["jobs.get", "jobs.wait"]`.

The only thing that differed was the string written into `platform_audit_log.tool` and the wrapper
docstring. The worker handler behind the job has always run one path — build, validate, publish
row-first — so "build without publishing" was never a capability the split bought.

The two names described the two halves of one job, and the docstrings encouraged an agent to read
them as sequential steps: an agent that called `images.build`, waited for the job, and then called
`images.publish` got a second reference to the job it had already waited on. Epic #1576's
requirement 5 permits consolidation when authorization, annotations, execution class, and result
shape match; here all four matched exactly, and no branch-dependent authorization exists to make
explicit.

## Decision

### 1. `images.publish` is the one tool

`images.build` is removed. `images.publish` survives, because the job's outcome is a published
catalog row and the tool is named for its outcome. It keeps the flat `provider` / `name` /
`packages` parameters ADR-0372 requires, the `IMAGE_BUILD` job kind, the
`image_build:{provider}:{name}` dedup key, and the `platform_operator` gate — no schema change, no
migration, no worker change.

Per epic requirement 7 the wrapper docstring and `Field` descriptions are the agent-facing
contract, so the surviving wrapper carries the build outcome explicitly rather than inheriting the
narrower "publish a built image" framing: it now states that the job builds, validates, and
publishes, that the same call promotes an already-realized `defined` baseline, and that re-issuing
it returns the existing job. `build` and `_operator_image_build` collapse into the single `publish`
handler; `BUILD_TOOL` is deleted.

`platform_audit_log` rows written before this change keep the literal `images.build` in their
`tool` column. Historical rows are not rewritten and no live read filters on that value.

### 2. `images.build` becomes search vocabulary

`RETIRED_TOOL_NAMES` gains one row:

```python
"images.build": "images.publish",
```

Because `tools.search` matches substrings, this also makes the bare word `build` rank
`images.publish`, which is the query an agent that wants an image built will actually type. The
name stays discovery vocabulary only: `tools.invoke("images.build")` returns the usual unknown-tool
`configuration_error`.

### 3. Both curated `kdivectl images` verbs are deleted, not just the retired one

`kdivectl` merges a schema-generated verb surface with a small set of curated overrides, and a
curated verb overrides the generated shape at its path. The curated `images build` and
`images publish` verbs both sent `{"request": {...}}`, a nesting ADR-0372 removed from these tools,
so both command lines sent a payload the server rejects — and the test that covered them asserted
the nested shape against a fake client, so nothing caught it.

Removing only the `images build` verb alongside the tool would have left `kdivectl images publish`
broken. Both curated verbs are therefore deleted and `images publish` takes the generated verb,
whose flags and payload are derived from the live schema. The replacement test drives argv through
`build_parser()` and `dispatch.run()` rather than calling a handler, because a curated verb bypasses
the generated-dispatch seam entirely and a handler-level assertion cannot observe which of the two
surfaces the shipped command line reaches.

## Consequences

- The live registry drops from 137 tools to 136.
- `kdivectl images build` is gone. `kdivectl images publish` works for the first time: it takes
  `--provider`, `--name`, and repeatable `--packages` from the live schema.
- An audit reader that filtered on `tool = 'images.build'` must filter on `images.publish` for new
  rows and accept both when reading history. The trail is pre-release and no shipped consumer does
  this.
- The `images` toolset guide, the served snapshot generated from it, the RBAC matrix, and the tool
  reference lose their `images.build` entry; the image-lifecycle runbook's operator example is now
  a single `images publish` call.
- `kdive.jobs.handlers.image_build`, the `IMAGE_BUILD` job kind, and the `image_catalog` schema are
  untouched; this is a tool-surface change only.

## Rejected alternatives

- **Keeping `images.build` and removing `images.publish`.** The job's outcome is a published
  catalog row, and epic #1576 asks for outcome-named tools. `build` names the mechanism, and a
  `build` that does not publish is not a thing this system can do.
- **Giving the survivor a `publish: bool` parameter to make the split real.** It would invent a
  build-without-publishing mode that no worker path implements, contradicting the no-phantom-feature
  rule, and the guest-contract validation that gates publication is what makes an unpublished build
  meaningless.
- **Keeping `images.build` as an alias onto the same handler.** The project is pre-release and
  follows replace-don't-deprecate; the second name would persist in the catalog, the RBAC matrix,
  the generated CLI, and the served docs while adding no capability.
- **Fixing the curated verbs' payload shape instead of deleting them.** The curated verbs existed
  only to hand-write a payload the generator now derives correctly from the schema. Repairing them
  would keep a second, hand-maintained source of truth for an argument shape that is already
  generated, and would leave the override in place to drift again at the next signature change.
- **Deleting only the `images build` curated verb.** It would fix half the defect and leave
  `kdivectl images publish` sending a rejected payload.
