# Issue 895 Composite Failure Details Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve underlying categorized phase failure details in `runs.build_install_boot`
job failure context while still adding the composite `failed_phase` marker.

**Architecture:** Keep the existing composite wrapper. Build the wrapper details from the
categorized cause details when available, then overwrite/add `failed_phase` with the
actual failed phase.

**Tech Stack:** Python 3.14, pytest, existing `CategorizedError` and worker failure context.

---

### Task 1: Preserve Categorized Cause Details

**Files:**
- Modify: `tests/jobs/handlers/runs/test_composite.py`
- Modify: `src/kdive/jobs/handlers/runs/composite.py`

- [ ] **Step 1: Write the failing regression test**

Add imports:

```python
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.jobs import worker
from kdive.security.secrets.secret_registry import SecretRegistry
```

Add this test after `test_failed_phase_is_in_failure_context`:

```python
def test_categorized_phase_details_survive_failure_context() -> None:
    """CompositePhaseError preserves safe structured details from the failed phase."""
    cause = CategorizedError(
        "kdump fragment symbols were dropped",
        category=ErrorCategory.BUILD_FAILURE,
        details={"dropped": "CONFIG_CRASH_DUMP", "failed_phase": "wrong"},
    )

    error = composite.CompositePhaseError("build", cause)

    assert error.category == ErrorCategory.BUILD_FAILURE
    assert error.details == {
        "dropped": "CONFIG_CRASH_DUMP",
        "failed_phase": "build",
    }
    assert worker._failure_context(error, SecretRegistry()) == {
        "failure_message": "build phase failed: kdump fragment symbols were dropped",
        "failure_detail_dropped": "CONFIG_CRASH_DUMP",
        "failure_detail_failed_phase": "build",
    }
```

- [ ] **Step 2: Run the focused test to verify it fails**

Run:

```bash
uv run python -m pytest tests/jobs/handlers/runs/test_composite.py::test_categorized_phase_details_survive_failure_context -q
```

Expected: fail because `error.details` and the worker failure context do not include
`dropped`.

- [ ] **Step 3: Implement the minimal wrapper detail merge**

Change `CompositePhaseError.__init__` in
`src/kdive/jobs/handlers/runs/composite.py`:

```python
        details = dict(cause.details) if isinstance(cause, CategorizedError) else {}
        details["failed_phase"] = failed_phase
        super().__init__(
            f"{failed_phase} phase failed: {cause}",
            category=category,
            details=details,
        )
```

- [ ] **Step 4: Run focused tests**

Run:

```bash
uv run python -m pytest tests/jobs/handlers/runs/test_composite.py -q
```

Expected: all tests in the file pass.

- [ ] **Step 5: Run relevant lint/type checks**

Run:

```bash
just lint
just type
```

Expected: both commands exit 0 with no warnings.
