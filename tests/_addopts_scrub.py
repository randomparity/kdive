"""Stop a pytest process handing ``PYTEST_ADDOPTS`` to anything it spawns (#2068, ADR-0578).

CI sets it for the whole ``Test`` step to ask for a JUnit report, so a nested pytest would
inherit ``--junit-xml=<shared path>`` and overwrite the run's own report. When the outer
session never reaches ``sessionfinish`` — a cancelled job, an OOM-killed controller, a step
timeout — the summary step reads that leftover and reports a clean run.

This lives beside ``conftest.py`` rather than inside it so the scrub's own tests can import the
shipped hook without importing ``tests.conftest``, which pulls in the whole ``kdive`` package
(1.45s per nested run against a 0.03s baseline, and it would couple those tests to
product-package import health).
"""

from __future__ import annotations

import os

import pytest


def pytest_collection(session: pytest.Session) -> None:
    """Pop ``PYTEST_ADDOPTS`` before this process imports any test module.

    The timing is the decision. This has to run after the process has configured itself from
    the variable and before it imports any test module, and ``pytest_collection`` is the only
    hook that is both. Under xdist it never runs on the controller at all —
    ``DSession.pytest_collection`` returns ``True`` and the hookspec is ``firstresult``, so the
    chain stops before this — which is exactly why workers keep their configuration: the
    controller still holds the variable when it spawns them, and each worker then pops it
    itself. Serially there is no ``DSession``, so this runs normally.

    **Do not mark this ``tryfirst``, ``wrapper``, or ``hookwrapper``.** Any of those orders it
    ahead of ``DSession``, moving the pop onto the controller before the workers are spawned
    and silently stripping every worker's options
    (``tests/guards/test_collection_hook_ordering.py``).

    The ``None`` default on the pop is load-bearing: the variable is unset on every local run,
    and a bare ``pop`` would raise ``KeyError`` and break ``just test`` for everyone.

    Returns ``None`` so the default collection still runs.
    """
    os.environ.pop("PYTEST_ADDOPTS", None)
    return None
