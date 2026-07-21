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
  `arch_traits.default_crashkernel_summary()` → `"512M on ppc64le, 256M on x86_64"` (the
  renderer is arch-sorted, so `ppc64le` sorts before `x86_64`; note this is the reverse of the
  order in that function's own docstring example, a pre-existing cosmetic nit in ADR-0346 that
  is out of scope here — the doc embeds the *actual* rendered string, verified by executing it).
- **Doc-resource pipeline** (ADR-0151): canonical `docs/<path>` → `scripts/gen_doc_resources.py`
  snapshot into `src/kdive/mcp/resources/_content/<file>` → an entry in
  `registrar.DOC_RESOURCES`. `resources-docs-check` gates snapshot drift. Two distinct citation
  guards, easy to conflate: `served-doc-links` (`check-served-doc-links.sh`) fails a served doc
  that links *another served doc by a relative path* instead of its `resource://` URI — it skips
  `://` targets, so it does **not** validate that a `resource://` citation resolves. The
  guarantee that every `resource://kdive/docs/...` citation resolves to an allowlisted resource
  is the pytest `test_doc_resources.py::test_served_doc_resource_citations_are_all_allowlisted`,
  and it scans only served-doc **bodies** (`DOC_RESOURCES[*].source`) — **not** tool `Field`
  descriptions. So a `resource://` citation added to a tool `Field` is covered by neither guard
  unless this change adds an assertion for it (see the drift guard below).

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

- **Set completeness (both directions):** collect every `##` heading whose text has the *shape*
  of an arch token — a bare lowercase identifier `^[a-z][a-z0-9_]*$` (matches `x86_64`,
  `ppc64le`, and any future `aarch64`/`s390x`) — then assert that collected set equals
  `SUPPORTED_ARCHES`. Collecting by *shape* (not by membership in `SUPPORTED_ARCHES`) is what
  makes the guard bidirectional: a *missing* supported arch fails the equality (completeness),
  and a *spurious or misnamed* section — `## aarch64`, or a subtly wrong `## ppc64` a reader
  would take as an arch — is also collected and fails it (the negative guarantee "no unsupported
  arch heads a section"). The multi-word aux headings (`## Determine your architecture first`,
  `## What differs by architecture`, `## Same for every architecture`) do not match the
  bare-identifier shape, so they are excluded without an allowlist. Authoring constraint this
  imposes: an aux `##` heading must not be a bare lowercase identifier (e.g. `## packaging`),
  or it would be miscollected as an arch and fail; keep aux headings multi-word/Title-Case. The
  test must **blank fenced code blocks before collecting headings** (the fence-toggle the
  sibling `check-served-doc-links.sh`/`check-doc-links.sh` scripts use), so a `##`-prefixed line
  inside the doc's shell samples cannot be miscounted as a section. **Scope note:** blanking is
  for *heading extraction only* — use the blanked copy to locate `##` positions, but split and
  scan the **raw** (unblanked) section body for the content checks below (the boot-container
  verbatim and strip-required assertions), because the `strip -s` command those need lives
  inside a fenced shell sample. Blanking once and reusing that copy for the content checks would
  erase the `strip` command and make the strip-required assertion permanently un-passable.
- **Boot-container names:** assert each `BOOT_MEMBER_FORMATS[arch].container` string appears
  *verbatim* in that arch's section, so a renamed container is caught. The exact required
  substrings today are `bzImage` (x86_64) and `ppc64le ELF (vmlinux)` (ppc64le) — the doc author
  must embed those literals deliberately (the doc already does), not a natural-language alias
  like "the stripped ELF vmlinux", or the assertion fails at test time.
- **Strip-required predicate (the load-bearing rule):** for any arch whose boot format is an
  ELF kernel, assert that arch's section contains a `strip` invocation with the `-s` flag. "Is
  an ELF kernel" is read from the contract's declared `magic` via a **prefix** match — a
  `MagicPin` at offset 0 whose `hex` *starts with* `_ELF_MAGIC.hex()` (`7f454c46`), **not**
  equality: ppc64le's actual offset-0 pin is `_ELF64LE_PREFIX` = `\x7fELF\x02\x01` (hex
  `7f454c460201`), so an equality check `pin.hex == _ELF_MAGIC.hex()` would never match and the
  strip-required set would be silently empty — a dead guard shipping green. Use
  `pin.offset == 0 and pin.hex.startswith(_ELF_MAGIC.hex())`. Reading the *pin* rather than the
  human-readable `container` display string means a display-string rename cannot drop an arch
  from the set. The `-s`-flag match must tolerate the cross-compile prefix and quoting the doc
  uses (`"${CROSS_COMPILE}strip" -s …`), so match a `strip`-token line that also carries a
  standalone `-s` argument (e.g. regex `strip.*\s-s\b` within the section), **not** the bare
  literal `"strip -s"` (which the quote in `strip" -s` would defeat) and **not** the bare word
  `strip` (which appears incidentally in "the bzImage is already stripped"). Two limits, stated so a third-arch
  author fixes the right lever: (1) the offset-0 ELF pin is a *proxy* for the real condition (an
  unstripped DWARF-heavy image overruns the scan bound), exact for both current arches; (2) the
  guard reads the *declared* pins, not image bytes — ppc64le declares both the offset-0
  `\x7fELF` pin and the disambiguating EM_PPC64 pin at 0x12, so the offset-0 pin is an authoring
  convention. A future strippable ELF arch that declared only a nonzero-offset discriminator and
  omitted the offset-0 pin would lead with `\x7fELF` in bytes yet escape the check; the author's
  obligation is to declare the offset-0 pin (or add an explicit `strip_required` flag — the
  durable fix, deferred as a validation-data-model change).
