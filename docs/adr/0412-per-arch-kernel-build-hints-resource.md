# ADR-0412: a per-architecture kernel build-hints agent resource (#1383)

- Status: Accepted
- Date: 2026-07-21

## Context

Agents driving the external build lane repeat expensive work when they miss an
architecture-specific packaging rule. The reported failure: an agent builds a kernel, tars
`lib/modules` plus a boot image, uploads, and only at `runs.complete_build` learns the
`boot/vmlinuz` member is wrong for the target arch — then rebuilds and re-uploads from
scratch. The concrete trap is the boot-image split: `x86_64` wants the compressed **bzImage**
at `boot/vmlinuz`, while `ppc64le` has no bzImage and wants the **stripped ELF `vmlinux`**.

The facts an agent needs to avoid this are already true in code and partly reachable:

- The bare boot-format split is **already inline in the tool schema**: the `runs.create`
  `build_profile.arch` `Field` (and `BuildProfile.arch`, `src/kdive/profiles/build.py`) reads
  "Selects the boot/vmlinuz payload format … (bzImage for x86_64, ELF vmlinux for ppc64le)."
  So an agent authoring the profile is not blind to the one-liner. What it does *not* get there
  is the actionable *why-and-how*: that the ppc64le member must be **stripped** first (the
  unstripped DWARF `vmlinux` overruns the validator scan bound), the cross-compile triples, the
  `crashkernel` default, and — the root of several reported cases — that the boot format follows
  the **target** arch, not the build host an agent may have misidentified.
- `docs/operating/external-build-upload.md` carries the full per-arch recipe including all of
  that — but inside a 257-line procedural narrative, so an agent scanning for "what differs for
  my arch" does not hit it in one read before building.
- `artifacts.expected_uploads` returns the machine-readable
  `contracts.kernel.layout[boot/vmlinuz].formats_by_arch`, but that is a byte contract (magic
  offsets), not a build-and-package hint.
- The arch-varying facts have single sources of truth in code:
  `domain/platform/arch_traits.py` (`_TRAITS`: machine, console, `default_crashkernel`) and
  `build_artifacts/validation.py` (`BOOT_MEMBER_FORMATS`, `SUPPORTED_ARCHES`).

So the gap is not the bare boot-format fact (the `arch` Field has it) but a **discoverable,
at-a-glance reference of the fuller per-arch build-and-package guidance** — strip rule and its
rationale, cross-compile, crashkernel, and build-host-vs-target awareness — that an agent hits
in one hop before it builds. The supported set is exactly two arches today (`x86_64`,
`ppc64le`); "all supported CPU architectures" in the issue means both, and a future third arch
must not be able to land without a corresponding hint.

This is documentation consumed *by* an external agent. It adds no LLM call, prompt, retrieval
path, classifier, or agent loop *inside kdive*, so it is not an AI surface and needs no eval
plan.

## Decision

Add one focused agent-facing resource, `docs/guide/kernel-build-per-arch.md`, served through
the existing ADR-0151 doc-resource pipeline (canonical `docs/` → `gen_doc_resources.py`
snapshot in `_content/` → `registrar.DOC_RESOURCES` entry, audience `all`, no provider gate).
It is a per-arch cheat-sheet — boot-image format and the strip requirement, the `crashkernel`
default, the cross-compile note, and a pointer to the full procedural recipe — one section per
supported arch. It **complements** `external-build-upload.md` (which stays the procedural
narrative); the reference does not restate the full tar recipe.

Discoverability is wired at the two points an agent looks: `agent-index.md`'s build stage
cites the new `resource://` URI next to the existing external-build-upload citation, and the
`runs.create` build-profile `arch` `Field` description points at it.

The code-owned facts are drift-guarded by a pytest over the canonical doc. This is the same
"guard a value restated in prose against its source" principle as ADR-0410; unlike ADR-0410's
single-capture-group `Binding` (one value per regex), it is a test because the load-bearing
guarantee here is a *set* invariant a `Binding` cannot express. The guard is honest about its
reach — it binds three code-derived facts and does **not** attempt to police the explanatory
prose:

- **Set completeness** — every arch in `SUPPORTED_ARCHES` must have a section, and no arch
  outside it may head a section. The test counts only `##` headings whose text is exactly a
  supported-arch token, so incidental prose that names another arch ("powerpc has no bzImage")
  does not pollute the set. Adding a `_TRAITS` row then fails the suite until the arch is
  documented.
- **`crashkernel` value** — reuses the existing `arch_traits.default_crashkernel_summary()`
  renderer: the doc embeds that exact string and the test asserts equality, so a changed
  default fails until the doc is resynced.
