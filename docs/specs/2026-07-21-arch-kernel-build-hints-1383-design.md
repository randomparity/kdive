# Per-architecture kernel build hints — design (#1383)

- Issue: #1383 "Create Arch Specific Kernel Build Hints"
- ADR: [ADR-0412](../adr/0412-per-arch-kernel-build-hints-resource.md)
- Status: Draft
- Date: 2026-07-21

## Problem

Agents driving the external build lane repeat expensive build-and-upload work when they miss
an architecture-specific packaging rule. The reported failure: an agent builds a kernel, tars
the module tree plus a boot image, uploads, and only at `runs.complete_build` learns the
`boot/vmlinuz` member is wrong for the target architecture — then rebuilds and re-uploads from
scratch. The sharp edge is the boot-image split:

| arch | `boot/vmlinuz` must be | why it trips agents |
|---|---|---|
| `x86_64` | the **bzImage** (`arch/x86/boot/bzImage`), renamed | the ELF `vmlinux` is *rejected* here |
| `ppc64le` | the **stripped ELF `vmlinux`** | powerpc has no bzImage; the *unstripped* vmlinux (full DWARF, hundreds of MB) also overruns the validator scan bound |

The facts already exist and are already correct in the codebase; the gap is that an agent does
not reach them *before* building.

## Goal / non-goals

**Goal.** Give an agent a discoverable, at-a-glance, per-architecture reference of the
build-and-package rules that vary by arch, reachable in one hop from where it decides to build,
so the "build → upload → rejected → rebuild" loop for the arch boot-image split is closed on
the discoverability side.

**Non-goals.**
- Not changing any validation behavior. The validator (`build_artifacts/validation.py`) is
  unchanged; this is agent-facing documentation only.
- Not adding a new response field or tool. The only tool-surface change is one `Field`
  description pointer.
- Not restating the full procedural tar recipe. `docs/operating/external-build-upload.md`
  stays the how-to-run-it narrative; the new doc is the what-differs-by-arch reference.
- Not an AI surface. This is documentation consumed *by* an external agent; it adds no LLM
  call, prompt, retrieval path, classifier, or agent loop inside kdive, so no eval plan applies
  (per the /design AI-surface gate).

## Ground truth (single sources this design must not diverge from)

- **Supported arches** = `SUPPORTED_ARCHES` = `frozenset(_TRAITS)` in
  `src/kdive/domain/platform/arch_traits.py`. Today: exactly `{x86_64, ppc64le}`.
- **Per-arch boot-image format** = `BOOT_MEMBER_FORMATS` in
  `src/kdive/build_artifacts/validation.py`. `container` names: `"bzImage"` (x86_64),
  `"ppc64le ELF (vmlinux)"` (ppc64le). An import-time check already asserts
  `set(BOOT_MEMBER_FORMATS) == SUPPORTED_ARCHES`.
- **Per-arch `crashkernel` default** = `_TRAITS[arch].default_crashkernel` (`256M` x86_64,
  `512M` ppc64le), already rendered agent-facing by
  `arch_traits.default_crashkernel_summary()` → `"256M on x86_64, 512M on ppc64le"`.
- **Doc-resource pipeline** (ADR-0151): canonical `docs/<path>` → `scripts/gen_doc_resources.py`
  snapshot into `src/kdive/mcp/resources/_content/<file>` → an entry in
  `registrar.DOC_RESOURCES`. `resources-docs-check` gates snapshot drift; `served-doc-links`
  gates that every `resource://kdive/docs/...` citation in a served doc resolves.

## Design

### The new doc

Add `docs/guide/kernel-build-per-arch.md`, an agent-facing reference. Structure:

1. A one-paragraph orientation naming the failure it prevents and pointing at
   `resource://kdive/docs/operating/external-build-upload.md` for the full recipe and
   `artifacts.expected_uploads` for the machine-readable byte contract.
2. One `## <arch>` section per supported arch, each stating: what `boot/vmlinuz` must be for
   that arch and the one-line reason, any pre-package step (ppc64le: strip the build-tree
   `vmlinux`; DWARF goes in the optional `vmlinux` artifact, not the boot member), the
   cross-compile note, and the `crashkernel` default in context.
3. A short shared "same for every arch" note (one combined gzip tar named `kernel`,
   `boot/vmlinuz` first, real `.ko` under `lib/modules/<release>/`, drop the `build`/`source`
   symlinks) — kept to a pointer-plus-summary, not a re-derivation of the recipe.

