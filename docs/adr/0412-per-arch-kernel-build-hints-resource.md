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

The facts an agent needs to avoid this are already true in code and already reachable:

- `docs/operating/external-build-upload.md` carries a correct per-arch recipe — but inside a
  257-line procedural narrative, so an agent scanning for "what differs for my arch" does not
  hit it before building.
- `artifacts.expected_uploads` returns the machine-readable
  `contracts.kernel.layout[boot/vmlinuz].formats_by_arch`, but that is a byte contract (magic
  offsets), not a build-and-package hint.
- The arch-varying facts have single sources of truth in code:
  `domain/platform/arch_traits.py` (`_TRAITS`: machine, console, `default_crashkernel`) and
  `build_artifacts/validation.py` (`BOOT_MEMBER_FORMATS`, `SUPPORTED_ARCHES`).

So the gap is a **discoverable, at-a-glance, per-arch reference** an agent hits *before* it
builds — not missing facts. The supported set is exactly two arches today (`x86_64`,
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
  `BOOT_MEMBER_FORMATS[arch].container` verbatim; and for any arch whose container names an
  ELF (the no-bzImage arches — the predicate is `"ELF" in container`, derived from the
  contract, not a hardcoded arch list), the section must also contain a `strip` instruction.
  This ties the single most load-bearing, most-error-prone hint — ppc64le has no bzImage, so
  strip the build-tree `vmlinux` first — to a source-derived predicate, rather than leaving it
  as unguarded free prose. The *explanatory* text around it (why DWARF overruns the scan bound,
  cross-compile triples) stays review-only; the guard pins the actionable rule's presence, not
  its wording.

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
- There are now two agent-facing homes for the per-arch boot-image fact (this reference and
  the procedural narrative). This duplication is deliberate and bounded — the reference is the
  scannable "what differs" surface, the narrative the "how to run it" surface. The shared
  code-derived facts (`crashkernel` string, boot-container name, and the strip-required
  predicate) are guarded against divergence in *this* doc; the same facts in the narrative are
  not bound by this test, and all explanatory prose in either doc remains a review
  responsibility.
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