- **Boot-member fact, bound to the contract** — for each arch the section must contain
  `BOOT_MEMBER_FORMATS[arch].container` verbatim; and for any arch whose boot format is an ELF
  kernel, the section must also contain the `strip -s` command token. "Is an ELF kernel" is read
  from the contract's declared `magic` — specifically a `MagicPin` at offset 0 whose bytes are
  the ELF magic (`\x7fELF`) — not from a substring of the human-readable `container` display
  string, so renaming that display string cannot silently drop an arch from the strip-required
  set. This ties the single most load-bearing, most-error-prone hint — ppc64le has no bzImage,
  so strip the build-tree `vmlinux` first — to a source signal rather than leaving it as
  unguarded free prose. Binding to `strip -s` (not the bare word `strip`, which appears
  incidentally in "the bzImage is already stripped") pins the actionable command's presence;
  the *explanatory* text around it (why DWARF overruns the scan bound, cross-compile triples)
  stays review-only — the guard pins the rule's presence, not its full wording.

  Two limits of this signal, stated plainly so a third-arch author fixes the right thing. First,
  the offset-0 `\x7fELF` pin is a **proxy** for the real reason strip is required (an unstripped
  DWARF-heavy image overruns the tar scan bound, `validation.py`), exact for both current arches.
  Second — and this is what the guard actually keys on — the predicate reads the *declared*
  `MagicPins`, not the image's bytes. ppc64le declares both the offset-0 `\x7fELF\x02\x01` pin
  and the EM_PPC64 discriminator at 0x12 (the 0x12 pin is what actually disambiguates it from
  other ELF64-LE kernels), so the offset-0 pin is an authoring **convention**, not a structural
  necessity. A future strippable ELF arch that declared only a nonzero-offset discriminator and
  omitted the offset-0 pin would lead with `\x7fELF` in its bytes yet be classified
  not-strip-required, and the completeness guard would pass with the strip hint absent from its
  section. So the third-arch author's real obligation is to **declare the offset-0 `\x7fELF`
  pin** (or add an explicit strip-required signal). The durable fix is an explicit
  `strip_required`/ELF flag on the `FormatContract` rather than a pin-shape convention; that is
  a code change to the validation data model, deferred with the inline-`Field` derivation below,
  not taken in this doc-scoped change.

`resources-docs-check` (snapshot mirror) and `served-doc-links` (a served doc must cite another
served doc by its `resource://` URI, not a relative path) already gate the registration and the
cross-links; both run in CI. Docs, one registrar row, a `Field`-text pointer, and a guard
test. No schema change, no migration.

## Consequences

- This is a **discoverability** fix, not an enforcement one, and the ADR claims only that. An
  agent that reads the build stage of `agent-index` or the `arch` field now has a one-hop path
  to the per-arch packaging rules before it builds. It reduces the "build, upload, learn it was
  wrong, rebuild" loop by making the correct facts reachable at the moment of building; it does
  not *prevent* the loop for an agent that skips the doc — that population is exactly who hit
  the failure, and only a build-time/upload-time guard (the rejected tool-surface option below)
  would deterministically stop them. That guard is a viable follow-up on top of this doc, not a
  substitute for it.
- Adding a supported architecture is a three-place edit CI blocks until all three agree — the
  `_TRAITS`/`BOOT_MEMBER_FORMATS` rows (cross-checked by an import-time assert) and a new doc
  section (or the completeness test fails). These are separate gates cleared one failure at a
  time, not one atomic check; the net effect is that the doc cannot silently cover fewer arches
  than the platform supports.
- The per-arch boot-image fact now has **four agent-facing homes**: this reference, the
  `external-build-upload.md` narrative, and two hardcoded inline `Field` descriptions
  (`BuildProfile.arch` and the `runs.create` `build_profile` Field). This new doc's guard binds
  only *this* reference (the `crashkernel` string, the boot-container name, and the structural
  strip-required predicate); the narrative and the two `Field` strings are left as they are and
  are **not** brought under a guard by this change. That is a bounded, acknowledged residual —
  a third arch or a container rename can still silently rot those three copies — and its remedy
  is the deferred inline-`Field` derivation recorded below, not this doc. The duplication with
  the narrative is otherwise deliberate: the reference is the scannable "what differs" surface,
  the narrative the "how to run it" surface.
- The reference and `external-build-upload.md` now hold the same *explanatory* prose (the strip
  rationale, the cross-compile triples) in two human-maintained docs, and `served-doc-links`
  checks only that the cross-citation exists, not that the two agree. Prose-vs-prose divergence
  is therefore an accepted residual distinct from the code-constant rot above — a higher-
  frequency one, since it is exactly the unguarded text the guard does not bind. The mitigation
  is an ownership split, not a guard: the narrative is the single owner of the exhaustive
  rationale; the reference carries only the scannable key hints (the strip-and-why, the
  crashkernel default, the target-vs-host rule) and points to the narrative for the rest, so an
  edit to the deep detail has one home to update.
