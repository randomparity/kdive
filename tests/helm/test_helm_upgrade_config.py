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
    assert "--dry-run=server" in text
    assert "--is-upgrade" in text
    assert '"${helm_ctx[@]}" "${helm_args[@]}"' in text
    assert 'payload["immutable"] = True' in text
    assert "current_cutover_secret" in text
    assert "worker_mutation_started" in text
    assert "complete legacy Kubernetes incarnation witness" in text
    assert "RepoDigests" in text
    assert '"list pods"' in text
    assert '"watch pods"' in text
    assert text.count("current_identity") >= 5
    upgrade_start = text.index("helm_args=(\n  upgrade")
    upgrade = text[upgrade_start : text.index("\n)\n", upgrade_start)]
    assert '"$frozen_chart"' in upgrade
    assert '"$frozen_values"' in upgrade
    assert '"${repo_root}/deploy/helm/kdive"' not in upgrade
    assert '"$values_file"' not in upgrade


def _write_tool(path: Path, body: str) -> None:
    path.write_text(f"#!/usr/bin/env bash\nset -euo pipefail\n{body}", encoding="utf-8")
    path.chmod(0o755)


def _write_mismatch_helm(bin_dir: Path) -> None:
    _write_tool(
        bin_dir / "helm",
        """
printf 'helm %s\n' "$*" >>"$CUTOVER_TEST_LOG"
case "$*" in
  'version'*) exit 0 ;;
  *'status kdive'*)
    [[ -e "$CUTOVER_TEST_UPGRADED" ]] && version=5 || version=4
    printf '{"version":%s}\n' "$version"
    ;;
  *'get values kdive'*)
    printf '%s%s\n' '{"worker":{"replicas":2},"databaseCredentials":{"migration":{"secr' \
      'etName":"db-secret","key":"migration-dsn"}}}'
    ;;
  *'template '*'kdive'*)
    secret_name=db-secret
    secret_key=migration-dsn
    for argument in "$@"; do
      case "$argument" in
        databaseCredentials.migration.secretName=*) secret_name=${argument#*=} ;;
        databaseCredentials.migration.key=*) secret_key=${argument#*=} ;;
      esac
    done
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
  *'upgrade kdive'*'--dry-run=server'*) : ;;
  *'upgrade kdive'*)
    [[ "${CUTOVER_TEST_UPGRADE_STATUS:-0}" -eq 0 ]] || exit "$CUTOVER_TEST_UPGRADE_STATUS"
    : >"$CUTOVER_TEST_UPGRADED"
    ;;
esac
""",
    )


def _write_mismatch_kubectl(bin_dir: Path, encoded: str) -> None:
    _write_tool(
        bin_dir / "kubectl",
        f"""
printf 'kubectl %s\n' "$*" >>"$CUTOVER_TEST_LOG"
case "$*" in
  'config current-context') printf 'ctx-a\n' ;;
  *'get namespace '*'jsonpath'*) printf 'namespace-uid\n' ;;
  *'auth can-i '*)
    if [[ -n "${{CUTOVER_TEST_DENIED_PERMISSION:-}}" &&
      "$*" == *"$CUTOVER_TEST_DENIED_PERMISSION"* ]]; then
      printf 'no\n'
    else
      printf 'yes\n'
    fi
    ;;
  *'get secret db-secret'*) printf '%s\n' '{{"data":{{"migration-dsn":"{encoded}"}}}}' ;;
  *'get secret kdive-cutover-'*'--ignore-not-found'*)
    [[ "${{CUTOVER_TEST_COLLISION:-0}}" == 1 ]] && printf 'secret/collision\n' || true
    ;;
  *'get secret kdive-cutover-'*'jsonpath='*)
    [[ "${{CUTOVER_TEST_EMPTY_SECRET_UID:-0}}" == 1 ]] || printf 'cutover-secret-uid\n'
    ;;
  *'get secret kdive-cutover-'*'--output json'*)
    count=0
    [[ ! -e "$CUTOVER_TEST_SECRET_READS" ]] || count=$(<"$CUTOVER_TEST_SECRET_READS")
    count=$((count + 1))
    printf '%s' "$count" >"$CUTOVER_TEST_SECRET_READS"
    secret_data="$CUTOVER_TEST_SECRET_DATA"
    if [[ "${{CUTOVER_TEST_SECRET_DRIFT_AFTER:-0}}" -gt 0 &&
      "$count" -gt "$CUTOVER_TEST_SECRET_DRIFT_AFTER" ]]; then
      secret_data="$CUTOVER_TEST_TAMPERED_DATA"
    fi
    printf '%s\n' '{{"metadata":{{"uid":"cutover-secret-uid"}},"immutable":true,' \
      '"data":{{"database-url":"'"$secret_data"'"}}}}'
    ;;
  *'create secret generic kdive-cutover-'*'--dry-run=client'*)
    previous=""
    for argument in "$@"; do
      [[ "$previous" != generic ]] || secret_name="$argument"
      previous="$argument"
    done
    printf '%s\n' 'apiVersion: v1' 'kind: Secret' \
      'metadata:' "  name: $secret_name" 'data:' \
      "  database-url: $CUTOVER_TEST_SECRET_DATA"
    ;;
  *'create --filename '*) : >"$CUTOVER_TEST_SECRET_CREATED" ;;
  *'get statefulset/'*'.spec.replicas'*) printf '0\n' ;;
  *'get statefulset/'*) printf 'statefulset-uid\n' ;;
  *'get pods '*) : ;;
  *'scale statefulset/'*) : ;;
  *'wait --for=delete pod'*) : ;;
  *'delete secret kdive-cutover-'*) : ;;
esac
""",
    )


