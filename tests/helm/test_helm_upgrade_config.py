"""Render assertions for chart upgrade correctness (ADR-0134, #469/#470).

Two upgrade footguns the chart must close:

- #470: a ``config.*`` change must roll the pods that read it via ``envFrom``. A
  ``checksum/config`` pod annotation makes the pod template vary with the rendered
  ConfigMap, so ``helm upgrade`` rolls exactly the three app workloads — and never
  postgres/minio (which do not consume the ConfigMap, so their demo data is preserved).
  The worker is a StatefulSet (ADR-0514); the other two are Deployments.
- #469: ``helm upgrade --reuse-values`` drops new chart-default config keys. The chart
  renders ``KDIVE_LOCAL_LIBVIRT_ENABLED`` from a defensive ``default "false"`` so a reused
  value-set missing the key still renders it (no reaper crash-loop after upgrade).

These shell out to a real ``helm`` binary like the rest of ``tests/helm``; they skip when
helm is absent, so CI must provide the binary for the gate to mean anything.
"""

from __future__ import annotations

import base64
import os
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
# Every workload kind carrying a pod template in this chart. The worker's per-replica scratch
# volumes make it a StatefulSet (ADR-0514); everything else is a Deployment.
_WORKLOAD_KINDS = ("Deployment", "StatefulSet")


def _template(*set_args: str) -> subprocess.CompletedProcess[str]:
    args = ["helm", "template", "kdive", CHART]
    for s in set_args:
        args += ["--set", s]
    return subprocess.run(args, capture_output=True, text=True, check=False)


def _workloads(*set_args: str) -> dict[str, dict[str, Any]]:
    """Render and index every pod-template-carrying workload by its process-name suffix."""
    res = _template(*set_args)
    assert res.returncode == 0, res.stderr
    out: dict[str, dict[str, Any]] = {}
    for doc in yaml.safe_load_all(res.stdout):
        if not (isinstance(doc, dict) and doc.get("kind") in _WORKLOAD_KINDS):
            continue
        name = str(doc["metadata"]["name"])
        suffix = name.rsplit("-", 1)[-1]
        out[suffix] = doc
    return out


def _pod_annotations(workload: dict[str, Any]) -> dict[str, str]:
    return workload["spec"]["template"]["metadata"].get("annotations", {}) or {}


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
    deploy = _workloads("config.KDIVE_DATABASE_URL=postgresql://x/y")[proc]
    annotations = _pod_annotations(deploy)
    checksum = annotations.get("checksum/config")
    assert checksum, f"{proc} pod template has no checksum/config annotation"
    # sha256sum renders a 64-hex-char digest (helm appends a trailing "  -" filename field
    # that the | sha256sum pipe strips via the function's first-field output).
    assert len(checksum) == 64, checksum


def test_config_checksum_changes_when_a_config_value_changes() -> None:
    a = _workloads("config.KDIVE_DATABASE_URL=postgresql://x/y")
    b = _workloads(
        "config.KDIVE_DATABASE_URL=postgresql://x/y", "config.KDIVE_S3_BUCKET=other-bucket"
    )
    for proc in _APP_PROCS:
        ca = _pod_annotations(a[proc])["checksum/config"]
        cb = _pod_annotations(b[proc])["checksum/config"]
        assert ca != cb, f"{proc} checksum did not change on a config.* change"


def test_config_checksum_is_stable_across_renders() -> None:
    # Same inputs must hash the same, or every upgrade would needlessly roll the pods.
    a = _workloads("config.KDIVE_DATABASE_URL=postgresql://x/y")
    b = _workloads("config.KDIVE_DATABASE_URL=postgresql://x/y")
    for proc in _APP_PROCS:
        assert (
            _pod_annotations(a[proc])["checksum/config"]
            == _pod_annotations(b[proc])["checksum/config"]
        )