- **`crashkernel` values:** assert the doc contains the exact `default_crashkernel_summary()`
  output, so a changed default fails until resynced.
- **Registration:** assert the new URI is in `DOC_RESOURCES` with `audience="all"` and the
  snapshot exists (the existing `test_doc_resources.py` may already cover generic
  registration; add only what is arch-specific).
- **Field-citation resolves (the one new tool-surface artifact):** assert the `BuildProfile.arch`
  and `runs.create` `build_profile` `Field` descriptions each contain the new
  `resource://kdive/docs/guide/kernel-build-per-arch.md` URI **and** that URI is a member of
  `DOC_RESOURCES`. Neither `served-doc-links` nor the existing citation pytest scans tool `Field`
  descriptions, so without this assertion a typo in the repointed citation ships green as an
  unfetchable dead end (the #1361/F1 class). This gives the Field citation the same
  "resolves-to-the-allowlist" guarantee served-doc bodies already have.

The guard is deliberately honest about reach: it binds set-completeness, the `crashkernel`
string, the boot-container name, and the strip-required predicate; the *explanatory* prose
(why DWARF overruns the scan bound, cross-compile triples) is review-only, not drift-guarded.

Prose-ownership rule (to bound the accepted prose-vs-prose divergence residual): the reference
carries only the scannable *key hints* (the strip-and-why one-liner, the crashkernel default,
the target-vs-host rule) and points to `external-build-upload.md` for the exhaustive rationale
and the full `tar` recipe. The narrative is the single owner of the deep detail, so an edit to
it has one home to update rather than two.

## Acceptance criteria

1. `resource://kdive/docs/guide/kernel-build-per-arch.md` is listable and readable over MCP
   (audience `all`), and its content covers exactly `x86_64` and `ppc64le`, each naming the
   correct `boot/vmlinuz` format.
2. `agent-index.md`'s build stage and the `runs.create` build-profile `arch` field both cite
   the new resource URI, and both citations are guarded to resolve to an allowlisted resource:
   the `agent-index` citation by the served-doc citation pytest (scans served-doc bodies), the
   `Field` citation by this change's new Field-citation-resolves assertion (served-doc-links does
   *not* validate either, and no existing guard scans `Field` descriptions).
3. The drift-guard pytest fails when the code and doc disagree. Red-step-verified by temporarily
   perturbing the source, then reverting: (a) adding a hypothetical arch to `_TRAITS`/
   `SUPPORTED_ARCHES` **and** `BOOT_MEMBER_FORMATS` together (a bare `_TRAITS` add is caught
   earlier by the import-time cross-table assert, not the doc test) without a doc section fails
   the completeness test; a *spurious* `## aarch64` section with no matching table row also fails
   it; (b) changing a `BOOT_MEMBER_FORMATS` container name without a doc edit fails; (c) changing
   a `crashkernel` default without a doc edit fails.
   - **Strip-guard red-step (the highest-value case, so it is called out separately):** deleting
     or altering the ppc64le `strip -s` invocation (offset-0 ELF pin unchanged) must fail the
     suite. And the guard must positively assert the strip-required set is **non-empty** for the
     current arch table — so a mis-implemented ELF predicate (e.g. the equality bug in the
     Drift-guard section) that empties the set is itself a test failure, not a silent green pass.
     Both are red-step-verified.
4. `just ci` is green, including `resources-docs-check`, `doc-constants-check`,
   `served-doc-links`, `lint`, `type`, and `test`.

## Edge cases and failure modes

- **A future third arch.** A supported arch is added by editing `_TRAITS`/`SUPPORTED_ARCHES`
  and `BOOT_MEMBER_FORMATS` *together* (a bare `_TRAITS` add fails the import-time
  `set(BOOT_MEMBER_FORMATS) == SUPPORTED_ARCHES` assert in `validation.py` first, before any
  test runs — the import-time cross-table check is the first gate). Once both tables carry the
  arch, the doc completeness test is the second gate: it fails until the arch gets a `##`
  section. The test message must name the missing arch so the fix is obvious.
- **Header-parse fragility.** The completeness test keys on `##` section headers; a non-arch
  `##` header would be miscounted if the test matched "any `##` line." Collecting by
  *bare-lowercase-identifier shape* (`^[a-z][a-z0-9_]*$`) excludes the multi-word aux headings
  and is what lets the guard reject a spurious `## aarch64` section as well as a missing one
  (see the set-completeness bullet). The residual authoring constraint — aux `##` headings must
  not be bare lowercase identifiers — is documented there. This is the main correctness risk in
  the guard and is called out for the plan.
- **Snapshot vs canonical.** The guard reads canonical `docs/`; if it read `_content/` it could
  pass on a stale snapshot. Read canonical; let `resources-docs-check` bind the snapshot.
- **`crashkernel` string coupling.** Embedding the exact `default_crashkernel_summary()` output
  couples the doc prose to the renderer's format (`"<size> on <arch>"`, arch-sorted). That is
  intentional and is the same single-source reuse `runs.install` already relies on.
- **Link direction.** The new doc links *out* to external-build-upload (already served) and is
  linked *in* from agent-index and the `arch` Field. `served-doc-links` only enforces that a
  served doc cites another served doc by its `resource://` URI (not a relative path); the doc↔doc
  citations resolving to the allowlist is the served-doc citation pytest, and the Field citation
  resolving is this change's new Field-citation assertion (see the drift guard). All three use
  `resource://` URIs, so none trips the relative-link rule.

## Rollback

Pure additive docs + one registrar row + a repointed `Field` citation + one test. Rollback is
deleting the doc, the snapshot, the registrar row, the agent-index citation, and the test,
reverting the `Field` citation to its prior external-build-upload target, and removing the
ADR-README row; no data or schema state is touched.
