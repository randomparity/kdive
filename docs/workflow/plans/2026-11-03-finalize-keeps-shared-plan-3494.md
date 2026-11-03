# Remote finalize keeps a shared plan pair (#3494) implementation plan

Goal: remote `finalize` removes a plan's materialization pair only when no other unfinalized
activation of the Run names the plan. Spec:
`docs/workflow/specs/2026-11-03-finalize-keeps-shared-plan-design.md`.

Architecture: `_activation_files` takes `keep_plan`; `_finalize_cleanup_quarantine` passes
`_plan_needed_by_sibling(binding, plan)`, a scan over `system_activations` under the store lock.

Tech stack: Python 3.14, pytest, ruff, ty; recipes in `justfile`.

Expected implementation size: 40–70 changed lines (M) — store code (~25), tests (~35); the
frozen M band is carried unchanged.

## Global Constraints

- Branch `fix/finalize-keeps-shared-plan-3494`. Stay out of materialize/prepare/stage paths.
- Guardrails: `just lint`, `just type`, focused pytest; the pre-push hook runs `just ci`.

## Task 1: finalize keeps the pair

Files: modify `src/kdive/providers/remote_libvirt/external_boot_authority.py`; test
`tests/providers/remote_libvirt/test_external_boot_authority.py`; ADR-0787 amendment.

Interfaces: consumes `_activation_file_groups(..., keep_plan)`, `system_activations(system_id)`,
`_finalized_name(binding, plan)`, `_read(name)`. Provides
`_plan_needed_by_sibling(binding, plan_identity) -> bool` and
`_activation_files(binding, plan_identity, *, keep_plan) -> list[str]`.

Verification:
- Contract: finalize keeps the pair until the last activation. Mode: focused-test.
  `test_remote_finalize_keeps_the_plan_pair_until_the_last_activation_finalizes`; red when
  `keep_plan=False`; green with
  `uv run python -m pytest tests/providers/remote_libvirt/test_external_boot_authority.py`
  (expect all pass).
- Contract: ADR amendment. Mode: task-test-not-applicable; prose, checked by `just records`.

Steps:
1. Add `keep_plan` to `_activation_files` and the `_plan_needed_by_sibling` scan.
2. Pass it from `_finalize_cleanup_quarantine`.
3. Add the test; update the finalize-deletes-files test, whose sibling now keeps the pair.
4. Append the ADR-0787 amendment.