@pytest.mark.parametrize(
    ("credential", "affected"),
    [
        ("server", {"server"}),
        ("worker", {"worker"}),
        ("reconciler", {"reconciler"}),
    ],
)
def test_database_secret_ref_change_rolls_only_consumers(
    credential: str, affected: set[str]
) -> None:
    before = _workloads()
    after = _workloads(f"databaseCredentials.{credential}.key=rotated-{credential}-dsn")
    for proc in _APP_PROCS:
        before_checksum = _pod_annotations(before[proc])["checksum/config"]
        after_checksum = _pod_annotations(after[proc])["checksum/config"]
        if proc in affected:
            assert before_checksum != after_checksum
        else:
            assert before_checksum == after_checksum


def test_lifecycle_witness_database_ref_change_rolls_only_witness() -> None:
    before = _workloads()
    after = _workloads("databaseCredentials.lifecycleWitness.key=rotated-witness-dsn")
    assert (
        _pod_annotations(before["witness"])["checksum/database-ref"]
        != _pod_annotations(after["witness"])["checksum/database-ref"]
    )
    for proc in _APP_PROCS:
        assert _pod_annotations(before[proc]) == _pod_annotations(after[proc])


def test_backend_pods_have_no_config_checksum_annotation() -> None:
    # postgres/minio/oidc do not consume the config ConfigMap; a checksum on them would roll
    # the emptyDir demo backends on a config change and wipe demo data (#470 acceptance).
    deploys = _workloads("bundledBackends=true", "demoAcknowledged=true")
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


