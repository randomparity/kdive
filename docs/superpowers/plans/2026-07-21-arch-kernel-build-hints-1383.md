# Implementation plan — per-arch kernel build hints (#1383)

- Spec: [`docs/specs/2026-07-21-arch-kernel-build-hints-1383-design.md`](../../specs/2026-07-21-arch-kernel-build-hints-1383-design.md)
- ADR: [`ADR-0412`](../../adr/0412-per-arch-kernel-build-hints-resource.md)
- Branch: `feat/arch-kernel-build-hints-1383` — Base: `main`
- Guardrails: `just lint`, `just type`, `just test`, `just ci` (full PR gate). Doc-resource
  drift regen: `just resources-docs` (write) / `just resources-docs-check` (verify).

## Status of prerequisites (already done in the design commits)

Already committed in the design phase on this branch: the spec, `ADR-0412`, and its
`docs/adr/README.md` index row (so Task-nothing creates them — the rollback's "remove the
ADR-README row / delete the ADR+spec" steps undo design-phase artifacts, listed here for
traceability). Also authored:

The canonical doc `docs/guide/kernel-build-per-arch.md` is **already authored** on this branch
(design phase), with the arch sections, the exact `default_crashkernel_summary()` string
(`512M on ppc64le, 256M on x86_64`), the verbatim container substrings (`bzImage`,
`ppc64le ELF (vmlinux)`), a `"${CROSS_COMPILE}strip" -s` invocation in the ppc64le section, and
the "determine your architecture first" (build-host vs target) section. The remaining work is:
the drift-guard test, resource registration + snapshot, the agent-index cross-link, and the
`arch` Field citation repoint. Do **not** re-author the doc; adjust it only if a task below
requires it (e.g. to satisfy the guard's exact-token expectations, which it already meets).

## Task 1 — Write the drift-guard test (RED first)

**What / where.** Add `tests/mcp/resources/test_kernel_build_per_arch_doc.py`. It reads the
**canonical** doc `docs/guide/kernel-build-per-arch.md` (not the `_content/` snapshot;
`resources-docs-check` binds the snapshot to canonical). Import ground truth from source:
`SUPPORTED_ARCHES` and `default_crashkernel_summary` from
`kdive.domain.platform.arch_traits`, and `BOOT_MEMBER_FORMATS`, `_ELF_MAGIC` from
`kdive.build_artifacts.validation`; `DOC_RESOURCES` from `kdive.mcp.resources.registrar`.

Assertions (each independently failing-informative):

1. **Set completeness (bidirectional, shape-based).** Blank fenced code blocks (```` ``` ````
   toggle) to locate `##` headings, collect heading texts matching the bare-lowercase-identifier
   shape `^[a-z][a-z0-9_]*$`, and assert that set `==` `SUPPORTED_ARCHES`. A missing arch and a
   spurious/misnamed `## aarch64` both fail.
2. **Boot-container verbatim.** For each arch, assert `BOOT_MEMBER_FORMATS[arch].container`
   appears in that arch's **raw** (unblanked) section body. (Split the raw doc on the located
   heading positions; do not scan the blanked copy — the container/strip content lives in prose
   and fences.)
3. **Strip-required predicate.** Compute the ELF-kernel arch set structurally:
   `arch` is ELF-required iff any declared `MagicPin` has `offset == 0` and
   `pin.hex.startswith(_ELF_MAGIC.hex())` (prefix, **not** equality — ppc64le's pin is
   `7f454c460201`). Assert this set is **non-empty** (a mis-implemented predicate that empties
   it is itself a failure). For each ELF-required arch, assert its raw section body contains a
   `strip` invocation carrying a standalone `-s` flag (regex tolerant of the
   `"${CROSS_COMPILE}strip" -s` form, e.g. `strip.*\s-s\b`; not the bare literal `strip -s`,
   not the bare word `strip`).
4. **crashkernel summary.** Assert the raw doc contains `default_crashkernel_summary()` verbatim.
5. **Registration.** Assert `resource://kdive/docs/guide/kernel-build-per-arch.md` is in
   `DOC_RESOURCES` with `audience == "all"`, `required_kind is None`, and its `_content`
   snapshot file exists. (If `test_doc_resources.py` already covers generic registration
   invariants, keep this minimal — assert only the arch-doc-specific entry.)
6. **Field-citation resolves.** Assert both build-profile `arch` Field descriptions
   (`BuildProfile.arch` in `src/kdive/profiles/build.py`, and the `runs.create` `build_profile`
   Field in `src/kdive/mcp/tools/lifecycle/runs/registrar.py`) contain the new
   `resource://kdive/docs/guide/kernel-build-per-arch.md` URI, and that URI is a member of
   `DOC_RESOURCES`. Read the description strings via the imported `BuildProfile` model field and
   the registrar (or, if the registrar Field is not readily importable, assert against the
   `list_tools()` schema for `runs.create`). Prefer the model/field object over re-reading source.

**Acceptance.** Run the test; it must be **RED** on the registration, snapshot, and
Field-citation assertions (the doc content assertions may already pass, since the doc exists).
Record which assertions are red before Task 2.

**Conventions.** ty strict; ruff `E,F,I,UP,B,SIM`, line length 100. Tests mirror the package
tree under `tests/`. No new dependency.

## Task 2 — Register the doc + generate the snapshot (GREEN for registration)

**What / where.** Add a `DocResource` entry to `DOC_RESOURCES` in
`src/kdive/mcp/resources/registrar.py`:
`uri="resource://kdive/docs/guide/kernel-build-per-arch.md"`,
`source="docs/guide/kernel-build-per-arch.md"`, `content_file="kernel-build-per-arch.md"`,
`name="kernel-build-per-arch"`, a title and description naming the per-arch boot-image split
(bzImage vs stripped ELF vmlinux) and the build-host-vs-target hint, `audience="all"`,
`required_kind=None`. Match the surrounding entries' style.

Then run `just resources-docs` to write `src/kdive/mcp/resources/_content/kernel-build-per-arch.md`.

**Acceptance.** `just resources-docs-check` passes; the registration + snapshot assertions in
Task 1's test go green; `served-doc-links` passes (the doc cites external-build-upload by its
`resource://` URI, already served). The served-doc citation pytest
(`test_served_doc_resource_citations_are_all_allowlisted`) passes (the doc's outbound
`resource://` citations are allowlisted).

