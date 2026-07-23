"""The autouse ``s3_backend_env`` fixture must yield to a real ambient ``KDIVE_S3_*``.

``tests/conftest.py`` seeds a dummy S3 configuration at import with ``os.environ.setdefault``,
which by construction yields to a real value in the operator's shell. The autouse fixture then
re-pins that configuration per test — and must re-pin the *resolved* value, not the dummy
constant. Re-pinning the dummy made every live_vm test that touches the object store dial
``http://minio.test:9000`` (an unresolvable placeholder host) even though the job had exported the
stack's real endpoint via scripts/live-stack/env.sh, failing with a name-resolution error.

Driven as a subprocess pytest run against a copy of the conftest so the ambient environment can be
controlled before the conftest is imported, which is impossible from inside this session.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_CONFTEST = _ROOT / "tests" / "conftest.py"

_PROBE = """
import os


def test_probe_reports_the_s3_config_the_fixture_installed():
    assert os.environ["KDIVE_S3_ENDPOINT_URL"] == "http://real-minio.example:9000"
    assert os.environ["KDIVE_S3_BUCKET"] == "kdive-real"
"""


def _run_probe(tmp_path: Path, **env_overrides: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "conftest.py").write_text(_CONFTEST.read_text())
    (tmp_path / "test_probe.py").write_text(_PROBE)
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(tmp_path), "-q", "-p", "no:cacheprovider"],
        capture_output=True,
        text=True,
        check=False,
        cwd=_ROOT,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), **env_overrides},
    )


def test_autouse_fixture_preserves_a_real_ambient_s3_endpoint(tmp_path: Path) -> None:
    result = _run_probe(
        tmp_path,
        KDIVE_S3_ENDPOINT_URL="http://real-minio.example:9000",
        KDIVE_S3_BUCKET="kdive-real",
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


def test_autouse_fixture_supplies_a_dummy_when_the_environment_is_bare(tmp_path: Path) -> None:
    """With no ambient config the fixture still supplies one — S3 is a required backend."""
    (tmp_path / "conftest.py").write_text(_CONFTEST.read_text())
    (tmp_path / "test_probe.py").write_text(
        'import os\n\n\ndef test_probe():\n    assert os.environ["KDIVE_S3_ENDPOINT_URL"]\n'
        '    assert os.environ["KDIVE_S3_BUCKET"]\n'
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(tmp_path), "-q", "-p", "no:cacheprovider"],
        capture_output=True,
        text=True,
        check=False,
        cwd=_ROOT,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