def test_protocol_cutover_requires_explicit_helm_authority_inputs() -> None:
    script = Path(__file__).resolve().parents[2] / "scripts/cutover-capture-protocol-helm.sh"
    result = subprocess.run([str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 2
    assert "RELEASE NAMESPACE VALUES_FILE BACKUP_PATH TARGET_IMAGE" in result.stderr


def test_protocol_cutover_preserves_original_worker_replicas() -> None:
    text = (
        Path(__file__).resolve().parents[2] / "scripts/cutover-capture-protocol-helm.sh"
    ).read_text()
    assert 'get values "$helm_release"' in text
    assert "worker.replicas" in text
    assert '"statefulset/${worker_name}"' in text
    assert "--replicas=0" in text
    assert '--set-string "worker.replicas=${worker_replicas}"' in text
    assert '"${repo_root}/deploy/helm/kdive"' in text
    assert "exact lifecycle termination witness" in text
    assert "workers may still be running" in text
    assert "--reuse-values" not in text


def test_helm_cutover_freezes_every_approved_authority_input() -> None:
    text = (
        Path(__file__).resolve().parents[2] / "scripts/cutover-capture-protocol-helm.sh"
    ).read_text()
    assert "frozen_kubeconfig" in text
    assert "frozen_chart" in text
    assert "frozen_values" in text
    assert '"${work_dir}/authority.json"' in text
    assert "resolved_image" in text
    assert "cutover_secret" in text
    assert "databaseCredentials.migration.secretName=${cutover_secret}" in text
    assert "--server-side --dry-run=server" in text
    assert '"list pods"' in text
    assert '"watch pods"' in text
    assert text.count("current_identity") >= 5
    upgrade = text[text.index("helm_args=(\n  upgrade") :]
    assert '"$frozen_chart"' in upgrade
    assert '"$frozen_values"' in upgrade
    assert '"${repo_root}/deploy/helm/kdive"' not in upgrade
    assert '"$values_file"' not in upgrade


def _write_tool(path: Path, body: str) -> None:
    path.write_text(f"#!/usr/bin/env bash\nset -euo pipefail\n{body}", encoding="utf-8")
    path.chmod(0o755)


def test_helm_cutover_refuses_release_target_dsn_mismatch_before_scale(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls"
    release_dsn = (
        "postgresql://operator:release-sentinel@db.example/kdive"  # pragma: allowlist secret
    )
    encoded = base64.b64encode(release_dsn.encode()).decode()
    _write_tool(
        bin_dir / "helm",
        """
printf 'helm %s\n' "$*" >>"$CUTOVER_TEST_LOG"
case "$*" in
  'version'*) exit 0 ;;
  *'status kdive'*) printf '%s\n' '{"version":4}' ;;
  *'get values kdive'*)
    printf '%s%s\n' '{"worker":{"replicas":2},"databaseCredentials":{"migration":{"secr' \
      'etName":"db-secret","key":"migration-dsn"}}}'
    ;;
  *'template kdive'*)
    secret_name=db-secret
    secret_key=migration-dsn
    if [[ "$*" == *databaseCredentials.migration.secretName=* ]]; then
      secret_name=kdive-kdive-cutover-4
      secret_key=database-url
    fi
    cat <<YAML
apiVersion: batch/v1
kind: Job
metadata: {name: kdive-kdive-migrate}
spec:
  template:
    spec:
      containers:
        - name: migrate
          image: registry.example/kdive@sha256:abc123
          env:
            - name: KDIVE_DATABASE_URL
              valueFrom: {secretKeyRef: {name: $secret_name, key: $secret_key}}
---
apiVersion: apps/v1
kind: StatefulSet
metadata: {name: kdive-kdive-worker}
spec:
  template:
    spec:
      containers:
        - {name: worker, image: 'registry.example/kdive@sha256:abc123'}
YAML
    ;;
esac
""",
    )
    _write_tool(
        bin_dir / "kubectl",
        f"""
printf 'kubectl %s\n' "$*" >>"$CUTOVER_TEST_LOG"
case "$*" in
  'config current-context') printf 'ctx-a\n' ;;
  *'get namespace '*'jsonpath'*) printf 'namespace-uid\n' ;;
  *'auth can-i '*) printf 'yes\n' ;;
  *'get secret db-secret'*) printf '%s\n' '{{"data":{{"migration-dsn":"{encoded}"}}}}' ;;
  *'get statefulset/'*) printf 'kdive\n' ;;
  *'get pods '*) : ;;
esac
""",
    )
    _write_tool(
        bin_dir / "docker",
        """
if [[ "$*" == *RepoDigests* ]]; then
  printf 'registry.example/kdive@sha256:abc123\n'
fi
""",
    )
    _write_tool(bin_dir / "psql", "cat >/dev/null\n")
    _write_tool(bin_dir / "pg_dump", "exit 0\n")
    _write_tool(bin_dir / "pg_restore", "exit 0\n")
    _write_tool(
        bin_dir / "gio",
        '[[ "${1:-}" == trash && -n "${2:-}" ]] && /usr/bin/unlink "$2" 2>/dev/null || true\n',
    )
    values = tmp_path / "values.yaml"
    values.write_text("{}\n", encoding="utf-8")
    backup = tmp_path / "backup.dump"
    supplied = (
        "postgresql://operator:supplied-sentinel@db.example/kdive"  # pragma: allowlist secret
    )

    result = subprocess.run(
        [
            str(Path(__file__).resolve().parents[2] / "scripts/cutover-capture-protocol-helm.sh"),
            "kdive",
            "kdive-system",
            str(values),
            str(backup),
            "registry.example/kdive:v3",
        ],
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "CUTOVER_TEST_LOG": str(log),
            "KDIVE_MIGRATION_DATABASE_URL": supplied,
            "KDIVE_CUTOVER_OPERATION_TIMEOUT_SECONDS": "3",
            "KDIVE_CUTOVER_DB_CONNECT_TIMEOUT_SECONDS": "1",
            "KDIVE_CUTOVER_DB_STATEMENT_TIMEOUT_SECONDS": "2",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )

    assert result.returncode != 0
    calls = log.read_text(encoding="utf-8")
    assert not any(" scale " in line for line in calls.splitlines())
    assert "migration database does not match" in result.stderr
    assert "release-sentinel" not in result.stdout + result.stderr
    assert "supplied-sentinel" not in result.stdout + result.stderr
    for line in calls.splitlines():
        if line.startswith("kubectl ") and "config current-context" not in line:
            assert "--context ctx-a" in line, line
        if line.startswith("helm ") and not line.startswith("helm version"):
            assert "--kube-context ctx-a" in line, line