def _helm_mismatch_environment(
    tmp_path: Path,
    *,
    matching_dsn: bool = False,
) -> tuple[dict[str, str], Path, Path, Path, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls"
    release_dsn = (
        "postgresql://operator:release-sentinel@db.example/kdive"  # pragma: allowlist secret
    )
    encoded = base64.b64encode(release_dsn.encode()).decode()
    _write_mismatch_helm(bin_dir)
    _write_mismatch_kubectl(bin_dir, encoded)
    _write_tool(
        bin_dir / "docker",
        """
if [[ "$*" == *RepoDigests* ]]; then
  printf '%s\n' "$CUTOVER_TEST_REPO_DIGESTS"
fi
""",
    )
    _write_tool(
        bin_dir / "psql",
        """
printf 'psql argv=%s pgdatabase=%s pgpassfile=%s kdive=%s migration=%s\n' \
  "$*" "${PGDATABASE:-}" "${PGPASSFILE:-}" "${KDIVE_DATABASE_URL:-}" \
  "${KDIVE_MIGRATION_DATABASE_URL:-}" >>"$CUTOVER_TEST_LOG"
if [[ "$*" == *coalesce* && -n "${CUTOVER_TEST_NEIGHBOR_BLOCKER:-}" ]]; then
  if [[ "$*" != *"'^[0-9]+$'"* ]]; then
    printf '%s\n' "$CUTOVER_TEST_NEIGHBOR_BLOCKER"
  fi
elif [[ -n "${CUTOVER_TEST_LEGACY_BLOCKER:-}" && "$*" == *coalesce* ]]; then
  printf '%s\n' "$CUTOVER_TEST_LEGACY_BLOCKER"
else
  cat >/dev/null
fi
""",
    )
    _write_tool(
        bin_dir / "pg_dump",
        """
printf 'pg_dump argv=%s pgdatabase=%s pgpassfile=%s kdive=%s migration=%s\n' \
  "$*" "${PGDATABASE:-}" "${PGPASSFILE:-}" "${KDIVE_DATABASE_URL:-}" \
  "${KDIVE_MIGRATION_DATABASE_URL:-}" >>"$CUTOVER_TEST_LOG"
for argument in "$@"; do
  case "$argument" in --file=*) : >"${argument#--file=}" ;; esac
done
""",
    )
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
    if matching_dsn:
        supplied = release_dsn
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "CUTOVER_TEST_LOG": str(log),
        "CUTOVER_TEST_REPO_DIGESTS": '["registry.example/kdive@sha256:abc123"]',
        "CUTOVER_TEST_SECRET_DATA": encoded,
        "CUTOVER_TEST_TAMPERED_DATA": base64.b64encode(b"tampered").decode(),
        "CUTOVER_TEST_SECRET_READS": str(tmp_path / "secret-reads"),
        "CUTOVER_TEST_SECRET_CREATED": str(tmp_path / "secret-created"),
        "CUTOVER_TEST_UPGRADED": str(tmp_path / "upgraded"),
        "KDIVE_MIGRATION_DATABASE_URL": supplied,
        "KDIVE_CUTOVER_OPERATION_TIMEOUT_SECONDS": "3",
        "KDIVE_CUTOVER_DB_CONNECT_TIMEOUT_SECONDS": "1",
        "KDIVE_CUTOVER_DB_STATEMENT_TIMEOUT_SECONDS": "2",
    }
    return env, log, values, backup, supplied


