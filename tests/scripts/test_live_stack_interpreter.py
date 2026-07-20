"""lib.sh resolves its interpreter from KDIVE_PYTHON when set (#1293, ADR-0389).

The self-hosted live_vm job runs the stack's worker under /opt/kdive's libguestfs venv via
KDIVE_PYTHON; lib.sh must honor it, else the worker's guestfs import fails. The default
(KDIVE_PYTHON unset) must stay the workspace .venv so operator use is unchanged.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_LIB = Path(__file__).resolve().parents[2] / "scripts" / "live-stack" / "lib.sh"


def _resolved_py(env_kdive_python: str | None) -> str:
    prelude = f'export KDIVE_PYTHON="{env_kdive_python}"\n' if env_kdive_python is not None else ""
    script = f'{prelude}source "{_LIB}"\nprintf "%s" "$py"\n'
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True)
    return out.stdout


def test_py_honors_kdive_python_when_set() -> None:
    assert _resolved_py("/opt/kdive/.venv/bin/python") == "/opt/kdive/.venv/bin/python"


def test_py_defaults_to_workspace_venv_when_unset() -> None:
    assert _resolved_py(None).endswith("/.venv/bin/python")
    assert "/opt/kdive/" not in _resolved_py(None)
