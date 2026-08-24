"""The scrub hook must not be reordered ahead of xdist's own (#2068, ADR-0578).

Under xdist the hook never runs on the controller: `DSession.pytest_collection` returns True
and the hookspec is `firstresult`, so the chain stops before ours. That is precisely why
workers keep their configuration — the controller still holds `PYTEST_ADDOPTS` when it spawns
them. Marking our implementation `tryfirst` (or as a wrapper) orders it ahead of `DSession`,
which moves the pop onto the controller and silently strips every worker's options.

Guarded rather than tested behaviourally because the observable is a worker process's option
state, which needs a second nested `-n 2` run to reach. This asserts pluggy's own runtime
metadata: `pytest_impl` is `{}` on an undecorated function and carries the flags when marked.
"""

from __future__ import annotations

import tests._addopts_scrub
import tests.conftest


def test_the_scrub_hook_is_registered_by_the_root_conftest() -> None:
    # tests/scripts/test_addopts_scrub.py imports the hook into a conftest of its own, so it
    # proves the hook works when registered — not that *this* repo registers it. Without this
    # assertion, deleting the re-export from tests/conftest.py leaves every scrub test green
    # while real runs go back to leaking PYTEST_ADDOPTS into every nested pytest (#2068).
    registered = getattr(tests.conftest, "pytest_collection", None)
    assert registered is tests._addopts_scrub.pytest_collection, (
        "tests/conftest.py no longer re-exports pytest_collection from tests/_addopts_scrub.py, "
        "so pytest does not register the scrub hook and a nested pytest inherits the run's "
        "--junit-xml path again (ADR-0578)."
    )


def test_the_scrub_hook_is_not_ordered_ahead_of_xdist() -> None:
    hook = tests._addopts_scrub.pytest_collection
    impl = getattr(hook, "pytest_impl", {})
    for flag in ("tryfirst", "wrapper", "hookwrapper"):
        assert not impl.get(flag), (
            f"tests/_addopts_scrub.py's pytest_collection is marked {flag}=True. That orders "
            "it ahead of DSession.pytest_collection, so the pop lands on the xdist controller "
            "before the workers are spawned and every worker silently loses its "
            "PYTEST_ADDOPTS options (ADR-0578)."
        )


# A third plugin preempting this hook — `pytest_collection` is `firstresult`, so the first
# implementation returning non-None ends the chain — is covered behaviourally, not here. The
# nested runs in tests/scripts/test_addopts_scrub.py fail with `'--tb=long' == 'None'` whenever
# the pop does not happen, whatever the reason, which is what preemption looks like from
# outside. A metadata check over `pytest11` entry points was tried and removed: it inspects
# module attributes, so it cannot see a class-registered hookimpl — including xdist's own
# `DSession.pytest_collection`, the very mechanism it would have been guarding against. It
# passed in a tree where the hazard was present, which is worse than no guard.