## Task 3 — Cross-link from agent-index (build stage)

**What / where.** In `docs/guide/agent-index.md`, in the build stage (the step that already
cites `resource://kdive/docs/operating/external-build-upload.md`), add a citation to
`resource://kdive/docs/guide/kernel-build-per-arch.md` as the per-arch packaging reference. Keep
the edit minimal and **do not touch** the `~NNN tools` doc-constant line (guarded by
`doc-constants-check`). Then re-run `just resources-docs` to re-mirror the agent-index snapshot.

**Acceptance.** `just resources-docs-check`, `served-doc-links`, and `doc-constants-check` all
pass; the agent-index snapshot reflects the new citation.

## Task 4 — Repoint the arch Field citations to the new doc

**What / where.** In `src/kdive/profiles/build.py` (`BuildProfile.arch` Field) and
`src/kdive/mcp/tools/lifecycle/runs/registrar.py` (the `runs.create` `build_profile` Field),
repoint the existing `resource://kdive/docs/operating/external-build-upload.md` citation to
`resource://kdive/docs/guide/kernel-build-per-arch.md` (the new doc chains onward to
external-build-upload, so no reachability is lost). Do **not** reword the inline boot-format
clause ("bzImage for x86_64, ELF vmlinux for ppc64le") — deriving that from `BOOT_MEMBER_FORMATS`
is the deferred follow-up recorded in ADR-0412.

**Guard note.** First check for an existing test that pins the `arch` Field to the
external-build-upload citation; per the spec's ground-truth check, the only external-build-upload
assertions in the suite are on validation error-message strings, not the Field, so the repoint
should break nothing — but re-run the full suite to confirm. Task 1's Field-citation assertion
now goes green.

**Acceptance.** Task 1's Field-citation assertion is green; `just test` passes (no test pinned
the old Field citation).

## Task 5 — Full guardrail sweep + commits

**What.** Run `just ci` (full PR gate: lint, type, lint-shell, lint-workflows, check-mermaid,
docs-links, docs-paths, served-doc-links, adr-status-check, docs-check, config-docs-check,
resources-docs-check, doc-constants-check, test, …). Fix any failure. Before the red-step
verification, confirm the guard's **green baseline** passes (catches the fence/strip interaction
at plan time, not debug time).

**Red-step verification (acceptance criterion 3 of the spec).** Temporarily, then revert each:
(a) delete the ppc64le `strip -s` line → strip-required assertion fails; (b) rename a
`BOOT_MEMBER_FORMATS` container without editing the doc → boot-container assertion fails;
(c) change a `crashkernel` default without editing the doc → crashkernel assertion fails;
(d) add a spurious `## aarch64` section → completeness assertion fails;
(e) add a hypothetical arch to `_TRAITS` (a **full** `ArchTraits` — `machine`, `console_device`,
`pin_nic_slot`, `kvm_cpu_mode`, `emit_acpi_features`, `default_crashkernel`, not just a stub) so
`SUPPORTED_ARCHES` grows, **and** a matching `BOOT_MEMBER_FORMATS` row (a `MagicPin` +
`container`) so the import-time cross-table assert passes, with **no** doc section → the doc
*completeness* test fails naming the missing arch. Note the expected signature: because a new
`_TRAITS` row also changes `default_crashkernel_summary()`, the crashkernel-summary assertion (4)
goes RED too — both REDs are expected and correct. This is the "a future arch cannot land without
a hint" direction and must be demonstrated, not just reasoned about;
(f) change the ELF predicate from `startswith(_ELF_MAGIC.hex())` to `== _ELF_MAGIC.hex()` → the
non-empty-strip-set assertion fails (the dead-guard bug is itself caught).
Each must produce a clean RED (not an import error, except (e)'s import-assert gate which must be
cleared first). Revert all perturbations; confirm green.

**Commits.** One logical change per commit (test; registrar+snapshot; agent-index; Field
repoint), imperative subject ≤72 chars, `Co-Authored-By` trailer. Stage explicit paths.

## Rollback

Additive except the Field repoint. Simplest rollback is to revert the whole branch/PR. If
undoing piecemeal, delete **together** so `adr-status-check` stays green (its invariant: exactly
one README index row per ADR file — removing the row while leaving the ADR file, or vice versa,
fails CI): the doc, its `_content` snapshot, the registrar row, the agent-index citation, the
test, the `docs/adr/0412-per-arch-kernel-build-hints-resource.md` ADR file, the spec, **and** the
ADR-README index row; and revert
the two `arch` Field citations to their prior external-build-upload target. No data or schema
state is touched.
