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

The facts already exist and are correct in the codebase, and the bare boot-format one-liner is
even inline in the `runs.create` `build_profile.arch` `Field` ("bzImage for x86_64, ELF vmlinux
for ppc64le"). The gap is the *fuller* actionable guidance an agent needs in one read before
building — the ppc64le strip requirement and why, cross-compile triples, the `crashkernel`
default, and (the root of several reported cases) that the boot format follows the **target**
arch, not the build host an agent may have misidentified. That guidance exists only inside the
257-line procedural narrative today.

## Goal / non-goals

**Goal.** Give an agent a discoverable, at-a-glance, per-architecture reference of the
build-and-package rules that vary by arch, reachable in one hop from where it decides to build,
so the "build → upload → rejected → rebuild" loop for the arch boot-image split is closed on
the discoverability side.

**Non-goals.**
- Not changing any validation behavior. The validator (`build_artifacts/validation.py`) is
  unchanged; this is agent-facing documentation only.
- Not adding a new response field or tool, and not rewording the inline boot-format prose in
  the `arch` Field (deriving it from `BOOT_MEMBER_FORMATS` is a recorded follow-up). The only
  tool-surface change is repointing the `arch` Field's existing doc citation at the new
  reference — one line per Field.
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
2. A "determine your architecture first" section: agents conflate the **build-host** arch (what
   they compile on) with the **target** arch (what the kernel boots on), and assuming x86_64
   when actually on `ppc64le` produces a kernel for the wrong arch. It tells the agent to check
   the build host explicitly (`uname -m`), read the provisioned target from
   `systems.get`/`runs.get` rather than inferring it, and states that the boot-image format
   follows the target arch (which a cross-compile inverts from the host). This is the root of
   the reported failure, one level up from the boot-image symptom.
3. One `## <arch>` section per supported arch, each stating: what `boot/vmlinuz` must be for
   that arch and the one-line reason, any pre-package step (ppc64le: strip the build-tree
   `vmlinux`; DWARF goes in the optional `vmlinux` artifact, not the boot member), the
   cross-compile note, and the `crashkernel` default in context.
4. A short shared "same for every arch" note (one combined gzip tar named `kernel`,
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
- Cite from the `runs.create` build-profile `arch` `Field` description. That Field already
  states the boot-format one-liner and already cites
  `resource://kdive/docs/operating/external-build-upload.md`; **repoint** that existing citation
  at the new focused reference (which itself links onward to external-build-upload for the full
  recipe), rather than appending a second URL that would bloat the description. Do the same for
  the `runs.create` `build_profile` Field if it carries its own citation. This is a one-line
  cross-link per Field, not a rewrite of the boot-format wording — deriving that hardcoded
  wording from `BOOT_MEMBER_FORMATS` is the deferred follow-up recorded in the ADR, out of
  scope here.
  - Guard note: check for an existing test that asserts the `arch` Field cites
    external-build-upload before repointing; if one exists, update it to the new target (the
    new doc chains onward, so no reachability is lost).

### Drift guard (the anti-rot mechanism)

A new pytest (`tests/mcp/resources/test_kernel_build_per_arch_doc.py`) over the **canonical**
doc (`docs/guide/kernel-build-per-arch.md`; `resources-docs-check` guarantees the snapshot
equals it):

- **Set completeness:** collect only `##` headings whose text is *exactly* a supported-arch
  token (membership in `SUPPORTED_ARCHES`), not "any `##` line"; assert that set equals
  `SUPPORTED_ARCHES`. Scoping to exact-arch-token headings is what makes the negative guarantee
  ("no unsupported arch heads a section") enforceable while incidental prose that names another
  arch ("powerpc has no bzImage") does not false-trip. A `_TRAITS` row added without a section
  fails here.
- **Boot-container names:** assert each `BOOT_MEMBER_FORMATS[arch].container` string appears
  in that arch's section, so a renamed container is caught.
- **Strip-required predicate (the load-bearing rule):** for any arch whose boot format is an
  ELF kernel, assert that arch's section contains the `strip -s` command token. "Is an ELF
  kernel" is read **structurally** from the contract — the format carries an ELF-magic
  `MagicPin` at offset 0 (`\x7fELF`) — not from a substring of the human-readable `container`
  display string, so a display-string rename cannot silently drop an arch from the
  strip-required set. Binding to `strip -s` (not the bare word `strip`, which appears
  incidentally in "the bzImage is already stripped") avoids a trivially-satisfied common-word
  match. This binds the single most error-prone instruction (ppc64le: strip the build-tree
  `vmlinux` first) to a structural source signal instead of leaving it as unguarded prose; the
  guard pins the command's *presence*, not its full wording.
- **`crashkernel` values:** assert the doc contains the exact `default_crashkernel_summary()`
  output, so a changed default fails until resynced.
- **Registration:** assert the new URI is in `DOC_RESOURCES` with `audience="all"` and the
  snapshot exists (the existing `test_doc_resources.py` may already cover generic
  registration; add only what is arch-specific).

The guard is deliberately honest about reach: it binds set-completeness, the `crashkernel`
string, the boot-container name, and the strip-required predicate; the *explanatory* prose
(why DWARF overruns the scan bound, cross-compile triples) is review-only, not drift-guarded.

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
