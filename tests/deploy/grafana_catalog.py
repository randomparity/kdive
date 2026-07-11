"""Static enumeration of kdive's emitted metric instruments for the dashboard coverage guard.

Instruments are created inside methods via ``meter.create_*("kdive…")`` string literals, not
module constants, so this walks the AST of the telemetry modules and collects the first
positional argument of each ``create_*`` call. The lifecycle-inventory gauges are the one
family whose name is an f-string (``f"kdive.{table}"`` in ``reconciler/fleet.py``); those four
names are expanded explicitly. Meter *scope* names and config module paths are not instruments
and are excluded by construction (they are never ``create_*`` arguments) and asserted out.
"""

from __future__ import annotations

import ast
import pathlib

from kdive.config.manifest import SETTING_MODULES
from kdive.health.metrics_text import _sanitize

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

TELEMETRY_MODULES: tuple[str, ...] = (
    "src/kdive/mcp/middleware/telemetry.py",
    "src/kdive/observability/console_telemetry.py",
    "src/kdive/observability/debug_session_telemetry.py",
    "src/kdive/services/allocation/admission/metrics.py",
    "src/kdive/reconciler/fleet.py",
    "src/kdive/reconciler/loop_telemetry.py",
    "src/kdive/jobs/worker_telemetry.py",
    "src/kdive/jobs/handlers/console/capture_telemetry.py",
)

_CREATE_ATTRS = frozenset(
    {
        "create_counter",
        "create_up_down_counter",
        "create_histogram",
        "create_observable_gauge",
        "create_observable_counter",
        "create_gauge",
    }
)

_INVENTORY_TABLES = ("allocations", "systems", "runs", "debug_sessions")

EXCLUDED_OTEL_NAMES: frozenset[str] = frozenset(
    {"kdive.mcp", "kdive.worker", "kdive.reconciler", *SETTING_MODULES}
)


def _otel_instrument_names() -> set[str]:
    names: set[str] = set()
    for rel in TELEMETRY_MODULES:
        tree = ast.parse((_REPO_ROOT / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr not in _CREATE_ATTRS:
                continue
            if not node.args:
                continue
            first = node.args[0]
            if (
                isinstance(first, ast.Constant)
                and isinstance(first.value, str)
                and first.value.startswith("kdive.")
            ):
                names.add(first.value)
    names.update(f"kdive.{table}" for table in _INVENTORY_TABLES)
    if names & EXCLUDED_OTEL_NAMES:
        msg = f"excluded non-instruments leaked into the catalog: {names & EXCLUDED_OTEL_NAMES}"
        raise AssertionError(msg)
    return names


def catalog_series() -> set[str]:
    """Return the rendered Prometheus base series names for every emitted instrument."""
    return {_sanitize(name) for name in _otel_instrument_names()}
