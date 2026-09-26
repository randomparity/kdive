# Remote external-boot initrd admission plan

Scope: issue #2795, `WORK:SCOPE` token `q2795-9f915ebc`.
Base: `main`. Guardrails: `just lint`, `just type`, focused pytest,
`just test-changed`; `just ci` before push.
Expected implementation size: 45–110 changed lines (S) — one admission guard,
one MCP conversion, agent-facing wrapper text, and null/missing-initrd tests.

## Task 1: Refuse the unsupported root at plan admission

Verification: Mode: focused-test. `tests/services/external_boot/test_plan.py`
must first fail because the remote no-initrd case returns `root=UUID=x` instead
of raising `CategorizedError` with reason `remote_external_boot_initrd_required`;
`uv run python -m pytest tests/services/external_boot/test_plan.py -q` passes
after the guard. Existing initrd and local-device tests stay green.

Interfaces: consume existing `InvestigationBuild.canonical_document`,
`RootSpecV1.arguments`, and `direct_root_arguments(root, device)`; retain
`external_boot_root_arguments(build, root, provider_root_cmdline) -> tuple[str, ...]`.
Task 2 consumes its new `CategorizedError` reason.

Replace the conditional in `src/kdive/services/external_boot/plan.py` with:

```python
    if provider_root_cmdline is None:
        if isinstance(evidence, dict) and evidence.get("initrd") is None:
            raise CategorizedError(
                "remote external boot requires an initrd; supply an initrd with the build",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"reason": "remote_external_boot_initrd_required"},
            )
        return root.arguments
    if isinstance(evidence, dict) and evidence.get("initrd") is not None:
        return root.arguments
    return direct_root_arguments(root, provider_root_cmdline.removeprefix("root="))
```

Change the existing remote no-initrd test to assert the category, reason, and
message. Retain the local no-initrd and initrd-present test bodies as behavioral
controls; add a remote initrd-present assertion and a missing-`initrd`-key
assertion. An entirely absent evidence object still reaches existing plan
validation.
Rollback: revert this guard and its tests together.

## Task 2: Return the refusal before activation

Verification: Mode: focused-test. The focused `runs.boot` external-boot test
must first observe activation, then a
configuration-error envelope with the initrd instruction and no activation.
Run `uv run python -m pytest tests/integration/test_external_boot_job_lifecycle.py::test_boot_without_initrd_and_provider_root_refuses_before_activation -q`;
expect one pass after the conversion.
Verification: Mode: task-test-not-applicable. The `runs.boot` wrapper docstring
is agent-facing prose with no executable behavior; no test should snapshot its
wording. Read it against the new response and recovery contract.

Interfaces: consume Task 1's `CategorizedError` and existing
`_config_error(object_id, detail=..., data=...) -> ToolResponse`;
preserve `_enqueue_external_boot_locked(...) -> ToolResponse`.

In `src/kdive/mcp/tools/lifecycle/runs/steps.py`, wrap only the call:

```python
    try:
        root_arguments = external_boot_root_arguments(
            build, root, binding.runtime.platform_root_cmdline
        )
    except CategorizedError as exc:
        return _config_error(str(run.id), detail=str(exc), data=exc.details)
```

Add the remote no-initrd refusal and supply-initrd recovery sentence to the
`runs.boot` wrapper docstring in `src/kdive/mcp/tools/lifecycle/runs/registrar.py`.

In `tests/integration/test_external_boot_job_lifecycle.py`, extend
`_seed_public_external_boot` with
`initrd_state: Literal["present", "null", "missing"] = "present"`.
For `null` or `missing`, omit both `initrd_ref` from `build_result` and
`initrd` from `artifacts`; set the evidence field to null or omit it,
respectively. Both represent a no-initrd build accepted by the current plan
builder. A parameterized test seeds those forms, uses
`provider_resolver(external_boot=_PreparingProvider(), platform_root_cmdline=None)`,
calls `boot_run`, and asserts error category `configuration_error`, reason
`remote_external_boot_initrd_required`, an initrd instruction in `detail`,
and zero rows in `external_boot_activations` for the Run. The default seed
continues to cover the successful initrd path. Rollback: revert Task 2 with
Task 1.

## Assembly check

Run focused tests, `just lint`, `just type`, and `just test-changed`. Inspect the
whole diff against the frozen scope before review. No database migration or
generated artifact changes are expected.
