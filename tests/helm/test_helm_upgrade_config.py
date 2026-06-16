"""Render assertions for chart upgrade correctness (ADR-0134, #469/#470).

Two upgrade footguns the chart must close:

- #470: a ``config.*`` change must roll the pods that read it via ``envFrom``. A
  ``checksum/config`` pod annotation makes the pod template vary with the rendered
  ConfigMap, so ``helm upgrade`` rolls exactly the three app Deployments — and never
  postgres/minio (which do not consume the ConfigMap, so their demo data is preserved).
- #469: ``helm upgrade --reuse-values`` drops new chart-default config keys. The chart
  renders ``KDIVE_LOCAL_LIBVIRT_ENABLED`` from a defensive ``default "false"`` so a reused
  value-set missing the key still renders it (no reaper crash-loop after upgrade).

These shell out to a real ``helm`` binary like the rest of ``tests/helm``; they skip when
helm is absent, so CI must provide the binary for the gate to mean anything.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")

CHART = str(Path(__file__).resolve().parents[2] / "deploy" / "helm" / "kdive")

# The three app processes whose pods read config.* via envFrom (and must roll on a change).
_APP_PROCS = ("server", "worker", "reconciler")
# The bundled-demo backends that do NOT consume the config ConfigMap (must NOT roll).
_BACKEND_PROCS = ("postgres", "minio", "oidc")


def _template(*set_args: str) -> subprocess.CompletedProcess[str]:
    args = ["helm", "template", "kdive", CHART]
    for s in set_args:
        args += ["--set", s]
    return subprocess.run(args, capture_output=True, text=True, check=False)


def _deployments(*set_args: str) -> dict[str, dict[str, Any]]:
    """Render and index every Deployment by its process-name suffix."""
    res = _template(*set_args)
    assert res.returncode == 0, res.stderr
    out: dict[str, dict[str, Any]] = {}
    for doc in yaml.safe_load_all(res.stdout):
        if not (isinstance(doc, dict) and doc.get("kind") == "Deployment"):
            continue
        name = str(doc["metadata"]["name"])
        suffix = name.rsplit("-", 1)[-1]
        out[suffix] = doc
    return out


def _pod_annotations(deploy: dict[str, Any]) -> dict[str, str]:
    return deploy["spec"]["template"]["metadata"].get("annotations", {}) or {}


def _config_value(res: subprocess.CompletedProcess[str], key: str) -> str | None:
    """Return the value of ``key`` in the rendered ``-config`` ConfigMap, or None."""
    for doc in yaml.safe_load_all(res.stdout):
        if not (isinstance(doc, dict) and doc.get("kind") == "ConfigMap"):
            continue
        if not str(doc.get("metadata", {}).get("name", "")).endswith("-config"):
            continue
        return doc.get("data", {}).get(key)
    return None


# --- #470: config-checksum pod annotation --------------------------------------------


@pytest.mark.parametrize("proc", _APP_PROCS)
def test_app_pods_carry_config_checksum_annotation(proc: str) -> None:
    deploy = _deployments("config.KDIVE_DATABASE_URL=postgresql://x/y")[proc]
    annotations = _pod_annotations(deploy)
    checksum = annotations.get("checksum/config")
    assert checksum, f"{proc} pod template has no checksum/config annotation"
    # sha256sum renders a 64-hex-char digest (helm appends a trailing "  -" filename field
    # that the | sha256sum pipe strips via the function's first-field output).
    assert len(checksum) == 64, checksum


def test_config_checksum_changes_when_a_config_value_changes() -> None:
    a = _deployments("config.KDIVE_DATABASE_URL=postgresql://x/y")
    b = _deployments(
        "config.KDIVE_DATABASE_URL=postgresql://x/y", "config.KDIVE_S3_BUCKET=other-bucket"
    )
    for proc in _APP_PROCS:
        ca = _pod_annotations(a[proc])["checksum/config"]
        cb = _pod_annotations(b[proc])["checksum/config"]
        assert ca != cb, f"{proc} checksum did not change on a config.* change"


def test_config_checksum_is_stable_across_renders() -> None:
    # Same inputs must hash the same, or every upgrade would needlessly roll the pods.
    a = _deployments("config.KDIVE_DATABASE_URL=postgresql://x/y")
    b = _deployments("config.KDIVE_DATABASE_URL=postgresql://x/y")
    for proc in _APP_PROCS:
        assert (
            _pod_annotations(a[proc])["checksum/config"]
            == _pod_annotations(b[proc])["checksum/config"]
        )


def test_backend_pods_have_no_config_checksum_annotation() -> None:
    # postgres/minio/oidc do not consume the config ConfigMap; a checksum on them would roll
    # the emptyDir demo backends on a config change and wipe demo data (#470 acceptance).
    deploys = _deployments("bundledBackends=true", "demoAcknowledged=true")
    for proc in _BACKEND_PROCS:
        assert proc in deploys, proc
        assert "checksum/config" not in _pod_annotations(deploys[proc]), proc


# --- #469: defensive KDIVE_LOCAL_LIBVIRT_ENABLED default ------------------------------


def test_local_libvirt_defaults_false_when_value_absent_external() -> None:
    # A bare --reuse-values upgrade can omit the key entirely; null clears it from the
    # merged value-set, modelling that drop. The rendered ConfigMap must still carry "false".
    res = _template(
        "config.KDIVE_DATABASE_URL=postgresql://x/y", "config.KDIVE_LOCAL_LIBVIRT_ENABLED=null"
    )
    assert res.returncode == 0, res.stderr
    assert _config_value(res, "KDIVE_LOCAL_LIBVIRT_ENABLED") == "false"


def test_local_libvirt_defaults_false_when_value_absent_bundled() -> None:
    res = _template(
        "bundledBackends=true",
        "demoAcknowledged=true",
        "config.KDIVE_LOCAL_LIBVIRT_ENABLED=null",
    )
    assert res.returncode == 0, res.stderr
    assert _config_value(res, "KDIVE_LOCAL_LIBVIRT_ENABLED") == "false"


def test_local_libvirt_honors_explicit_true() -> None:
    # A host that genuinely runs libvirtd opts back in; the defensive default must not clobber it.
    res = _template(
        "config.KDIVE_DATABASE_URL=postgresql://x/y", "config.KDIVE_LOCAL_LIBVIRT_ENABLED=true"
    )
    assert res.returncode == 0, res.stderr
    assert _config_value(res, "KDIVE_LOCAL_LIBVIRT_ENABLED") == "true"


def test_local_libvirt_emitted_once() -> None:
    # The key is excluded from the .Values.config range and emitted explicitly; a regression
    # that left it in the range too would emit a duplicate ConfigMap key (last-wins, silent).
    # An explicit value present in both the range and the explicit line would render twice.
    res = _template(
        "config.KDIVE_DATABASE_URL=postgresql://x/y", "config.KDIVE_LOCAL_LIBVIRT_ENABLED=true"
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.count("\n  KDIVE_LOCAL_LIBVIRT_ENABLED:") == 1, (
        "KDIVE_LOCAL_LIBVIRT_ENABLED rendered more than once (range + explicit?)"
    )
