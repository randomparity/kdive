# INITRD component-source implementation plan

Goal: make INITRD a real, provider-validated provisioning input, use a local file for the
local-libvirt baseline boot, and reject it early for remote-libvirt. The architecture adds one
optional profile field, one admission check, and one local materialization override governed by
ADR-0583. Python 3.14, Pydantic, pytest, and the existing provider runtime remain the stack.

## Global constraints

- Targets are x86_64 and ppc64le; the x86_64 host is included in that set.
- Accept only the existing `local` component source for local-libvirt and fault-inject; accept no
  INITRD source for remote-libvirt. Add no dependency or migration.
- Preserve the existing component-validation error shape and remote `dracut`/GRUB behavior.
- Guardrails are `just lint`, `just type`, focused pytest selections, and `just ci` before push.

## Task 1 — Profile entry point and provider admission

Files: `src/kdive/profiles/provisioning.py`, `src/kdive/services/systems/validation.py`, the three
provider `composition.py` files, `tests/profiles/test_provisioning.py`,
`tests/services/systems/test_system_validation.py`, and
`tests/providers/test_capability_parity.py`.

Interfaces: add `ProvisioningProfile.initrd: ComponentRef | None = Field(default=None,
description=...)`; the description names the discriminated reference and provider acceptance
matrix. Consume it in
`validate_profile_for_provider` by calling
`reject_unsupported_component_source(capabilities, component_kind=INITRD_COMPONENT,
ref=profile.initrd)` when present. Provider capability maps expose INITRD `{"local"}` for local
and fault-inject and an empty set for remote.

1. Add tests that parse and round-trip each discriminated reference, assert generated-schema text,
   reject remote `local` with exact details, accept local/fault `local`, detect INITRD as enforced,
   and require the exact provider-difference waiver rationale. Run
   `uv run python -m pytest tests/profiles/test_provisioning.py tests/services/systems/test_system_validation.py tests/providers/test_capability_parity.py -q` and expect the new assertions to fail.
2. Add the optional field, admission call, declarations, and the remote in-guest dracut/root-device
   rationale beside its empty INITRD declaration, plus the matching cross-provider waiver. Do not
   exempt INITRD from the declared-kind AST enforcement guard. Run
   the same command and expect all selected tests to pass.
3. Run `just lint` and `just type`; expect exit 0. Commit the task as one contract change.

Acceptance: profiles without INITRD are unchanged; every supplied kind reaches provider admission;
remote fails before provider I/O with the existing details shape; schema text exposes the provider
matrix; the cross-provider difference is waived explicitly and the parity AST guard passes without
an enforcement waiver.

## Task 2 — Local baseline materialization

Files: `src/kdive/providers/local_libvirt/lifecycle/provisioning.py`,
`src/kdive/providers/local_libvirt/lifecycle/rootfs/baseline_kernel.py`,
`tests/providers/local_libvirt/test_provisioning.py`, and
`tests/providers/local_libvirt/lifecycle/test_baseline_kernel.py`.

Interfaces: preserve the shared three-argument `ExtractBaselineKernel` contract used by
`rootfs_build.py`. Add a separate `ExtractBaselineKernelWithInitrd` Protocol with
`__call__(base: Path, dest: Path, hint: str | None, *, initrd_override: LocalComponentRef,
allowed_roots: list[Path]) -> BaselineKernel` and inject it into `LocalLibvirtProvisioning`.
Both real extractors delegate to one private extraction routine; the override variant resolves the
path through existing `validate_local_component_path`, copies it onto `<baseline>.part/initrd`
after guest extraction, and only then performs the existing rename. `_prepare_baseline_kernel`
selects the override seam only for a fresh baseline with a supplied ref; its existing
visible-baseline branch performs no resolution or copy. The returned `BaselineKernel.initrd`
points at `<baseline>/initrd` when either the rootfs or override supplies it. The rootfs-build
consumer and its injected three-argument test doubles remain unchanged.

1. Add tests proving supplied bytes replace the extracted initrd before rename, absence preserves
   extraction, retry reuses the visible file, invalid path/checksum failures precede domain define,
   and a controlled copy/validation fault leaves no visible baseline. Also retain a focused
   `test_rootfs_build.py` case proving the existing three-argument consumer is unchanged. Run `uv run python -m pytest
   tests/providers/local_libvirt/test_provisioning.py
   tests/providers/local_libvirt/lifecycle/test_baseline_kernel.py
   tests/providers/local_libvirt/test_rootfs_build.py -q` and expect the new
   assertions to fail.
2. Implement the minimum override using existing local-component path/checksum helpers. Run the
   same command and expect it to pass.
3. Run the Task 1 focused command, `just lint`, and `just type`; expect exit 0. Commit separately.

Acceptance: local direct-kernel XML uses the supplied stable initrd path, retries do not replace a
materialized baseline, and cleanup remains owned by the existing baseline failure path.

## Task 3 — Durable rejection record and branch proof

Files: `docs/adr/0583-provider-aware-initrd-component-input.md`,
`docs/workflow/specs/2026-08-27-initrd-component-source-design.md`,
`docs/workflow/plans/2026-08-27-initrd-component-source.md`, plus epic #1423's Non-goals through
the quest-authorized GitHub edit after code proof. Issue #1428 is updated only if its live waiver
text still describes INITRD as undecided.

1. Run `just adr-status-check`, relevant docs checks, and focused tests; expect exit 0.
2. Before external edits, save the exact `body` fields for #1423 and #1428 to distinct run-unique,
   private temporary files and read them back byte-for-byte. Compose each edited body in a separate
   file, run `just check-pr-body` on it, publish with `gh issue edit --body-file`, and verify the
   returned JSON body matches apart from at most one trailing newline. Keep the originals until the
   implementation commits and both readbacks are verified. If publication must be reversed,
   restore each captured original with `--body-file` and repeat the JSON readback.
3. Run `just ci` bare; expect exit 0. Update only the tracker bodies whose live text still needs the
   recorded rejected outcome.
4. Re-read the complete diff, run pre-commit over explicitly staged paths, and commit any doc-only
   normalization separately.

Acceptance: future readers find the remote rejection in code, ADR-0583, and epic #1423; all local
guardrails pass. Rollback is `git revert` of the implementation commits plus restoration of the
tracker text; no persisted-data cleanup is required.

## Durable workflow context

Branch `feat/initrd-component-source-1436`; `BASE_BRANCH=main`; scope token
`q1436-956f81c1`; guardrails `just lint`, `just type`, focused pytest, and `just ci`.