The section headers are the machine-checkable contract: `## x86_64`, `## ppc64le`. The doc
embeds the exact `default_crashkernel_summary()` string verbatim so the guard can assert
equality.

### Registration and discoverability

- Add a `DocResource` to `registrar.DOC_RESOURCES`:
  `uri="resource://kdive/docs/guide/kernel-build-per-arch.md"`, `audience="all"`,
  `required_kind=None`, with a title and description naming the arch boot-image split.
- `just resources-docs` writes the `_content/kernel-build-per-arch.md` snapshot;
  `resources-docs-check` then verifies it.
- Cross-link from `docs/guide/agent-index.md`: in the build stage (step 4), cite the new
  `resource://` URI beside the existing external-build-upload reference. (The agent-index
  snapshot is re-mirrored by `resources-docs`; the edit must not touch the `~NNN tools`
  doc-constant.)
- Cite from the `runs.create` build-profile `arch` `Field` description: append a short "see
  resource://kdive/docs/guide/kernel-build-per-arch.md for per-arch packaging" pointer, so the
  agent reading the schema at build time has the link.

### Drift guard (the anti-rot mechanism)

A new pytest (`tests/mcp/resources/test_kernel_build_per_arch_doc.py`) over the **canonical**
doc (`docs/guide/kernel-build-per-arch.md`; `resources-docs-check` guarantees the snapshot
equals it):

- **Set completeness:** parse the `##`-level section headers; assert the set of documented
  arches equals `SUPPORTED_ARCHES`. A `_TRAITS` row added without a section fails here; a
  section for an arch the platform does not support also fails.
- **Boot-container names:** assert each `BOOT_MEMBER_FORMATS[arch].container` string appears
  in that arch's section, so a renamed container is caught.
- **`crashkernel` values:** assert the doc contains the exact `default_crashkernel_summary()`
  output, so a changed default fails until resynced.
- **Registration:** assert the new URI is in `DOC_RESOURCES` with `audience="all"` and the
  snapshot exists (the existing `test_doc_resources.py` may already cover generic
  registration; add only what is arch-specific).

## Acceptance criteria

1. `resource://kdive/docs/guide/kernel-build-per-arch.md` is listable and readable over MCP
   (audience `all`), and its content covers exactly `x86_64` and `ppc64le`, each naming the
   correct `boot/vmlinuz` format.
2. `agent-index.md`'s build stage and the `runs.create` build-profile `arch` field both cite
   the new resource URI; `served-doc-links` passes (the citations resolve).
3. The drift-guard pytest fails if a hypothetical arch is added to `_TRAITS` without a doc
   section, if a `BOOT_MEMBER_FORMATS` container name changes without a doc edit, or if a
   `crashkernel` default changes without a doc edit. (Verified by temporarily perturbing the
   source in the red step, then reverting.)
4. `just ci` is green, including `resources-docs-check`, `doc-constants-check`,
   `served-doc-links`, `lint`, `type`, and `test`.

## Edge cases and failure modes

- **A future third arch.** Adding `_TRAITS`/`BOOT_MEMBER_FORMATS` rows without a doc section
  fails the completeness test — the intended forcing function. The test message must name the
  missing arch so the fix is obvious.
- **Header-parse fragility.** The completeness test keys on `##` section headers; a
  non-arch `##` header (e.g. a "Same for every arch" section) would be miscounted as an arch.
  The test must scope to headers that are exactly an arch token (match against
  `SUPPORTED_ARCHES` membership), not "any `##` line," so shared/aux sections do not pollute
  the set. This is the main correctness risk in the guard and is called out for the plan.
- **Snapshot vs canonical.** The guard reads canonical `docs/`; if it read `_content/` it could
  pass on a stale snapshot. Read canonical; let `resources-docs-check` bind the snapshot.
- **`crashkernel` string coupling.** Embedding the exact `default_crashkernel_summary()` output
  couples the doc prose to the renderer's format (`"<size> on <arch>"`, arch-sorted). That is
  intentional and is the same single-source reuse `runs.install` already relies on.
- **Link direction.** The new doc links *out* to external-build-upload (already served) and is
  linked *in* from agent-index; both directions must resolve under `served-doc-links`.

## Rollback

Pure additive docs + one registrar row + one `Field`-text line + one test. Rollback is deleting
the doc, the snapshot, the registrar row, the `Field` pointer, the agent-index citation, the
ADR-README row, and the test; no data or schema state is touched.