def _run_fake_helm_cutover(
    env: dict[str, str], values: Path, backup: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            str(Path(__file__).resolve().parents[2] / "scripts/cutover-capture-protocol-helm.sh"),
            "kdive",
            "kdive-system",
            str(values),
            str(backup),
            "registry.example/kdive:v3",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )


def test_helm_cutover_refuses_release_target_dsn_mismatch_before_scale(tmp_path: Path) -> None:
    env, log, values, backup, supplied = _helm_mismatch_environment(tmp_path)

    result = _run_fake_helm_cutover(env, values, backup)

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


@pytest.mark.parametrize(
    "repo_digests",
    [
        '["other.example/kdive@sha256:abc123"]',
        '["registry.example/kdive@sha256:abc123","registry.example/kdive@sha256:def456"]',
    ],
)
def test_helm_cutover_requires_unique_digest_for_approved_repository(
    tmp_path: Path, repo_digests: str
) -> None:
    env, log, values, backup, _supplied = _helm_mismatch_environment(tmp_path, matching_dsn=True)
    env["CUTOVER_TEST_REPO_DIGESTS"] = repo_digests

    result = _run_fake_helm_cutover(env, values, backup)

    assert result.returncode != 0
    assert "no unique digest for its approved repository" in result.stderr
    assert not any(" scale " in line for line in log.read_text().splitlines())


def test_helm_secret_collision_aborts_without_create_or_worker_stop(tmp_path: Path) -> None:
    env, log, values, backup, _supplied = _helm_mismatch_environment(tmp_path, matching_dsn=True)
    env["CUTOVER_TEST_COLLISION"] = "1"

    result = _run_fake_helm_cutover(env, values, backup)

    assert result.returncode != 0
    calls = log.read_text(encoding="utf-8")
    assert "unexpectedly exists" in result.stderr
    assert " create --filename " not in calls
    assert " scale " not in calls


def test_helm_post_create_identity_failure_never_stops_worker(tmp_path: Path) -> None:
    env, log, values, backup, _supplied = _helm_mismatch_environment(tmp_path, matching_dsn=True)
    env["CUTOVER_TEST_EMPTY_SECRET_UID"] = "1"

    result = _run_fake_helm_cutover(env, values, backup)

    assert result.returncode != 0
    assert "worker deployment was not mutated" in result.stderr
    assert " scale " not in log.read_text(encoding="utf-8")


def test_helm_revalidates_immutable_secret_before_backup(tmp_path: Path) -> None:
    env, log, values, backup, _supplied = _helm_mismatch_environment(tmp_path, matching_dsn=True)
    env["CUTOVER_TEST_SECRET_DRIFT_AFTER"] = "2"

    result = _run_fake_helm_cutover(env, values, backup)

    assert result.returncode != 0
    calls = log.read_text(encoding="utf-8")
    assert "credential changed" in result.stderr
    assert calls.count(" scale statefulset/") == 2
    assert "pg_dump " not in calls


def test_helm_preflight_and_upgrade_use_exact_upgrade_mode(tmp_path: Path) -> None:
    env, log, values, backup, _supplied = _helm_mismatch_environment(tmp_path, matching_dsn=True)

    result = _run_fake_helm_cutover(env, values, backup)

    assert result.returncode == 0, result.stderr
    calls = log.read_text(encoding="utf-8")
    assert " template --is-upgrade " in calls
    assert " upgrade kdive " in calls
    assert "--dry-run=server" in calls
    assert "kubectl " in calls and " apply " not in calls
    snapshots = list(tmp_path.glob(".kdive-helm-cutover.*/cutover-secret.yaml"))
    assert len(snapshots) == 1
    secret = yaml.safe_load(snapshots[0].read_text(encoding="utf-8"))
    assert secret["immutable"] is True
    assert secret["metadata"]["name"].startswith("kdive-cutover-")


