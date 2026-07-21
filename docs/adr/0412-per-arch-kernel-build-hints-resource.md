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

The code-owned facts are drift-guarded so a future arch (or a changed default) cannot leave the
doc stale — the exact failure class ADR-0410 addresses:

- **Set completeness** is enforced by a pytest over the canonical doc: every arch in
  `SUPPORTED_ARCHES` must have a section, and no arch outside it may be presented as a
  supported build target. Adding a `_TRAITS` row then fails the suite until the arch is
  documented. This is a *set* invariant the single-capture-group `Binding` regex of
  `gen_doc_constants.py` cannot express, so it lives as a test, not a binding.
- **Value correctness** for the per-arch `crashkernel` defaults reuses the existing
  `arch_traits.default_crashkernel_summary()` renderer: the doc embeds that exact string and
  the same pytest asserts equality, so a changed default fails until the doc is resynced. The
  per-arch boot-container names (`BOOT_MEMBER_FORMATS[arch].container`) are asserted present
  the same way.

`resources-docs-check` (snapshot mirror) and `served-doc-links` (every `resource://` citation
in a served doc resolves to an allowlisted resource) already gate the registration and the
cross-links; both run in CI. Docs, one registrar row, a `Field`-text pointer, and a guard
test. No schema change, no migration.

## Consequences

- An agent that reads the build stage of `agent-index` or the `arch` field now has a one-hop
  path to the per-arch packaging rules before it builds, so the "build, upload, learn it was
  wrong, rebuild" loop for the boot-image split is closed on the discoverability side.
- Adding a supported architecture is now a three-place edit that CI enforces atomically: the
  `_TRAITS`/`BOOT_MEMBER_FORMATS` rows (already cross-checked at import), and a new doc
  section (or the completeness test fails). The doc cannot silently cover fewer arches than
  the platform supports.
- There are now two agent-facing homes for the per-arch boot-image fact (this reference and
  the procedural narrative). This duplication is deliberate and bounded — the reference is the
  scannable "what differs" surface, the narrative the "how to run it" surface — and the shared
  code-owned values (`crashkernel`, boot-container names) are guarded against divergence. Free
  prose in either doc is not guarded and remains a review responsibility.
- The guard covers only the code-owned values it binds; a newly hand-copied per-arch constant
  is not auto-detected until added to the test, same asymmetry ADR-0410 accepted.

## Considered & rejected

- **Do nothing / rely on the existing narrative.** The facts are already correct in
  `external-build-upload.md`, but the reported failure is that agents do not reach them before
  building. Leaving the content buried in a long procedural doc does not create the at-a-glance
  surface the failure calls for. Rejected.
- **Augment `external-build-upload.md` only** (add a per-arch table near the top, no new
  resource). Lowest redundancy, but keeps the hint inside the same long doc an agent already
  skims past, and gives `agent-index`/the tool schema nothing new and short to cite. Rejected
  in favor of a separate, citable, scannable resource; the narrative keeps its recipe.
- **Surface the arch hint in the tool response/schema instead of a doc** (e.g. an
  `expected_uploads` per-arch note or a `runs.create` echo). A stronger fix for the failure,
  but a larger blast radius on the response contract, and the issue asks specifically for a
  resource doc. Kept minimal: only a `Field`-text pointer to the doc, not a new response
  field. Rejected as the primary mechanism; folded in as the one-line citation.
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
