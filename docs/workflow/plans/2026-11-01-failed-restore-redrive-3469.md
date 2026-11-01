# Plan: re-drive a failed remote module restore (#3469)

Spec: [2026-11-01-failed-restore-redrive-3469-design.md](../specs/2026-11-01-failed-restore-redrive-3469-design.md).
Branch `feat/restore-redrive-3469`, base `main`.

Goal: a `failed-after-mutation` restore is re-driven under the retry's own deadline. Architecture:
three edits in the durable lifecycle layer of `external_boot_authority.py`; host and appliance
unchanged. Stack: Python 3, pytest with `anyio`.

Global Constraints: no schema or wire change; no new ADR (ADR-0620 amendment only); reap and
preparation completions keep their current refusal; prose avoids "critical", "robust",
"comprehensive", "elegant".

Expected implementation size: 110–170 changed lines (M) — tests ~80–120 across two files, source
~20–30 in one file, ADR amendment ~14.

## Task 1: failing tests

Files: `tests/providers/remote_libvirt/test_remote_module_authority_host.py`,
`tests/providers/remote_libvirt/test_external_boot_authority.py`.

Interfaces: consumes `_Quiesce` (`store`, `host`, `durable`, `clock`, `_restore_request()`,
`_binding()`, `reap("cleanup")`), `DurableRemoteModuleVolumePreparationHost(store, host,
monotonic=...)`, `RemoteModuleVolumePreparationStore.reopen_lifecycle_completion` and
`reopen_restored`. Defines a module helper `_raise_after_the_restore(stack, monkeypatch, times=1)`.

Verification:
- Mode: focused-test — re-drive after a raise. `test_restore_retry_after_the_host_raised_past_the_rewrite_re_drives_it`;
  red: the retry raises `CategorizedError("remote module lifecycle requires recovery")`.
- Mode: focused-test — fresh deadline. `test_restore_re_drive_runs_under_its_own_deadline`;
  red on main: `requires recovery`; red with only steps 1 and 3 of Task 2: the retry raises
  instead of returning the RESTORED response.
- Mode: focused-test — repeated failure stays retryable.
  `test_restore_re_drive_that_fails_again_stays_retryable`; red: `requires recovery` on the
  second call.
- Mode: focused-test — crash before the replace. `test_restore_re_drive_crash_before_the_replace_finishes_from_restored`;
  red: `requires recovery`.
- Mode: focused-test — reap stays latched. `test_failed_remote_reap_completion_still_requires_recovery`
  passes on main; it pins the exclusion and goes red under the Task 3 fault.

Green command: `uv run pytest -q tests/providers/remote_libvirt/test_remote_module_authority_host.py tests/providers/remote_libvirt/test_external_boot_authority.py -k "re_drive or still_requires_recovery"`.

Code (authority host tests):

```python
def _raise_after_the_restore(
    stack: _Quiesce, monkeypatch: pytest.MonkeyPatch, times: int = 1
) -> None:
    """Let the appliance rewrite the scratch volume, then fail the host call ``times`` times."""
    restore = stack.host._restore
    remaining = times

    def restore_then_raise(*args: Any) -> RemoteModuleLifecycleResponseV1:
        nonlocal remaining
        response = restore(*args)
        if remaining:
            remaining -= 1
            raise RuntimeError("remote module restore teardown is incomplete")
        return response

    monkeypatch.setattr(stack.host, "_restore", restore_then_raise)
```

Test bodies: `stack = _Quiesce(tmp_path, _SystemDomain(active=False))`; run
`stack.durable.execute(stack.request)`; install the helper; one `request = stack._restore_request()`;
the first `execute_lifecycle(request)` raises `RuntimeError` and leaves `failed-after-mutation`.
- re-drive: the retry's `result == stack.scratch`, `reopen_restored(...)` equals the response, the
  completion is `terminal`, and `(await stack.reap("cleanup")).volumes_absent`.
- deadline: both calls through `DurableRemoteModuleVolumePreparationHost(stack.store, stack.host,
  monotonic=stack.clock)`, `stack.clock.value = 1_000.0` before the retry (staged deadline 300.0).
- fails again: `times=2`; two raising calls each leave `failed-after-mutation`; the third succeeds.
- crash before the replace: on the retry, `monkeypatch.setattr(stack.store, "_replace", ...)` raising
  `SystemExit` once; the next retry returns the RESTORED response, the completion is `terminal`, and
  a counter in the helper shows `_restore` ran exactly twice.

Reap test: a `teardown` reap (`_module_lifecycle_request(preparation, purpose="teardown",
operation="teardown", action="reap")`) against a host that counts calls and raises
`RuntimeError("provider failed after mutation")`: the second call raises `requires recovery` and
`calls == 1`.

Commit: `test(remote-libvirt): pin the failed restore re-drive (#3469)`.

## Task 2: re-drive a failed restore

File: `src/kdive/providers/remote_libvirt/external_boot_authority.py`.

Interfaces: none new. Uses the existing `RemoteModuleVolumePreparationStore._read`, `_publish`,
`_replace(name, expected, data)`.

Verification:
- Mode: focused-test — Task 1's four tests, green command above, all pass.

Steps:
1. `observe_lifecycle`: raise `requires recovery` only when `completion.response is None and
   request.action != "restore"`; otherwise return `completion.response` (None for a failed
   restore).
2. `execute_lifecycle`: first line `deadline = self._monotonic() + request.budget_seconds`; stage
   with it; pass `deadline if request.action == "restore" else admitted.local_deadline` to
   `self._host.execute_lifecycle`.
3. Module constant after `RemoteModuleLifecycleCompletionV1`:
   `_FAILED_LIFECYCLE_COMPLETION = RemoteModuleLifecycleCompletionV1(state="failed-after-mutation").to_canonical_json()`.
   In `publish_lifecycle_completion`, compute `name` and `data`; when `response is not None and
   response.action == "restore" and self._read(name) == _FAILED_LIFECYCLE_COMPLETION`, call
   `self._replace(name, _FAILED_LIFECYCLE_COMPLETION, data)`; else `self._publish(name, data)`.

Commit: `fix(remote-libvirt): re-drive a restore whose host call failed (#3469)`.

## Task 3: ADR amendment, controlled faults, guardrails

The ADR-0620 amendment `## Amendment (2026-11-01): A failed module restore is re-driven on retry
(#3469)` lands with the design commit.

Controlled faults, after committing Task 2, each reverted with `git checkout -- <file>`:
- step 1 reverted → re-drive test red with `requires recovery`;
- step 1 without the `action` check (refuse never) → reap test red;
- step 2 reverted to `admitted.local_deadline` → deadline test red;
- step 3 reverted to `_publish` → re-drive test red with `conflicts with durable bytes`.

Guardrails: `just lint`, `just type`, `just test-changed`, `git fetch origin main && just records`.