def test_helm_database_processes_never_receive_owner_dsn(tmp_path: Path) -> None:
    env, log, values, backup, _supplied = _helm_mismatch_environment(tmp_path, matching_dsn=True)

    result = _run_fake_helm_cutover(env, values, backup)

    assert result.returncode == 0, result.stderr
    database_calls = "\n".join(
        line
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.startswith(("psql ", "pg_dump "))
    )
    assert "release-sentinel" not in database_calls
    assert "pgpassfile=" in database_calls
    assert "pgdatabase=postgresql://operator@db.example/kdive" in database_calls


def test_helm_full_permission_preflight_denial_never_mutates(tmp_path: Path) -> None:
    env, log, values, backup, _supplied = _helm_mismatch_environment(tmp_path, matching_dsn=True)
    env["CUTOVER_TEST_DENIED_PERMISSION"] = "delete jobs.batch"

    result = _run_fake_helm_cutover(env, values, backup)

    assert result.returncode != 0
    calls = log.read_text(encoding="utf-8")
    assert "authorization denied: delete jobs.batch" in result.stderr
    assert " create --filename " not in calls
    assert " scale " not in calls


def test_helm_replicaset_read_denial_never_mutates(tmp_path: Path) -> None:
    env, log, values, backup, _supplied = _helm_mismatch_environment(tmp_path, matching_dsn=True)
    env["CUTOVER_TEST_DENIED_PERMISSION"] = "get replicasets.apps"

    result = _run_fake_helm_cutover(env, values, backup)

    assert result.returncode != 0
    calls = log.read_text(encoding="utf-8")
    assert "authorization denied: get replicasets.apps" in result.stderr
    assert " create --filename " not in calls
    assert " scale " not in calls


def test_helm_complete_legacy_incarnation_witness_blocks_backup(tmp_path: Path) -> None:
    env, log, values, backup, _supplied = _helm_mismatch_environment(tmp_path, matching_dsn=True)
    env["CUTOVER_TEST_LEGACY_BLOCKER"] = "kubernetes:kdive-system:kdive-worker-2:uid-new"

    result = _run_fake_helm_cutover(env, values, backup)

    assert result.returncode != 0
    calls = log.read_text(encoding="utf-8")
    assert "not exactly terminated" in result.stderr
    assert "uid-new" in result.stderr
    assert "pg_dump " not in calls
    assert "old schema remains authoritative" in result.stderr
    assert "cutover-capture-protocol-helm.sh" in result.stderr
    assert "pg_restore" not in result.stderr


def test_helm_legacy_witness_ignores_neighboring_release_prefix_collision(
    tmp_path: Path,
) -> None:
    env, log, values, backup, _supplied = _helm_mismatch_environment(tmp_path, matching_dsn=True)
    env["CUTOVER_TEST_NEIGHBOR_BLOCKER"] = (
        "kubernetes:kdive-system:kdive-kdive-worker-x-kdive-worker-0:neighbor-uid"
    )

    result = _run_fake_helm_cutover(env, values, backup)

    assert result.returncode == 0, result.stderr
    assert backup.exists()
    assert "pg_dump " in log.read_text(encoding="utf-8")


def test_helm_upgrade_failure_prints_exact_resume_and_rollback(tmp_path: Path) -> None:
    env, _log, values, backup, _supplied = _helm_mismatch_environment(tmp_path, matching_dsn=True)
    env["CUTOVER_TEST_UPGRADE_STATUS"] = "41"

    result = _run_fake_helm_cutover(env, values, backup)

    assert result.returncode == 41
    assert backup.exists()
    assert "resume the frozen protocol-3 upgrade exactly" in result.stderr
    assert "helm --kubeconfig" in result.stderr
    assert "delete secret kdive-cutover-" in result.stderr
    assert "pg_restore --clean --if-exists" in result.stderr
    assert "PGPASSFILE=" in result.stderr
    assert "postgresql://operator@db.example/kdive" in result.stderr
    assert "release-sentinel" not in result.stdout + result.stderr