- The guard covers only the code-owned values it binds; a newly hand-copied per-arch constant
  is not auto-detected until added to the test, same asymmetry ADR-0410 accepted.

## Considered & rejected

- **Do nothing / rely on the existing narrative.** The facts are already correct in
  `external-build-upload.md`, but the reported failure is that agents do not reach them before
  building. Leaving the content buried in a long procedural doc does not create the at-a-glance
  surface the failure calls for. Rejected.
- **Augment `external-build-upload.md` only** (add a per-arch table near the top, no new
  resource). Lowest redundancy, and citability is not the obstacle — `served-doc-links` permits
  an anchored `resource://…external-build-upload.md#per-arch` citation. The real reason it does
  not deliver the same value: an MCP `read_resource` returns the *whole* document, and the
  `#fragment` is not resolved server-side, so citing an anchor into the 257-line operating doc
  still hands the agent the full procedural narrative — not the focused, scannable read a
  separate short resource gives. The two docs also serve different audiences (a `guide`
  reference vs an `operating` how-to). Rejected in favor of a separate short resource; the
  narrative keeps its recipe.
- **Expand the `arch` `Field` description in place** to carry the missing why-and-how (strip
  rule, cross-compile, crashkernel, target-vs-host), rather than stand up a new resource. This
  is the lowest-surface option and it targets exactly where the agent authoring the profile
  looks. It loses on cost, not concept: the `arch` Field description is serialized into every
  `tools/list` response, so lengthening it from a one-line pointer into a multi-paragraph guide
  taxes every catalog listing for every client on every session — and kdive deliberately keeps
  the `arch` Field to a one-line pointer for that reason. A served resource is fetched only when
  an agent asks for it. Rejected; the Field keeps its one-line pointer (repointed at the new
  reference).
- **Surface the arch hint in the tool response/schema instead of a doc** (e.g. an
  `expected_uploads` per-arch note, a `runs.create` echo, or an early upload-manifest rejection
  when the boot member contradicts the declared arch). This is the option that would
  *deterministically* prevent the wasted rebuild — the doc only makes the facts reachable. It
  is out of scope here for two reasons: a larger blast radius on the response/validation
  contract, and a deliberate scoping decision to ship the resource doc the issue asks for
  first. It is recorded as the natural follow-up, not dismissed on its merits; this ADR keeps
  the tool surface to a one-line `Field` pointer, adding no response field.
- **Hand-write the doc with no drift guard.** Simplest, but a future arch or a changed
  `crashkernel` default silently rots the doc — the precise bug ADR-0410 was created to
  prevent. Rejected.
- **Generate the whole doc from code (a new generator like `gen_tool_reference`).** A fully
  generated per-arch page would eliminate all prose drift, but the useful content here is
  explanatory prose ("powerpc has no bzImage; strip the build-tree vmlinux first"), not a
  table of constants; a generator that owns the whole file would fight the prose. Rejected in
  favor of hand-authored prose with the code-owned *values* guarded — the same split ADR-0410
  drew between generated and guarded bindings.
- **`gen_doc_constants.py` bindings instead of a pytest.** The single-capture-group `Binding`
  checks one value per pattern and cannot express "the documented arch set equals
  `SUPPORTED_ARCHES`," which is the guard that matters most for "all supported architectures."
  A pytest expresses both the set invariant and the per-value checks in one place. Rejected for
  the completeness guard; the `crashkernel` value reuses the existing summary renderer rather
  than a new binding.
- **Derive and/or guard the two hardcoded inline `Field` boot-format strings** in this same
  change. The `arch` `Field` descriptions restate "bzImage for x86_64, ELF vmlinux for ppc64le"
  as hardcoded literals (only the arch *list* is derived from `SUPPORTED_ARCHES`), so a third
  arch rots them silently — the same drift class this ADR guards for the new doc. Deriving that
  clause from `BOOT_MEMBER_FORMATS` (as `default_crashkernel_summary()` already derives the
  crashkernel text) would remove the risk at low blast radius and reach the doc-skipping
  population directly. It is **deferred, not dismissed**: it is a change to existing tool-schema
  prose, which is the tool-surface scope this issue's chosen deliverable (a resource doc)
  deliberately stayed out of. Recorded as the natural companion follow-up to the deterministic
  build-time guard above. This change does repoint the `arch` `Field`'s existing doc citation at
  the new focused reference (a one-line cross-link), but leaves the inline boot-format wording
  as-is. The same deferred contract-model work would add the explicit `strip_required`/ELF flag
  on `FormatContract` that the strip-guard predicate above notes as its durable replacement for
  the offset-0-pin convention.
