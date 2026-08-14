"""Render/lint gate for the kdive Helm chart (ADR-0088, M2.1 Phase 4).

These tests shell out to a real ``helm`` binary so the chart's templating logic
(the demo-acknowledged render gate, migrate-Job hook phase, workload count) is
exercised end to end. They skip when ``helm`` is not installed; a skipped run
validates nothing, so CI must provide the binary for this gate to mean anything.

The worker is a StatefulSet, not a Deployment (ADR-0514), so the per-process helpers
index both kinds — see :func:`_workloads`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")

CHART = str(Path(__file__).resolve().parents[2] / "deploy" / "helm" / "kdive")

_VERSIONING_REPLIES = (
    (
        "enabled-omitted-exclusions",
        '{"Op":"info","status":"success","url":"local/kdive-artifacts",'
        '"versioning":{"status":"Enabled","MFADelete":""}}',
        0,
    ),
    (
        "enabled-explicit-empty-exclusions",
        '{"Op":"info","status":"success","url":"local/kdive-artifacts",'
        '"versioning":{"status":"Enabled","MFADelete":"","ExcludedPrefixes":[]}}',
        0,
    ),
    (
        "suspended",
        '{"Op":"info","status":"success","url":"local/kdive-artifacts",'
        '"versioning":{"status":"Suspended","MFADelete":""}}',
        1,
    ),
    (
        "excluded-prefix",
        '{"Op":"info","status":"success","url":"local/kdive-artifacts",'
        '"versioning":{"status":"Enabled","MFADelete":"",'
        '"ExcludedPrefixes":["tmp/"]}}',
        1,
    ),
    (
        "excluded-folders",
        '{"Op":"info","status":"success","url":"local/kdive-artifacts",'
        '"versioning":{"status":"Enabled","MFADelete":"","ExcludeFolders":true}}',
        1,
    ),
    (
        "missing-status",
        '{"Op":"info","status":"success","url":"local/kdive-artifacts",'
        '"versioning":{"MFADelete":""}}',
        1,
    ),
    (
        "mfa-delete-enabled",
        '{"Op":"info","status":"success","url":"local/kdive-artifacts",'
        '"versioning":{"status":"Enabled","MFADelete":"Enabled"}}',
        1,
    ),
    (
        "missing-mfa-delete",
        '{"Op":"info","status":"success","url":"local/kdive-artifacts",'
        '"versioning":{"status":"Enabled"}}',
        1,
    ),
    (
        "malformed-exclusions",
        '{"Op":"info","status":"success","url":"local/kdive-artifacts",'
        '"versioning":{"status":"Enabled","MFADelete":"","ExcludedPrefixes":null}}',
        1,
    ),
    (
        "compatible-decoy-before-suspended-real-state",
        '{"Op":"info","status":"success","url":"local/kdive-artifacts",'
        '"decoy":{"versioning":{"status":"Enabled","MFADelete":""}},'
        '"versioning":{"status":"Suspended","MFADelete":""}}',
        1,
    ),
    (
        "duplicate-versioning-keys",
        '{"Op":"info","status":"success","url":"local/kdive-artifacts",'
        '"versioning":{"status":"Enabled","MFADelete":""},'
        '"versioning":{"status":"Suspended","MFADelete":""}}',
        1,
    ),
    (
        "trailing-junk",
        '{"Op":"info","status":"success","url":"local/kdive-artifacts",'
        '"versioning":{"status":"Enabled","MFADelete":""}}not-json',
        1,
    ),
    (
        "error-status-decoy",
        '{"Op":"info","status":"error","url":"local/kdive-artifacts",'
        '"versioning":{"status":"Enabled","MFADelete":""}}',
        1,
    ),
    (
        "reordered-fields",
        '{"status":"success","Op":"info","url":"local/kdive-artifacts",'
        '"versioning":{"status":"Enabled","MFADelete":""}}',
        1,
    ),
    (
        "extra-top-level-field",
        '{"Op":"info","status":"success","url":"local/kdive-artifacts",'
        '"versioning":{"status":"Enabled","MFADelete":""},"extra":true}',
        1,
    ),
)

_SHELL_BUCKET_CASES = (
    ("command-substitution", "custom$(touch {sentinel})"),
    ("semicolon", "custom;touch {sentinel}"),
    ("whitespace", "custom bucket"),
    ("quotes", "custom'\"quoted"),
    ("other-metacharacters", "custom&touch {sentinel}"),
)

# Per-process aux health/metrics ports (ADR-0090 §5), matching the registry defaults.
_AUX_PORTS = {"server": 9464, "worker": 9465, "reconciler": 9466}

# The workload kind that carries each app process's pod template. The worker owns per-replica
# scratch volumes, so it is a StatefulSet with volumeClaimTemplates (ADR-0514).
_WORKLOAD_KINDS = {
    "server": "Deployment",
    "worker": "StatefulSet",
    "reconciler": "Deployment",
}


def _template(*set_args: str) -> subprocess.CompletedProcess[str]:
    args = ["helm", "template", "kdive", CHART]
    for s in set_args:
        args += ["--set", s]
    return _template_args(*args)


def _template_args(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False)


def _template_bundled_bucket(bucket: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "helm",
            "template",
            "kdive",
            CHART,
            "--set",
            "bundledBackends=true",
            "--set",
            "demoAcknowledged=true",
            "--set-json",
            f"config.KDIVE_S3_BUCKET={json.dumps(bucket)}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def _minio_init_container_from_render(
    res: subprocess.CompletedProcess[str],
) -> dict[str, Any]:
    assert res.returncode == 0, res.stderr
    for doc in yaml.safe_load_all(res.stdout):
        if not (isinstance(doc, dict) and doc.get("kind") == "Job"):
            continue
        if str(doc.get("metadata", {}).get("name", "")).endswith("-minio-init"):
            return doc["spec"]["template"]["spec"]["containers"][0]
    raise AssertionError("bundled chart rendered no MinIO initializer Job")


def _minio_init_container(*set_args: str) -> dict[str, Any]:
    res = _template("bundledBackends=true", "demoAcknowledged=true", *set_args)
    return _minio_init_container_from_render(res)


def _minio_init_script(*set_args: str) -> str:
    command = _minio_init_container(*set_args)["command"]
    assert command[:2] == ["/bin/sh", "-c"]
    return command[2]


def _literal_env(container: dict[str, Any]) -> dict[str, str]:
    return {entry["name"]: entry["value"] for entry in container["env"]}


def _fake_mc(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    calls = tmp_path / "mc-calls"
    executable = tmp_path / "mc"
    executable.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        'printf \'%s\\n\' "$*" >>"$MC_CALLS"\n'
        "last_argument=\n"
        'for argument in "$@"; do last_argument=$argument; done\n'
        'case "$1 ${2:-}" in\n'
        '  "mb --ignore-existing"|"version enable"|"version info")\n'
        '    printf \'%s\\n\' "$last_argument" >>"$MC_BUCKET_ARGS"\n'
        "    ;;\n"
        "esac\n"
        'case "$*" in\n'
        '  "version info --json "*)\n'
        '    [ "${MC_INFO_FAIL:-0}" = 0 ] || exit 23\n'
        "    printf '%s\\n' \"$MC_VERSION_INFO\"\n"
        "    ;;\n"
        "esac\n"
    )
    executable.chmod(0o755)
    return calls


def _run_minio_init(
    tmp_path: Path,
    reply: str,
    *,
    info_fails: bool = False,
    container: dict[str, Any] | None = None,
) -> tuple[int, list[str]]:
    calls = _fake_mc(tmp_path)
    rendered_container = container or _minio_init_container()
    command = rendered_container["command"]
    assert command[:2] == ["/bin/sh", "-c"]
    result = subprocess.run(
        ["/bin/bash", "-c", command[2]],
        capture_output=True,
        text=True,
        env={
            **_literal_env(rendered_container),
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "MC_CALLS": str(calls),
            "MC_BUCKET_ARGS": str(tmp_path / "mc-buckets"),
            "MC_VERSION_INFO": reply,
            "MC_INFO_FAIL": "1" if info_fails else "0",
        },
    )
    return result.returncode, calls.read_text().splitlines()


def _minio_ext_cidrs(res: subprocess.CompletedProcess[str]) -> list[str] | None:
    """Return the ipBlock CIDRs of the `-minio-ext` NetworkPolicy, or None if it's not rendered.

    This is the actual exposure control (NOTES only mirrors it in prose). Rendered by
    `helm template`, so it works offline — unlike NOTES, which Helm only emits via
    `helm install`, and that contacts the cluster.
    """
    for doc in yaml.safe_load_all(res.stdout):
        if not (isinstance(doc, dict) and doc.get("kind") == "NetworkPolicy"):
            continue
        if not str(doc.get("metadata", {}).get("name", "")).endswith("-minio-ext"):
            continue
        cidrs: list[str] = []
        for rule in doc["spec"]["ingress"]:
            for src in rule["from"]:
                cidrs.append(src["ipBlock"]["cidr"])
        return cidrs
    return None


def _oidc_request_mappings(res: subprocess.CompletedProcess[str]) -> list[dict[str, Any]]:
    """Parse the demo issuer's ``requestMappings`` out of the rendered JSON_CONFIG env var.

    Returns the ordered list of mappings (order is load-bearing: mock-oauth2-server is
    first-match-wins). Raises if the oidc Deployment or its JSON_CONFIG is absent.
    """
    for doc in yaml.safe_load_all(res.stdout):
        if not (isinstance(doc, dict) and doc.get("kind") == "Deployment"):
            continue
        if not str(doc.get("metadata", {}).get("name", "")).endswith("-oidc"):
            continue
        env = doc["spec"]["template"]["spec"]["containers"][0]["env"]
        raw = next(e["value"] for e in env if e["name"] == "JSON_CONFIG")
        mappings = json.loads(raw)["tokenCallbacks"][0]["requestMappings"]
        assert isinstance(mappings, list)
        return mappings
    raise AssertionError("no -oidc Deployment with a JSON_CONFIG env var in the render")


def test_renders_three_app_workloads_against_external_backends() -> None:
    res = _template("config.KDIVE_DATABASE_URL=postgresql://x/y")
    assert res.returncode == 0, res.stderr
    # Server, reconciler, and lifecycle witness are Deployments; worker is a StatefulSet.
    assert res.stdout.count("kind: Deployment") == 3
    assert res.stdout.count("kind: StatefulSet") == 1
    assert "pre-install" in res.stdout


def _container_env(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    container = doc["spec"]["template"]["spec"]["containers"][0]
    return {item["name"]: item for item in container.get("env", [])}


def test_database_principals_are_distinct_secret_refs() -> None:
    res = _template("config.KDIVE_DATABASE_URL=postgresql://migration-owner/db")
    assert res.returncode == 0, res.stderr
    docs = [doc for doc in yaml.safe_load_all(res.stdout) if isinstance(doc, dict)]
    expected = {
        "migrate": ("kdive-database", "migration-dsn"),
        "server": ("kdive-database", "server-dsn"),
        "worker": ("kdive-database", "worker-dsn"),
        "reconciler": ("kdive-database", "reconciler-dsn"),
    }
    for suffix, (secret_name, key) in expected.items():
        workload = next(
            doc
            for doc in docs
            if doc.get("kind") in {"Deployment", "StatefulSet", "Job"}
            and str(doc["metadata"]["name"]).endswith(f"-{suffix}")
        )
        ref = _container_env(workload)["KDIVE_DATABASE_URL"]["valueFrom"]["secretKeyRef"]
        assert ref == {"name": secret_name, "key": key}

    witness_workload = next(
        doc
        for doc in docs
        if doc.get("kind") == "Deployment" and str(doc["metadata"]["name"]).endswith("-witness")
    )
    witness = _container_env(witness_workload)["KDIVE_DATABASE_URL"]
    assert witness["valueFrom"]["secretKeyRef"] == {
        "name": "kdive-database",
        "key": "lifecycle-witness-dsn",
    }


def test_shared_config_omits_database_credentials() -> None:
    res = _template("config.KDIVE_DATABASE_URL=postgresql://migration-owner/db")
    assert res.returncode == 0, res.stderr
    config = next(
        doc
        for doc in yaml.safe_load_all(res.stdout)
        if isinstance(doc, dict)
        and doc.get("kind") == "ConfigMap"
        and str(doc["metadata"]["name"]).endswith("-config")
    )
    assert "KDIVE_DATABASE_URL" not in config["data"]
    runtime_docs = [
        doc
        for doc in yaml.safe_load_all(res.stdout)
        if isinstance(doc, dict) and doc.get("kind") in {"Deployment", "StatefulSet"}
    ]
    assert "migration-dsn" not in yaml.safe_dump_all(runtime_docs)


@pytest.mark.parametrize(
    "role", ["migration", "server", "worker", "reconciler", "lifecycleWitness"]
)
@pytest.mark.parametrize("field", ["secretName", "key"])
def test_missing_database_credential_ref_is_rejected(role: str, field: str) -> None:
    res = _template(f"databaseCredentials.{role}.{field}=")
    assert res.returncode != 0
    assert f"databaseCredentials.{role}" in res.stderr


@pytest.mark.parametrize("role", ["server", "worker", "reconciler", "lifecycleWitness"])
def test_runtime_database_credential_cannot_alias_migration_ref(role: str) -> None:
    res = _template(f"databaseCredentials.{role}.key=migration-dsn")
    assert res.returncode != 0
    assert f"databaseCredentials.{role} must not alias databaseCredentials.migration" in res.stderr


@pytest.mark.parametrize(
    ("role", "other"),
    [
        ("server", "worker"),
        ("server", "reconciler"),
        ("server", "lifecycleWitness"),
        ("worker", "reconciler"),
        ("worker", "lifecycleWitness"),
        ("reconciler", "lifecycleWitness"),
    ],
)
def test_runtime_database_credential_refs_are_pairwise_distinct(role: str, other: str) -> None:
    default_keys = {
        "server": "server-dsn",
        "worker": "worker-dsn",
        "reconciler": "reconciler-dsn",
        "lifecycleWitness": "lifecycle-witness-dsn",
    }
    res = _template(
        f"databaseCredentials.{role}.secretName=kdive-database",
        f"databaseCredentials.{role}.key={default_keys[other]}",
    )
    assert res.returncode != 0
    assert "must not alias" in res.stderr
    assert f"databaseCredentials.{role}" in res.stderr
    assert f"databaseCredentials.{other}" in res.stderr


def test_database_principals_support_distinct_secrets_and_keys() -> None:
    overrides = []
    for role in ("migration", "server", "worker", "reconciler", "lifecycleWitness"):
        overrides.extend(
            [
                f"databaseCredentials.{role}.secretName={role}-database",
                f"databaseCredentials.{role}.key={role}-url",
            ]
        )
    res = _template(*overrides)
    assert res.returncode == 0, res.stderr
    docs = [doc for doc in yaml.safe_load_all(res.stdout) if isinstance(doc, dict)]
    rendered = yaml.safe_dump_all(docs)
    for role in ("migration", "server", "worker", "reconciler", "lifecycleWitness"):
        assert f"name: {role}-database" in rendered
        assert f"key: {role}-url" in rendered


def test_worker_death_verifier_has_pod_uid_identity_and_namespaced_get_only_rbac() -> None:
    res = _template("config.KDIVE_DATABASE_URL=postgresql://x/y", "worker.replicas=2")
    assert res.returncode == 0, res.stderr
    docs = [doc for doc in yaml.safe_load_all(res.stdout) if isinstance(doc, dict)]
    role = next(doc for doc in docs if doc.get("kind") == "Role")
    binding = next(doc for doc in docs if doc.get("kind") == "RoleBinding")
    server = next(
        doc
        for doc in docs
        if doc.get("kind") == "Deployment" and str(doc["metadata"]["name"]).endswith("-server")
    )
    worker = next(doc for doc in docs if doc.get("kind") == "StatefulSet")

    rule = role["rules"]
    assert rule[0]["apiGroups"] == [""]
    assert rule[0]["resources"] == ["pods"]
    assert rule[0]["verbs"] == ["get"]
    assert rule[0]["resourceNames"] == [f"kdive-kdive-worker-{ordinal}" for ordinal in range(32)]
    assert rule[1]["resources"] == ["pods"]
    assert rule[1]["verbs"] == ["patch"]
    assert rule[1]["resourceNames"] == rule[0]["resourceNames"]
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": "kdive-kdive-worker-termination-witness"}
    ]
    assert server["spec"]["template"]["spec"]["serviceAccountName"].endswith("-server")
    server_env = server["spec"]["template"]["spec"]["containers"][0]["env"]
    assert {item["name"]: item.get("value") for item in server_env}[
        "KDIVE_WORKER_DEATH_VERIFIER"
    ] == "kubernetes"
    worker_env = worker["spec"]["template"]["spec"]["containers"][0]["env"]
    env_by_name = {item["name"]: item for item in worker_env}
    assert env_by_name["KDIVE_POD_UID"]["valueFrom"]["fieldRef"]["fieldPath"] == "metadata.uid"
    assert worker["spec"]["template"]["metadata"]["finalizers"] == [
        "kdive.io/worker-termination-evidence"
    ]
    assert worker["metadata"]["annotations"]["kdive.io/death-verification-ordinal-ceiling"] == "32"

    witness = next(
        doc
        for doc in docs
        if doc.get("kind") == "Deployment" and str(doc["metadata"]["name"]).endswith("-witness")
    )
    assert witness["spec"]["template"]["spec"]["serviceAccountName"].endswith(
        "-worker-termination-witness"
    )


def test_worker_credential_broker_is_private_tls_and_init_only() -> None:
    res = _template(
        "config.KDIVE_DATABASE_URL=postgresql://x/y",
        "workerCredentialBroker.tls.secretName=broker-tls",
        "workerCredentialBroker.envelopeKey.secretName=broker-envelope",
    )
    assert res.returncode == 0, res.stderr
    docs = [doc for doc in yaml.safe_load_all(res.stdout) if isinstance(doc, dict)]
    service = next(
        doc
        for doc in docs
        if doc.get("kind") == "Service"
        and str(doc["metadata"]["name"]).endswith("-worker-credential-broker")
    )
    policy = next(
        doc
        for doc in docs
        if doc.get("kind") == "NetworkPolicy"
        and str(doc["metadata"]["name"]).endswith("-worker-credential-broker")
    )
    worker = next(doc for doc in docs if doc.get("kind") == "StatefulSet")
    witness = next(
        doc
        for doc in docs
        if doc.get("kind") == "Deployment" and str(doc["metadata"]["name"]).endswith("-witness")
    )

    assert service["spec"]["type"] == "ClusterIP"
    assert service["spec"].get("clusterIP") != "None"
    assert policy["spec"]["podSelector"]["matchLabels"]["app"].endswith("-witness")
    assert policy["spec"]["ingress"][0]["from"][0]["podSelector"]["matchLabels"]["app"].endswith(
        "-worker"
    )
    assert worker["spec"]["template"]["spec"]["automountServiceAccountToken"] is False
    init = worker["spec"]["template"]["spec"]["initContainers"][0]
    worker_container = worker["spec"]["template"]["spec"]["containers"][0]
    assert init["command"] == [
        "python",
        "-m",
        "kdive.processes.lifecycle.kubernetes_credential_init",
    ]
    assert any(
        volume["emptyDir"].get("medium") == "Memory"
        for volume in worker["spec"]["template"]["spec"]["volumes"]
    )
    assert all(
        "credential-token" not in mount["name"] for mount in worker_container["volumeMounts"]
    )
    assert all("broker-envelope" not in mount["name"] for mount in worker_container["volumeMounts"])
    witness_mounts = witness["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    assert any(mount["name"] == "broker-envelope" for mount in witness_mounts)
    lifecycle_dsn = next(
        item
        for item in witness["spec"]["template"]["spec"]["containers"][0]["env"]
        if item["name"] == "KDIVE_DATABASE_URL"
    )
    assert lifecycle_dsn["valueFrom"]["secretKeyRef"]["name"] == "kdive-database"


def test_lifecycle_authority_is_isolated_from_reconciler() -> None:
    res = _template()
    assert res.returncode == 0, res.stderr
    docs = [doc for doc in yaml.safe_load_all(res.stdout) if isinstance(doc, dict)]
    deployments = {
        str(doc["metadata"]["name"]).rsplit("-", 1)[-1]: doc
        for doc in docs
        if doc.get("kind") == "Deployment"
    }
    reconciler = deployments["reconciler"]
    witness = deployments["witness"]
    reconciler_container = reconciler["spec"]["template"]["spec"]["containers"][0]
    witness_container = witness["spec"]["template"]["spec"]["containers"][0]
    reconciler_text = yaml.safe_dump(reconciler_container)
    witness_text = yaml.safe_dump(witness_container)

    assert reconciler_container["args"] == ["reconciler"]
    assert "lifecycle-witness-dsn" not in reconciler_text
    assert "broker-envelope" not in reconciler_text
    assert "broker-tls" not in reconciler_text
    assert witness_container["args"] == ["lifecycle-witness"]
    assert witness_container["ports"][0]["containerPort"] == 9467
    assert witness_container["readinessProbe"]["httpGet"] == {"path": "/readyz", "port": 9467}
    assert "lifecycle-witness-dsn" in witness_text
    assert "broker-envelope" in witness_text
    assert "broker-tls" in witness_text
    assert "envFrom" not in witness_container
    assert witness["spec"]["template"]["spec"]["serviceAccountName"].endswith(
        "-worker-termination-witness"
    )
    assert reconciler["spec"]["template"]["spec"]["automountServiceAccountToken"] is False

    service = next(
        doc
        for doc in docs
        if doc.get("kind") == "Service"
        and str(doc["metadata"]["name"]).endswith("-worker-credential-broker")
    )
    policy = next(
        doc
        for doc in docs
        if doc.get("kind") == "NetworkPolicy"
        and str(doc["metadata"]["name"]).endswith("-worker-credential-broker")
    )
    assert service["spec"]["selector"]["app"].endswith("-witness")
    assert policy["spec"]["podSelector"]["matchLabels"]["app"].endswith("-witness")


def test_worker_credential_broker_port_names_meet_kubernetes_limit() -> None:
    res = _template()
    assert res.returncode == 0, res.stderr
    docs = [doc for doc in yaml.safe_load_all(res.stdout) if isinstance(doc, dict)]
    service = next(
        doc
        for doc in docs
        if doc.get("kind") == "Service"
        and str(doc["metadata"]["name"]).endswith("-worker-credential-broker")
    )
    witness = next(
        doc
        for doc in docs
        if doc.get("kind") == "Deployment" and str(doc["metadata"]["name"]).endswith("-witness")
    )
    names = [service["spec"]["ports"][0]["name"]]
    container_ports = witness["spec"]["template"]["spec"]["containers"][0]["ports"]
    names.extend(port["name"] for port in container_ports if "name" in port)
    assert all(len(name) <= 15 for name in names)
    assert service["spec"]["ports"][0]["targetPort"] in names


def test_worker_death_authority_ceiling_survives_scale_down_and_is_bounded() -> None:
    res = _template(
        "config.KDIVE_DATABASE_URL=postgresql://x/y",
        "worker.replicas=0",
        "worker.deathVerificationOrdinalCeiling=4",
    )
    assert res.returncode == 0, res.stderr
    role = next(
        doc
        for doc in yaml.safe_load_all(res.stdout)
        if isinstance(doc, dict) and doc.get("kind") == "Role"
    )
    assert role["rules"][0]["resourceNames"] == [
        "kdive-kdive-worker-0",
        "kdive-kdive-worker-1",
        "kdive-kdive-worker-2",
        "kdive-kdive-worker-3",
    ]


def test_worker_death_authority_ceiling_must_cover_replicas() -> None:
    res = _template(
        "config.KDIVE_DATABASE_URL=postgresql://x/y",
        "worker.replicas=3",
        "worker.deathVerificationOrdinalCeiling=2",
    )
    assert res.returncode != 0
    assert "deathVerificationOrdinalCeiling" in res.stderr


@pytest.mark.parametrize("ceiling", [0, 257])
def test_worker_death_authority_ceiling_is_bounded(ceiling: int) -> None:
    res = _template(
        "config.KDIVE_DATABASE_URL=postgresql://x/y",
        "worker.replicas=0",
        f"worker.deathVerificationOrdinalCeiling={ceiling}",
    )
    assert res.returncode != 0
    assert "between 1 and 256" in res.stderr


def test_bundled_without_ack_fails_to_render() -> None:
    res = _template("bundledBackends=true")
    assert res.returncode != 0
    assert "demoAcknowledged" in res.stderr


def test_bundled_with_ack_uses_post_install_migrate() -> None:
    res = _template("bundledBackends=true", "demoAcknowledged=true")
    assert res.returncode == 0, res.stderr
    assert "post-install" in res.stdout


def _bundled_documents(*, upgrade: bool) -> list[dict[str, Any]]:
    args = ["helm", "template", "kdive", CHART]
    if upgrade:
        args.append("--is-upgrade")
    args.extend(["--set", "bundledBackends=true", "--set", "demoAcknowledged=true"])
    rendered = _template_args(*args)
    assert rendered.returncode == 0, rendered.stderr
    return [doc for doc in yaml.safe_load_all(rendered.stdout) if isinstance(doc, dict)]


def test_bundled_fresh_install_keeps_worker_at_zero_until_later_scaler_hook() -> None:
    docs = _bundled_documents(upgrade=False)
    worker = next(doc for doc in docs if doc.get("kind") == "StatefulSet")
    jobs = [doc for doc in docs if doc.get("kind") == "Job"]
    migrate = next(doc for doc in jobs if doc["metadata"]["name"].endswith("-migrate"))
    assert worker["spec"]["replicas"] == 0
    assert migrate["metadata"]["annotations"]["helm.sh/hook"] == ("post-install,pre-upgrade")
    scaler = next(doc for doc in jobs if doc["metadata"]["name"].endswith("-worker-scaler"))
    assert scaler["metadata"]["annotations"]["helm.sh/hook"] == "post-install"
    assert int(scaler["metadata"]["annotations"]["helm.sh/hook-weight"]) > int(
        migrate["metadata"]["annotations"]["helm.sh/hook-weight"]
    )
    assert scaler["spec"]["backoffLimit"] == 0
    assert scaler["spec"]["template"]["spec"]["restartPolicy"] == "Never"


def test_bundled_install_scaler_has_only_namespaced_worker_scale_authority() -> None:
    docs = _bundled_documents(upgrade=False)
    role = next(
        doc
        for doc in docs
        if doc.get("kind") == "Role" and doc["metadata"]["name"].endswith("install-worker-scaler")
    )
    assert role["rules"] == [
        {
            "apiGroups": ["apps"],
            "resources": ["statefulsets/scale"],
            "resourceNames": ["kdive-kdive-worker"],
            "verbs": ["patch"],
        },
        {
            "apiGroups": ["rbac.authorization.k8s.io"],
            "resources": ["rolebindings"],
            "resourceNames": ["kdive-kdive-install-worker-scaler"],
            "verbs": ["patch"],
        },
    ]
    assert not any(
        doc.get("kind") in {"ClusterRole", "ClusterRoleBinding"}
        and str(doc.get("metadata", {}).get("name", "")).endswith("install-worker-scaler")
        for doc in docs
    )


def test_bundled_install_scaler_revokes_its_binding_after_scaling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docs = _bundled_documents(upgrade=False)
    scaler = next(
        doc
        for doc in docs
        if doc.get("kind") == "Job" and doc["metadata"]["name"].endswith("-worker-scaler")
    )
    container = scaler["spec"]["template"]["spec"]["containers"][0]
    requests: list[Any] = []

    class Response:
        status = 200

        def __init__(self, payload: bytes) -> None:
            self.payload = payload

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return self.payload

    def fake_open(path: str, **_kwargs: object) -> Any:
        import io

        value = "kdive-system\n" if path.endswith("/namespace") else "token\n"
        return io.StringIO(value)

    def fake_urlopen(request: Any, **kwargs: object) -> Response:
        requests.append((request, kwargs))
        payload = b'{"subjects": []}' if "/rolebindings/" in request.full_url else b"{}"
        return Response(payload)

    import builtins
    import ssl
    import urllib.request

    monkeypatch.setattr(builtins, "open", fake_open)
    monkeypatch.setattr(ssl, "create_default_context", lambda **_kwargs: object())
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("WORKER_STATEFULSET", "kdive-kdive-worker")
    monkeypatch.setenv("WORKER_REPLICAS", "2")
    monkeypatch.setenv("SCALER_ROLE_BINDING", "kdive-kdive-install-worker-scaler")

    exec(compile(container["args"][0], "install-worker-scaler", "exec"), {})

    assert [request.full_url for request, _kwargs in requests] == [
        "https://kubernetes.default.svc/apis/apps/v1/namespaces/"
        "kdive-system/statefulsets/kdive-kdive-worker/scale",
        "https://kubernetes.default.svc/apis/rbac.authorization.k8s.io/v1/namespaces/"
        "kdive-system/rolebindings/kdive-kdive-install-worker-scaler",
    ]
    assert requests[1][0].data == b'{"subjects": []}'
    assert all(kwargs["timeout"] == 10 for _request, kwargs in requests)


def test_bundled_install_scaler_cleanup_failure_is_diagnosable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docs = _bundled_documents(upgrade=False)
    scaler = next(
        doc
        for doc in docs
        if doc.get("kind") == "Job" and doc["metadata"]["name"].endswith("-worker-scaler")
    )
    script = scaler["spec"]["template"]["spec"]["containers"][0]["args"][0]
    requests: list[Any] = []

    class Response:
        status = 200

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return b"{}"

    def fake_open(path: str, **_kwargs: object) -> Any:
        import io

        return io.StringIO("kdive-system\n" if path.endswith("/namespace") else "token\n")

    def fail_cleanup(request: Any, **_kwargs: object) -> Response:
        requests.append(request)
        if len(requests) == 2:
            raise OSError("API unavailable")
        return Response()

    import builtins
    import ssl
    import urllib.request

    monkeypatch.setattr(builtins, "open", fake_open)
    monkeypatch.setattr(ssl, "create_default_context", lambda **_kwargs: object())
    monkeypatch.setattr(urllib.request, "urlopen", fail_cleanup)
    monkeypatch.setenv("WORKER_STATEFULSET", "kdive-kdive-worker")
    monkeypatch.setenv("WORKER_REPLICAS", "2")
    monkeypatch.setenv("SCALER_ROLE_BINDING", "kdive-kdive-install-worker-scaler")

    recovery = (
        "worker scaled but scaler authority cleanup failed; run: kubectl -n kdive-system "
        "patch rolebinding kdive-kdive-install-worker-scaler"
    )
    with pytest.raises(SystemExit, match=re.escape(recovery)):
        exec(compile(script, "install-worker-scaler", "exec"), {})

    assert len(requests) == 2


def test_bundled_upgrade_migrates_before_workload_rollout() -> None:
    docs = _bundled_documents(upgrade=True)
    worker = next(doc for doc in docs if doc.get("kind") == "StatefulSet")
    jobs = [doc for doc in docs if doc.get("kind") == "Job"]
    migrate = next(doc for doc in jobs if doc["metadata"]["name"].endswith("-migrate"))
    assert worker["spec"]["replicas"] == 2
    assert not any(doc["metadata"]["name"].endswith("-worker-scaler") for doc in jobs)
    phase = migrate["metadata"]["annotations"]["helm.sh/hook"]
    assert "post-install" in phase
    assert "pre-upgrade" in phase
    assert "post-upgrade" not in phase


def test_bundled_runtime_role_bootstrap_runs_after_migration() -> None:
    jobs = _jobs_by_name("bundledBackends=true", "demoAcknowledged=true")
    assert jobs["migrate"]["phase"] == "post-install,pre-upgrade"
    script = jobs["migrate"]["args"][0]
    assert script.index("python -m kdive migrate") < script.index("GRANT {} TO {}")


def test_bundled_role_bootstrap_survives_kubernetes_argument_expansion() -> None:
    jobs = _jobs_by_name("bundledBackends=true", "demoAcknowledged=true")
    script = jobs["migrate"]["args"][0]

    # Kubernetes command/args expansion turns a doubled dollar into one literal dollar before
    # /bin/sh sees the heredoc. A named PostgreSQL delimiter must survive that boundary unchanged.
    assert "DO $$".replace("$$", "$") == "DO $"
    executed_script = script.replace("$$", "$")
    python_source = executed_script.split("python - <<'PY'\n", maxsplit=1)[1].rsplit(
        "\nPY", maxsplit=1
    )[0]
    compile(python_source, "bundled-role-bootstrap", "exec")
    assert '"DO $kdive_role$ BEGIN CREATE ROLE {}' in python_source
    assert "END $kdive_role$" in python_source
    assert "EXCEPTION WHEN duplicate_object THEN NULL" in python_source


def test_external_render_omits_post_install_migrate_hook() -> None:
    # The migrate Job must stay pre-* on the external path (the bundled path runs it post-install
    # after the in-chart DB). Assert on the migrate Job's phase specifically, not a blanket output
    # scan.
    jobs = _jobs_by_name("config.KDIVE_DATABASE_URL=postgresql://x/y")
    assert "post-install" not in (jobs["migrate"]["phase"] or "")
    assert "pre-install" in jobs["migrate"]["phase"]


def test_bundled_path_wires_backends_into_config() -> None:
    res = _template("bundledBackends=true", "demoAcknowledged=true")
    assert res.returncode == 0, res.stderr
    # The demo apps reach the in-chart database through distinct Secret keys.
    secret = next(
        doc
        for doc in yaml.safe_load_all(res.stdout)
        if isinstance(doc, dict)
        and doc.get("kind") == "Secret"
        and doc["metadata"]["name"] == "kdive-database"
    )
    assert set(secret["stringData"]) == {
        "migration-dsn",
        "server-dsn",
        "worker-dsn",
        "reconciler-dsn",
        "lifecycle-witness-dsn",
    }
    assert len(set(secret["stringData"].values())) == 5
    assert 'KDIVE_S3_ENDPOINT_URL: "http://kdive-kdive-minio:9000"' in res.stdout
    assert 'KDIVE_OIDC_ISSUER: "http://kdive-kdive-oidc:8080/default"' in res.stdout
    assert 'KDIVE_OIDC_JWKS_URI: "http://kdive-kdive-oidc:8080/default/jwks"' in res.stdout
    assert "wait-for-db" in res.stdout


def test_external_path_ignores_legacy_db_url_and_omits_demo_creds() -> None:
    res = _template("config.KDIVE_DATABASE_URL=postgresql://ext/db")
    assert res.returncode == 0, res.stderr
    assert "postgresql://ext/db" not in res.stdout
    assert "AWS_ACCESS_KEY_ID" not in res.stdout
    assert "wait-for-db" not in res.stdout


def _hooks_by_kind(*set_args: str) -> dict[str, dict[str, Any]]:
    """Render the chart and index its hook-annotated manifests by Kind.

    Returns ``{kind: {"phase": <hook value>, "weight": <int>}}`` for every doc
    that carries a ``helm.sh/hook`` annotation, so a test can assert the relative
    creation order Helm derives from hook phase + weight.
    """
    res = _template(*set_args)
    assert res.returncode == 0, res.stderr
    out: dict[str, dict[str, Any]] = {}
    for doc in yaml.safe_load_all(res.stdout):
        if not isinstance(doc, dict):
            continue
        annotations = doc.get("metadata", {}).get("annotations") or {}
        hook = annotations.get("helm.sh/hook")
        if hook is None:
            continue
        out[doc["kind"]] = {
            "phase": hook,
            "weight": int(annotations.get("helm.sh/hook-weight", "0")),
        }
    return out


def _jobs_by_name(*set_args: str) -> dict[str, dict[str, Any]]:
    """Index every rendered Job by its metadata.name suffix.

    Returns ``{name_suffix: {"phase", "weight", "volumes", "args"}}``. Name-keyed because the
    chart renders two Jobs (migrate, validate-systems) and the Kind-keyed ``_hooks_by_kind``
    cannot tell them apart.
    """
    res = _template(*set_args)
    assert res.returncode == 0, res.stderr
    jobs: dict[str, dict[str, Any]] = {}
    for doc in yaml.safe_load_all(res.stdout):
        if not (isinstance(doc, dict) and doc.get("kind") == "Job"):
            continue
        name = str(doc.get("metadata", {}).get("name", ""))
        ann = doc.get("metadata", {}).get("annotations", {}) or {}
        spec = doc["spec"]["template"]["spec"]
        container = spec["containers"][0]
        suffix = name.split("-kdive-", 1)[-1] if "-kdive-" in name else name
        jobs[suffix] = {
            "phase": ann.get("helm.sh/hook"),
            "weight": int(ann.get("helm.sh/hook-weight", "0")),
            "volumes": [v["name"] for v in spec.get("volumes", [])],
            "args": container.get("args", []),
            "backoff_limit": doc["spec"].get("backoffLimit"),
            "env_names": [e["name"] for e in container.get("env", [])],
        }
    return jobs


def test_validate_hook_rendered_only_with_systems_configmap() -> None:
    without = _jobs_by_name("config.KDIVE_DATABASE_URL=postgresql://x/y")
    assert "validate-systems" not in without
    with_cm = _jobs_by_name(
        "config.KDIVE_DATABASE_URL=postgresql://x/y", "systems.configMapName=my-systems"
    )
    assert "validate-systems" in with_cm


def test_validate_hook_is_pre_upgrade_weighted_before_migrate() -> None:
    jobs = _jobs_by_name(
        "config.KDIVE_DATABASE_URL=postgresql://x/y", "systems.configMapName=my-systems"
    )
    v = jobs["validate-systems"]
    assert "pre-install" in v["phase"] and "pre-upgrade" in v["phase"]
    assert v["weight"] < jobs["migrate"]["weight"]  # runs before migrate
    assert v["args"][:2] == ["reconcile-systems", "--check"]
    assert "kdive-systems" in v["volumes"]
    # Tolerate transient pod failures like migrate/seed (a bad file still re-fails fast).
    assert v["backoff_limit"] == 3
    # The explicit --path is authoritative; KDIVE_SYSTEMS_TOML must not be set (it would be dead).
    assert "KDIVE_SYSTEMS_TOML" not in v["env_names"]


def test_migrate_job_has_no_systems_volume() -> None:
    # migrate() no longer reads systems.toml (ADR-0121), so the migrate Job must not mount the
    # systems ConfigMap even when one is configured.
    jobs = _jobs_by_name(
        "config.KDIVE_DATABASE_URL=postgresql://x/y", "systems.configMapName=my-systems"
    )
    assert "migrate" in jobs
    assert "kdive-systems" not in jobs["migrate"]["volumes"]


def test_external_configmap_is_a_pre_install_hook_before_migrate() -> None:
    # The migrate Job is a pre-install hook that envFroms the config ConfigMap. Helm
    # creates normal resources only AFTER pre-install hooks, so a normal-resource
    # ConfigMap leaves the migrate pod in CreateContainerConfigError until the hook
    # timeout (issue #311). The ConfigMap must therefore be a pre-install hook too,
    # weighted strictly lower than the migrate Job so Helm creates it first.
    hooks = _hooks_by_kind("config.KDIVE_DATABASE_URL=postgresql://x/y")
    migrate_weight = _jobs_by_name("config.KDIVE_DATABASE_URL=postgresql://x/y")["migrate"][
        "weight"
    ]
    assert "ConfigMap" in hooks, "external-path ConfigMap is not a hook; migrate cannot read it"
    assert "pre-install" in hooks["ConfigMap"]["phase"]
    assert "pre-upgrade" in hooks["ConfigMap"]["phase"]
    assert hooks["ConfigMap"]["weight"] < migrate_weight, (
        "ConfigMap hook-weight must be strictly below the migrate Job's so it is created first"
    )


def test_bundled_configmap_stays_a_normal_resource() -> None:
    # The bundled demo path runs migrate POST-install (after the bundled Postgres is
    # created), so its ConfigMap is already present as a normal resource by then.
    # Turning it into a hook would change the bundled path's behavior for no reason,
    # so the hook annotation must be scoped to the external path only.
    hooks = _hooks_by_kind("bundledBackends=true", "demoAcknowledged=true")
    assert "ConfigMap" not in hooks, "bundled-path ConfigMap should stay a normal resource"


def _workloads(*set_args: str) -> dict[str, dict[str, Any]]:
    """Render the external-backend chart and index each app workload by process name.

    Covers both workload kinds: server/reconciler are Deployments, the worker is a
    StatefulSet (ADR-0514). Extra ``--set`` args layer onto the external-backend base.
    """
    return _rendered_app_workloads("config.KDIVE_DATABASE_URL=postgresql://x/y", *set_args)


def _witness_workload(*set_args: str) -> dict[str, Any]:
    res = _template("config.KDIVE_DATABASE_URL=postgresql://x/y", *set_args)
    assert res.returncode == 0, res.stderr
    for doc in yaml.safe_load_all(res.stdout):
        if not (isinstance(doc, dict) and doc.get("kind") == "Deployment"):
            continue
        if str(doc.get("metadata", {}).get("name", "")).endswith("-witness"):
            return doc
    raise AssertionError("chart rendered no lifecycle-witness Deployment")


def test_lifecycle_witness_enabled_defaults_to_one_and_accepts_false() -> None:
    assert _witness_workload()["spec"]["replicas"] == 1
    assert _witness_workload("lifecycleWitness.enabled=false")["spec"]["replicas"] == 0


def test_staged_fence_render_scales_all_kdive_workloads_to_zero() -> None:
    staged_values = (
        "server.replicas=0",
        "worker.replicas=0",
        "reconciler.replicas=0",
        "lifecycleWitness.enabled=false",
    )
    workloads = _workloads(*staged_values)

    assert {workload["spec"]["replicas"] for workload in workloads.values()} == {0}
    assert _witness_workload(*staged_values)["spec"]["replicas"] == 0


@pytest.mark.parametrize(
    "args",
    [
        ("--set", "lifecycleWitness.enabled=0"),
        ("--set", "lifecycleWitness.enabled=0.5"),
        ("--set-string", "lifecycleWitness.enabled=false"),
        ("--set-json", "lifecycleWitness.enabled=null"),
        ("--set-json", "lifecycleWitness.enabled=1e-400"),
    ],
)
def test_lifecycle_witness_enabled_rejects_non_boolean_values(args: tuple[str, str]) -> None:
    res = _template_args("helm", "template", "kdive", CHART, *args)

    assert res.returncode != 0
    assert "lifecycleWitness.enabled must be a boolean" in res.stderr


def _rendered_app_workloads(*set_args: str) -> dict[str, dict[str, Any]]:
    """Render and index each app workload without assuming a backend mode."""
    res = _template(*set_args)
    return _rendered_app_workloads_from_render(res)


def _rendered_app_workloads_from_render(
    res: subprocess.CompletedProcess[str],
) -> dict[str, dict[str, Any]]:
    assert res.returncode == 0, res.stderr
    out: dict[str, dict[str, Any]] = {}
    for doc in yaml.safe_load_all(res.stdout):
        if not (isinstance(doc, dict) and doc.get("kind") in set(_WORKLOAD_KINDS.values())):
            continue
        name = doc["metadata"]["name"]
        for proc, kind in _WORKLOAD_KINDS.items():
            if name.endswith(f"-{proc}") and doc["kind"] == kind:
                out[proc] = doc
    return out


def _bundled_workloads(*set_args: str) -> dict[str, dict[str, Any]]:
    return _rendered_app_workloads("bundledBackends=true", "demoAcknowledged=true", *set_args)


def _minio_barrier(workload: dict[str, Any]) -> dict[str, Any]:
    init_containers = workload["spec"]["template"]["spec"].get("initContainers", [])
    matches = [item for item in init_containers if item["name"] == "verify-minio-versioning"]
    assert len(matches) == 1
    return matches[0]


def _run_minio_barrier(
    tmp_path: Path, workload: dict[str, Any], reply: str
) -> tuple[int, list[str]]:
    calls = _fake_mc(tmp_path)
    barrier = _minio_barrier(workload)
    assert barrier["command"][:2] == ["/bin/sh", "-c"]
    result = subprocess.run(
        ["/bin/bash", "-c", barrier["command"][2]],
        capture_output=True,
        text=True,
        env={
            **_literal_env(barrier),
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "MC_CALLS": str(calls),
            "MC_BUCKET_ARGS": str(tmp_path / "mc-buckets"),
            "MC_VERSION_INFO": reply,
            "MC_INFO_FAIL": "0",
        },
    )
    return result.returncode, calls.read_text().splitlines()


def _container(workload: dict[str, Any]) -> dict[str, Any]:
    return workload["spec"]["template"]["spec"]["containers"][0]


def test_bundled_app_workloads_share_minio_versioning_startup_barrier() -> None:
    barriers = {proc: _minio_barrier(workload) for proc, workload in _bundled_workloads().items()}
    assert set(barriers) == set(_WORKLOAD_KINDS)
    assert len({barrier["command"][2] for barrier in barriers.values()}) == 1
    for proc, barrier in barriers.items():
        assert barrier["image"].startswith("minio/mc:"), proc
        env = {entry["name"]: entry["value"] for entry in barrier["env"]}
        assert env["MC_CONFIG_DIR"] == "/tmp/.mc", proc
        assert env["MC_BUCKET"] == "kdive-artifacts", proc
        assert set(env) == {"MC_CONFIG_DIR", "MC_USER", "MC_PASS", "MC_BUCKET"}, proc


def test_external_app_workloads_omit_minio_versioning_startup_barrier() -> None:
    for proc, workload in _workloads().items():
        names = {
            item["name"] for item in workload["spec"]["template"]["spec"].get("initContainers", [])
        }
        assert "verify-minio-versioning" not in names, proc


@pytest.mark.parametrize("proc", list(_WORKLOAD_KINDS))
def test_bundled_minio_barrier_allows_only_compatible_policy(tmp_path: Path, proc: str) -> None:
    workload = _bundled_workloads()[proc]
    barrier_env = _literal_env(_minio_barrier(workload))
    compatible = (
        '{"Op":"info","status":"success","url":"local/kdive-artifacts",'
        '"versioning":{"status":"Enabled","MFADelete":""}}'
    )
    returncode, calls = _run_minio_barrier(tmp_path / "ok", workload, compatible)
    assert returncode == 0
    assert calls == [
        "alias set local http://kdive-kdive-minio:9000 "
        f"{barrier_env['MC_USER']} {barrier_env['MC_PASS']}",
        "version info --json local/kdive-artifacts",
    ]

    incompatible = (
        '{"Op":"info","status":"success","url":"local/kdive-artifacts",'
        '"versioning":{"status":"Suspended","MFADelete":""}}'
    )
    returncode, calls = _run_minio_barrier(tmp_path / "bad", workload, incompatible)
    assert returncode != 0
    assert calls[-1] == "version info --json local/kdive-artifacts"


def test_bundled_minio_barrier_uses_configured_bucket(tmp_path: Path) -> None:
    workload = _bundled_workloads("config.KDIVE_S3_BUCKET=custom-artifacts")["server"]
    reply = (
        '{"Op":"info","status":"success","url":"local/custom-artifacts",'
        '"versioning":{"status":"Enabled","MFADelete":"","ExcludedPrefixes":[]}}'
    )
    returncode, calls = _run_minio_barrier(tmp_path, workload, reply)
    assert returncode == 0
    assert calls[-1] == "version info --json local/custom-artifacts"


@pytest.mark.parametrize(("_case", "bucket_pattern"), _SHELL_BUCKET_CASES)
def test_bundled_minio_barrier_passes_bucket_as_literal_data(
    tmp_path: Path, _case: str, bucket_pattern: str
) -> None:
    sentinel = tmp_path / "shell-executed"
    bucket_value = bucket_pattern.format(sentinel=sentinel)
    rendered = _template_bundled_bucket(bucket_value)
    workload = _rendered_app_workloads_from_render(rendered)["server"]
    barrier = _minio_barrier(workload)
    assert bucket_value not in barrier["command"][2]
    assert _literal_env(barrier)["MC_BUCKET"] == bucket_value

    reply = (
        '{"Op":"info","status":"success","url":"local/kdive-artifacts",'
        '"versioning":{"status":"Enabled","MFADelete":""}}'
    )
    returncode, _calls = _run_minio_barrier(tmp_path, workload, reply)
    assert returncode == 0
    assert not sentinel.exists()
    assert (tmp_path / "mc-buckets").read_text().splitlines() == [f"local/{bucket_value}"]


@pytest.mark.parametrize("proc", list(_AUX_PORTS))
def test_workload_carries_its_process_kind(proc: str) -> None:
    # The worker's kind is load-bearing: only a StatefulSet gives each replica its own
    # build/install claim. A silent revert to a Deployment reintroduces #1703.
    #
    # Matched on NAME against every pod-carrying kind, not via _workloads(), which filters on
    # the kind it expects — asserting there could only ever fail with a KeyError, never on the
    # kind itself. The candidate set is deliberately wider than what the chart renders.
    res = _template("config.KDIVE_DATABASE_URL=postgresql://x/y")
    assert res.returncode == 0, res.stderr
    candidates = [
        doc
        for doc in yaml.safe_load_all(res.stdout)
        if isinstance(doc, dict)
        and doc.get("kind") in ("Deployment", "StatefulSet", "DaemonSet", "ReplicaSet")
        and doc["metadata"]["name"] == f"kdive-kdive-{proc}"
    ]
    assert len(candidates) == 1, f"expected exactly one {proc} workload, got {len(candidates)}"
    assert candidates[0]["kind"] == _WORKLOAD_KINDS[proc]


@pytest.mark.parametrize("proc", list(_AUX_PORTS))
def test_workload_binds_aux_listener_to_pod_interface(proc: str) -> None:
    # Each pod runs in its own network namespace; the kubelet probes from the node and a
    # scrape comes from outside the container, so the aux listener binds 0.0.0.0:<port>
    # via an explicit per-deployment KDIVE_HEALTH_BIND_ADDR env (env wins over the shared
    # configMap). No Service fronts the aux port, so it stays pod-local / non-public.
    env = {e["name"]: e.get("value") for e in _container(_workloads()[proc])["env"]}
    assert env["KDIVE_HEALTH_BIND_ADDR"] == f"0.0.0.0:{_AUX_PORTS[proc]}"


@pytest.mark.parametrize("proc", list(_AUX_PORTS))
def test_workload_liveness_probes_livez_on_aux_port(proc: str) -> None:
    # Liveness probes /livez (loop-alive), NOT /readyz: a failing readiness (a backend
    # down) must not let the kubelet kill a live-but-not-ready pod (ADR-0090 §5).
    probe = _container(_workloads()[proc])["livenessProbe"]
    assert probe["httpGet"]["path"] == "/livez"
    assert probe["httpGet"]["port"] == _AUX_PORTS[proc]


@pytest.mark.parametrize("proc", list(_AUX_PORTS))
def test_workload_readiness_probes_readyz_on_aux_port(proc: str) -> None:
    probe = _container(_workloads()[proc])["readinessProbe"]
    assert probe["httpGet"]["path"] == "/readyz"
    assert probe["httpGet"]["port"] == _AUX_PORTS[proc]


@pytest.mark.parametrize("proc", list(_AUX_PORTS))
def test_workload_has_prometheus_scrape_annotations(proc: str) -> None:
    # The pull-based scrape targets /metrics on the aux port (ADR-0090 §5).
    annotations = _workloads()[proc]["spec"]["template"]["metadata"].get("annotations", {})
    assert annotations.get("prometheus.io/scrape") == "true"
    assert annotations.get("prometheus.io/path") == "/metrics"
    assert annotations.get("prometheus.io/port") == str(_AUX_PORTS[proc])


def test_no_service_exposes_an_aux_port() -> None:
    # The aux listener carries no auth; the network boundary is its access control. No Service
    # may front it. The server's MCP Service publishes 8000 only; the worker's governing
    # headless Service (ADR-0514) publishes NO ports at all — declaring 9465 there would give
    # the unauthenticated aux listener a named Service endpoint.
    res = _template("config.KDIVE_DATABASE_URL=postgresql://x/y")
    assert res.returncode == 0, res.stderr
    published: dict[str, set[int]] = {}
    for doc in yaml.safe_load_all(res.stdout):
        if isinstance(doc, dict) and doc.get("kind") == "Service":
            ports = doc["spec"].get("ports") or []
            published[doc["metadata"]["name"]] = {p.get("port") for p in ports}
    assert published == {
        "kdive-kdive-server": {8000},
        "kdive-kdive-worker": set(),
        "kdive-kdive-worker-credential-broker": {9443},
    }
    for name, ports in published.items():
        assert not ports & set(_AUX_PORTS.values()), name


def _service(*set_args: str) -> dict[str, Any]:
    """The server's MCP Service — the only Service in the render that publishes ports."""
    res = _template("config.KDIVE_DATABASE_URL=postgresql://x/y", *set_args)
    assert res.returncode == 0, res.stderr
    return next(
        doc
        for doc in yaml.safe_load_all(res.stdout)
        if isinstance(doc, dict)
        and doc.get("kind") == "Service"
        and str(doc["metadata"]["name"]).endswith("-server")
    )


def test_service_defaults_to_clusterip_without_a_nodeport() -> None:
    svc = _service()
    assert svc["spec"]["type"] == "ClusterIP"
    assert "nodePort" not in svc["spec"]["ports"][0]


def test_service_type_nodeport_lets_the_cluster_assign_the_port() -> None:
    svc = _service("service.type=NodePort")
    assert svc["spec"]["type"] == "NodePort"
    # No pin: the cluster assigns the nodePort, so the chart must not emit one.
    assert "nodePort" not in svc["spec"]["ports"][0]


def test_service_nodeport_can_be_pinned() -> None:
    svc = _service("service.type=NodePort", "service.nodePort=30800")
    assert svc["spec"]["type"] == "NodePort"
    assert svc["spec"]["ports"][0]["nodePort"] == 30800


def test_secrets_unset_mounts_nothing() -> None:
    # The opt-in secret projection (#313) must be inert by default: no mount, no volume, no
    # KDIVE_SECRETS_ROOT, so a deployment that does not need file secrets is unchanged.
    for proc, deploy in _workloads().items():
        container = _container(deploy)
        env_names = {e["name"] for e in container["env"]}
        assert "KDIVE_SECRETS_ROOT" not in env_names, proc
        mounts = {m["name"] for m in container.get("volumeMounts", [])}
        assert "kdive-secrets" not in mounts, proc
        volumes = {v["name"] for v in deploy["spec"]["template"]["spec"].get("volumes", [])}
        assert "kdive-secrets" not in volumes, proc


@pytest.mark.parametrize("proc", list(_AUX_PORTS))
def test_secrets_set_projects_readonly_on_each_component(proc: str) -> None:
    # With secrets.secretName set, every component that resolves file-ref secrets gets the
    # Secret mounted read-only under KDIVE_SECRETS_ROOT (#313): worker/reconciler open the
    # remote-libvirt qemu+tls transport, the server resolves debug-session secrets.
    deploy = _workloads("secrets.secretName=kdive-remote-tls")[proc]
    container = _container(deploy)
    env = {e["name"]: e.get("value") for e in container["env"]}
    assert env["KDIVE_SECRETS_ROOT"] == "/etc/kdive/secrets"
    mount = next(m for m in container["volumeMounts"] if m["name"] == "kdive-secrets")
    assert mount["mountPath"] == "/etc/kdive/secrets"
    assert mount["readOnly"] is True
    volume = next(
        v for v in deploy["spec"]["template"]["spec"]["volumes"] if v["name"] == "kdive-secrets"
    )
    assert volume["secret"]["secretName"] == "kdive-remote-tls"  # pragma: allowlist secret
    # The non-root UID (10001) reads the root-owned Secret files via the pod fsGroup's group
    # bit, so the mode must grant group read (0440, not owner-only 0400) and fsGroup must be set
    # — verified on a real cluster. YAML parses the octal literal 0440 to 288.
    assert volume["secret"]["defaultMode"] == 0o440
    assert deploy["spec"]["template"]["spec"]["securityContext"]["fsGroup"] == 10001


def test_systems_inventory_unset_mounts_nothing() -> None:
    for proc, deploy in _workloads().items():
        container = _container(deploy)
        env_names = {e["name"] for e in container["env"]}
        assert "KDIVE_SYSTEMS_TOML" not in env_names, proc
        mounts = {m["name"] for m in container.get("volumeMounts", [])}
        assert "kdive-systems" not in mounts, proc
        volumes = {v["name"] for v in deploy["spec"]["template"]["spec"].get("volumes", [])}
        assert "kdive-systems" not in volumes, proc


def test_systems_inventory_configmap_mounts_on_components_not_migrate() -> None:
    res = _template("config.KDIVE_DATABASE_URL=postgresql://x/y", "systems.configMapName=inv")
    assert res.returncode == 0, res.stderr
    docs = [doc for doc in yaml.safe_load_all(res.stdout) if isinstance(doc, dict)]

    workloads = _workloads("systems.configMapName=inv")
    for proc in ("server", "worker", "reconciler"):
        deploy = workloads[proc]
        container = _container(deploy)
        env = {e["name"]: e.get("value") for e in container["env"]}
        assert env["KDIVE_SYSTEMS_TOML"] == "/etc/kdive/systems/systems.toml"
        mount = next(m for m in container["volumeMounts"] if m["name"] == "kdive-systems")
        assert mount["mountPath"] == "/etc/kdive/systems"
        assert mount["readOnly"] is True
        volume = next(
            v for v in deploy["spec"]["template"]["spec"]["volumes"] if v["name"] == "kdive-systems"
        )
        assert volume["configMap"]["name"] == "inv"
        assert volume["configMap"]["items"] == [{"key": "systems.toml", "path": "systems.toml"}]

    # ADR-0121: migrate() no longer reads systems.toml, so the migrate Job does not mount the
    # systems ConfigMap or set KDIVE_SYSTEMS_TOML (the validate-systems hook mounts it instead).
    migrate = next(
        doc
        for doc in docs
        if doc.get("kind") == "Job" and doc["metadata"]["name"].endswith("-migrate")
    )
    spec = migrate["spec"]["template"]["spec"]
    container = spec["containers"][0]
    assert all(e["name"] != "KDIVE_SYSTEMS_TOML" for e in container.get("env", []))
    assert not any(m["name"] == "kdive-systems" for m in container.get("volumeMounts", []))
    assert not any(v["name"] == "kdive-systems" for v in spec.get("volumes", []))


def test_fixtures_unset_mounts_nothing() -> None:
    for proc, deploy in _workloads().items():
        container = _container(deploy)
        env_names = {e["name"] for e in container["env"]}
        assert "KDIVE_FIXTURE_CATALOG_PATH" not in env_names, proc
        mounts = {m["name"] for m in container.get("volumeMounts", [])}
        assert "kdive-fixtures" not in mounts, proc
        volumes = {v["name"] for v in deploy["spec"]["template"]["spec"].get("volumes", [])}
        assert "kdive-fixtures" not in volumes, proc


def test_fixtures_configmap_mounts_on_components_not_migrate() -> None:
    res = _template("config.KDIVE_DATABASE_URL=postgresql://x/y", "fixtures.configMapName=fx")
    assert res.returncode == 0, res.stderr
    docs = [doc for doc in yaml.safe_load_all(res.stdout) if isinstance(doc, dict)]

    workloads = _workloads("fixtures.configMapName=fx")
    for proc in ("server", "worker", "reconciler"):
        deploy = workloads[proc]
        container = _container(deploy)
        env = {e["name"]: e.get("value") for e in container["env"]}
        assert env["KDIVE_FIXTURE_CATALOG_PATH"] == "/etc/kdive/fixtures", proc
        mount = next(m for m in container["volumeMounts"] if m["name"] == "kdive-fixtures")
        assert mount["mountPath"] == "/etc/kdive/fixtures"
        assert mount["readOnly"] is True
        volumes = deploy["spec"]["template"]["spec"]["volumes"]
        volume = next(v for v in volumes if v["name"] == "kdive-fixtures")
        assert volume["configMap"]["name"] == "fx"
        assert "items" not in volume["configMap"], "fixtures mount must be flat (no items)"

    migrate = next(doc for doc in docs if doc.get("kind") == "Job")
    mmounts = {m["name"] for m in _container(migrate).get("volumeMounts", [])}
    assert "kdive-fixtures" not in mmounts, "migrate does not read the fixture catalog"


def test_bundled_renders_demo_backends() -> None:
    res = _template("bundledBackends=true", "demoAcknowledged=true")
    assert res.returncode == 0, res.stderr
    for name in ("kdive-kdive-postgres", "kdive-kdive-minio", "kdive-kdive-oidc"):
        assert f"name: {name}\n" in res.stdout, name
    assert "mock-oauth2-server" in res.stdout
    assert "kind: NetworkPolicy" in res.stdout
    # Six Deployments: 3 app (server, reconciler, witness) + 3 demo backends. The
    # worker is the one StatefulSet (ADR-0514).
    assert res.stdout.count("kind: Deployment") == 6
    assert res.stdout.count("kind: StatefulSet") == 1


@pytest.mark.parametrize(("_case", "reply", "expected"), _VERSIONING_REPLIES)
def test_bundled_minio_init_fails_closed_on_bucket_versioning(
    tmp_path: Path, _case: str, reply: str, expected: int
) -> None:
    returncode, calls = _run_minio_init(tmp_path, reply)
    assert (returncode == 0) is (expected == 0), _case
    bucket = "local/kdive-artifacts"
    assert calls[1:] == [
        f"mb --ignore-existing {bucket}",
        f"version enable {bucket}",
        f"version info --json {bucket}",
    ]


def test_bundled_minio_init_uses_configured_bucket(tmp_path: Path) -> None:
    container = _minio_init_container("config.KDIVE_S3_BUCKET=custom-artifacts")
    reply = (
        '{"Op":"info","status":"success","url":"local/custom-artifacts",'
        '"versioning":{"status":"Enabled","MFADelete":""}}'
    )
    returncode, calls = _run_minio_init(tmp_path, reply, container=container)
    assert returncode == 0
    assert calls[1:] == [
        "mb --ignore-existing local/custom-artifacts",
        "version enable local/custom-artifacts",
        "version info --json local/custom-artifacts",
    ]


@pytest.mark.parametrize(("_case", "bucket_pattern"), _SHELL_BUCKET_CASES)
def test_bundled_minio_init_passes_bucket_as_literal_data(
    tmp_path: Path, _case: str, bucket_pattern: str
) -> None:
    sentinel = tmp_path / "shell-executed"
    bucket_value = bucket_pattern.format(sentinel=sentinel)
    container = _minio_init_container_from_render(_template_bundled_bucket(bucket_value))
    assert bucket_value not in container["command"][2]
    assert _literal_env(container)["MC_BUCKET"] == bucket_value

    reply = (
        '{"Op":"info","status":"success","url":"local/kdive-artifacts",'
        '"versioning":{"status":"Enabled","MFADelete":""}}'
    )
    returncode, _calls = _run_minio_init(tmp_path, reply, container=container)
    assert returncode == 0
    assert not sentinel.exists()
    assert (tmp_path / "mc-buckets").read_text().splitlines() == [f"local/{bucket_value}"] * 3


def test_bundled_minio_init_propagates_version_info_command_failure(tmp_path: Path) -> None:
    returncode, calls = _run_minio_init(tmp_path, "", info_fails=True)
    assert returncode != 0
    assert calls[-1] == "version info --json local/kdive-artifacts"


def test_external_path_has_no_demo_backends() -> None:
    res = _template("config.KDIVE_DATABASE_URL=postgresql://x/y")
    assert res.returncode == 0, res.stderr
    assert "mock-oauth2-server" not in res.stdout
    assert res.stdout.count("kind: NetworkPolicy") == 1
    assert res.stdout.count("kind: Deployment") == 3
    assert res.stdout.count("kind: StatefulSet") == 1


def test_bundled_oidc_pins_audience_kdive() -> None:
    res = _template("bundledBackends=true", "demoAcknowledged=true")
    assert res.returncode == 0, res.stderr
    assert '"aud":["kdive"]' in res.stdout


def test_bundled_oidc_mints_role_claims() -> None:
    # The default demo claim set must carry a usable RBAC grant so a stock demo deploy can
    # exercise the authz surface (#369): admin on the seeded `demo` project + all three
    # platform roles. toJson sorts keys, so these JSON substrings are stable.
    res = _template("bundledBackends=true", "demoAcknowledged=true")
    assert res.returncode == 0, res.stderr
    assert '"projects":["demo"]' in res.stdout
    assert '"roles":{"demo":"admin"}' in res.stdout
    for role in ("platform_admin", "platform_operator", "platform_auditor"):
        assert f'"{role}"' in res.stdout, role


def test_bundled_oidc_claims_value_is_wired() -> None:
    # An operator narrows the grant to test a denial; --set deep-merges into the default
    # map, overriding only the targeted leaf and leaving the other claims defaulted.
    res = _template(
        "bundledBackends=true",
        "demoAcknowledged=true",
        "demo.oidc.claims.roles.demo=viewer",
    )
    assert res.returncode == 0, res.stderr
    assert '"roles":{"demo":"viewer"}' in res.stdout
    assert '"roles":{"demo":"admin"}' not in res.stdout
    # The other defaults survive the targeted override (deep-merge, not replace).
    assert '"projects":["demo"]' in res.stdout
    assert '"platform_admin"' in res.stdout
    assert '"aud":["kdive"]' in res.stdout


def test_bundled_oidc_aud_pin_survives_override() -> None:
    # `aud` is a template invariant, not a value: an operator override can never break
    # audience verification and lock the demo out.
    res = _template(
        "bundledBackends=true",
        "demoAcknowledged=true",
        "demo.oidc.claims.aud=nope",
    )
    assert res.returncode == 0, res.stderr
    assert '"aud":["kdive"]' in res.stdout
    assert "nope" not in res.stdout


def test_bundled_oidc_mints_role_scoped_variants() -> None:
    # ADR-0108 §4: per-role client_id mappings let `demo-token.sh --role <role>` mint a
    # narrowed token (project role only, no platform roles) so a denial is reachable without
    # a chart redeploy. toJson sorts keys, so each variant's full claims object is a stable
    # substring — pinning it proves the variant carries NO platform_roles.
    res = _template("bundledBackends=true", "demoAcknowledged=true")
    assert res.returncode == 0, res.stderr
    assert '"match":"kdive-demo-viewer","requestParam":"client_id"' in res.stdout
    assert '"match":"kdive-demo-operator","requestParam":"client_id"' in res.stdout
    assert (
        '{"aud":["kdive"],"projects":["demo"],"roles":{"demo":"viewer"},"sub":"kdive-demo-viewer"}'
        in res.stdout
    )
    assert (
        '{"aud":["kdive"],"projects":["demo"],"roles":{"demo":"operator"},"sub":"kdive-demo-operator"}'
        in res.stdout
    )


def test_bundled_oidc_variant_mappings_precede_catch_all() -> None:
    # The feature's load-bearing invariant: every per-role client_id mapping must come BEFORE
    # the grant_type:"*" catch-all. mock-oauth2-server is first-match-wins, so if a reorder
    # ever put the catch-all first, a client_id=kdive-demo-viewer request would match it and
    # silently mint a FULL-ADMIN token — a privilege escalation for a request that asked for
    # less. A presence-only assertion can't catch that; pin the order explicitly.
    res = _template("bundledBackends=true", "demoAcknowledged=true")
    assert res.returncode == 0, res.stderr
    mappings = _oidc_request_mappings(res)
    catch_all_idx = next(
        i for i, m in enumerate(mappings) if m["requestParam"] == "grant_type" and m["match"] == "*"
    )
    client_id_idxs = [i for i, m in enumerate(mappings) if m["requestParam"] == "client_id"]
    assert client_id_idxs, "expected per-role client_id mappings"
    assert max(client_id_idxs) < catch_all_idx, (
        "a client_id variant mapping is at/after the catch-all; first-match-wins would mint "
        "an admin token for a narrowed-role request"
    )
    # The catch-all is the only mapping carrying platform_roles (the full admin grant); the
    # variants must not, or the denial they exist to demonstrate would not occur.
    for m in mappings:
        if m["requestParam"] == "client_id":
            assert "platform_roles" not in m["claims"], m["match"]


def test_demo_token_script_client_ids_match_rendered_variants() -> None:
    # The script (scripts/demo-token.sh) and the chart hold the client_id literals in two
    # places; if they drift (e.g. the template prefix changes), `--role viewer` sends an
    # unmatched client_id that falls through to the admin catch-all and silently mints a full
    # token. Pin that every non-admin role the script can request maps to a client_id the
    # chart actually registers as a variant.
    res = _template("bundledBackends=true", "demoAcknowledged=true")
    assert res.returncode == 0, res.stderr
    rendered_variant_ids = {
        m["match"] for m in _oidc_request_mappings(res) if m["requestParam"] == "client_id"
    }

    script = (Path(CHART).resolve().parents[2] / "scripts" / "demo-token.sh").read_text()
    # Lines like: `viewer) client_id="kdive-demo-viewer" ;;`
    script_map = {
        m.group(1): m.group(2)
        for m in re.finditer(r'^(\w+)\)\s*client_id="([^"]+)"', script, re.MULTILINE)
    }
    narrowed = {cid for role, cid in script_map.items() if role != "admin"}
    assert narrowed, "no non-admin client_id literals parsed from demo-token.sh"
    missing = narrowed - rendered_variant_ids
    assert not missing, f"script client_ids with no chart variant mapping (drift): {missing}"

    # The symmetric hole: `admin` must resolve via the grant_type catch-all, NOT a variant.
    # If it ever pointed at a variant client_id, `--role admin` would silently mint a narrowed
    # token and break the default full grant every first-run flow relies on.
    assert script_map.get("admin") not in rendered_variant_ids, (
        "demo-token.sh maps --role admin to a variant client_id; it must use the catch-all"
    )


def test_exposed_minio_networkpolicy_ingress_scope() -> None:
    # The actual exposure control (the NOTES warning only mirrors this in prose, and NOTES can't
    # render offline so it isn't asserted here). A ClusterIP store renders no companion policy;
    # exposing it with the empty default opens :9000 to the world (0.0.0.0/0); a scoped override
    # opens only that CIDR. Pins both the footgun default and that scoping actually narrows it.
    base = ("bundledBackends=true", "demoAcknowledged=true")

    clusterip = _template(*base)
    assert clusterip.returncode == 0, clusterip.stderr
    assert _minio_ext_cidrs(clusterip) is None, "ClusterIP store must render no -minio-ext policy"

    exposed = _template(*base, "demo.minio.service.type=LoadBalancer")
    assert exposed.returncode == 0, exposed.stderr
    assert _minio_ext_cidrs(exposed) == ["0.0.0.0/0"], "empty sourceRanges must open to the world"

    scoped = _template(
        *base,
        "demo.minio.service.type=LoadBalancer",
        "demo.minio.service.sourceRanges={192.168.16.0/24}",
    )
    assert scoped.returncode == 0, scoped.stderr
    cidrs = _minio_ext_cidrs(scoped)
    assert cidrs == ["192.168.16.0/24"], f"scoped override must narrow the ingress, got {cidrs}"


def test_bundled_oidc_blanked_claims_degrade_to_floor() -> None:
    # Blanking the claims map (a plausible "token with no grant" override) must render the
    # safe {sub,aud} floor, not a nil-pointer template error. With no role grant the
    # role-scoped variants are suppressed (there is nothing to downgrade), so the catch-all
    # mapping is the only one rendered.
    res = _template("bundledBackends=true", "demoAcknowledged=true", "demo.oidc.claims=null")
    assert res.returncode == 0, res.stderr
    assert '"aud":["kdive"]' in res.stdout
    assert '"sub":"kdive-demo"' in res.stdout
    assert '"roles"' not in res.stdout
    assert '"requestParam":"client_id"' not in res.stdout


def test_bundled_demo_services_are_clusterip() -> None:
    res = _template("bundledBackends=true", "demoAcknowledged=true")
    assert res.returncode == 0, res.stderr
    for doc in yaml.safe_load_all(res.stdout):
        if isinstance(doc, dict) and doc.get("kind") == "Service":
            assert doc["spec"].get("type", "ClusterIP") == "ClusterIP", doc["metadata"]["name"]


def test_bundled_with_nodeport_is_rejected() -> None:
    res = _template("bundledBackends=true", "demoAcknowledged=true", "service.type=NodePort")
    assert res.returncode != 0
    assert "service.type must stay ClusterIP" in res.stderr


def test_bundled_has_a_helm_test_hook() -> None:
    hooks = _hooks_by_kind("bundledBackends=true", "demoAcknowledged=true")
    assert hooks.get("Pod", {}).get("phase") == "test"


def test_lint_is_clean() -> None:
    res = subprocess.run(
        ["helm", "lint", CHART],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "0 chart(s) failed" in res.stdout


# --- Worker scale-out: per-replica scratch volumes (#1703, ADR-0514) ----------------------


def _worker_workload(*set_args: str) -> dict[str, Any]:
    """The rendered doc carrying the worker pod template, whatever workload kind it is.

    Deliberately kind-agnostic (unlike :func:`_workloads`, which pins the kind) so the
    guards below stay meaningful against a chart that spells the worker as a Deployment —
    which is exactly the shape #1703 describes.
    """
    res = _template("config.KDIVE_DATABASE_URL=postgresql://x/y", *set_args)
    assert res.returncode == 0, res.stderr
    return next(
        doc
        for doc in yaml.safe_load_all(res.stdout)
        if isinstance(doc, dict)
        and doc.get("kind") in ("Deployment", "StatefulSet")
        and str(doc["metadata"]["name"]).endswith("-worker")
    )


def _worker_shared_claim_names(*set_args: str) -> set[str]:
    """PVC names the worker pod template mounts by fixed ``claimName``.

    Every such entry is one claim mounted into EVERY replica: a pod-template
    ``persistentVolumeClaim`` volume names a single claim, so two replicas either
    multi-attach it (different nodes, ReadWriteOnce, Pending) or share the filesystem
    (same node). ``volumeClaimTemplates`` produces per-replica claims and never appears
    here, so this set must be empty.
    """
    volumes = _worker_workload(*set_args)["spec"]["template"]["spec"].get("volumes") or []
    return {
        v["persistentVolumeClaim"]["claimName"] for v in volumes if "persistentVolumeClaim" in v
    }


@pytest.mark.parametrize("replicas", [1, 2, 5])
def test_scaled_worker_mounts_no_shared_claim(replicas: int) -> None:
    # The #1703 regression guard. Asserted at every replica count, including 1: the old chart
    # mounted the same two ReadWriteOnce claims by name regardless, so the breakage was latent
    # at 1 and only surfaced when the scheduler placed a second pod.
    shared = _worker_shared_claim_names(f"worker.replicas={replicas}")
    assert shared == set(), f"worker replicas share fixed claims across replicas: {sorted(shared)}"


def test_worker_scratch_comes_from_per_replica_claim_templates() -> None:
    # The positive half: with no shared claims, the build/install mounts must still resolve —
    # to volumeClaimTemplates, which Kubernetes instantiates once per ordinal.
    sts = _worker_workload("worker.replicas=2")
    assert sts["kind"] == "StatefulSet"
    assert sts["spec"]["replicas"] == 2
    templates = {t["metadata"]["name"]: t for t in sts["spec"]["volumeClaimTemplates"]}
    assert set(templates) == {"build", "install"}
    for name, template in templates.items():
        assert template["spec"]["accessModes"] == ["ReadWriteOnce"], name
        assert template["spec"]["resources"]["requests"]["storage"], name
    mounts = {m["name"]: m["mountPath"] for m in _container(sts)["volumeMounts"]}
    assert mounts["build"] == "/var/lib/kdive/build"
    assert mounts["install"] == "/var/lib/kdive/install"


def test_chart_renders_no_standalone_worker_pvc() -> None:
    # The retired pvc-worker.yaml declared two release-scoped ReadWriteOnce PVCs. A standalone
    # PVC is by construction shared by every replica that names it, so the chart must render none.
    res = _template("config.KDIVE_DATABASE_URL=postgresql://x/y", "worker.replicas=2")
    assert res.returncode == 0, res.stderr
    standalone = [
        doc["metadata"]["name"]
        for doc in yaml.safe_load_all(res.stdout)
        if isinstance(doc, dict) and doc.get("kind") == "PersistentVolumeClaim"
    ]
    assert standalone == [], standalone


def test_worker_claim_templates_carry_no_version_dependent_metadata() -> None:
    # volumeClaimTemplates is immutable after install: the API server rejects an update to any
    # StatefulSet field outside replicas/template/updateStrategy/minReadySeconds/ordinals and the
    # retention policy. `kdive.labels` embeds helm.sh/chart (the chart version), so labelling the
    # templates would make every chart bump fail `helm upgrade` with an immutable-field error.
    sts = _worker_workload()
    for template in sts["spec"]["volumeClaimTemplates"]:
        assert set(template["metadata"]) == {"name"}, template["metadata"]


def test_worker_statefulset_is_governed_by_a_headless_service() -> None:
    # serviceName must name a Service that exists, or the pods get no stable DNS identity.
    res = _template("config.KDIVE_DATABASE_URL=postgresql://x/y")
    assert res.returncode == 0, res.stderr
    docs = [d for d in yaml.safe_load_all(res.stdout) if isinstance(d, dict)]
    sts = next(d for d in docs if d["kind"] == "StatefulSet")
    svc = next(
        d
        for d in docs
        if d["kind"] == "Service" and d["metadata"]["name"] == sts["spec"]["serviceName"]
    )
    assert svc["spec"]["clusterIP"] == "None"
    assert svc["spec"]["selector"] == sts["spec"]["selector"]["matchLabels"]


def test_worker_replicas_default_to_two() -> None:
    # The job queue is built for parallel workers (FOR UPDATE SKIP LOCKED, ADR-0018); a
    # one-worker default never exercises that path in any stock deploy.
    assert _worker_workload()["spec"]["replicas"] == 2


def test_worker_pods_start_in_parallel() -> None:
    # Workers are interchangeable and claim disjoint rows, so OrderedReady's one-at-a-time
    # start/stop buys nothing and serializes every scale and rollout.
    assert _worker_workload()["spec"]["podManagementPolicy"] == "Parallel"


def test_worker_claims_are_released_on_scale_down_and_uninstall() -> None:
    # build/install are scratch. Retaining them would strand a full set of PVCs per removed
    # ordinal, which a scale-up would repopulate from the object store anyway.
    policy = _worker_workload()["spec"]["persistentVolumeClaimRetentionPolicy"]
    assert policy == {"whenScaled": "Delete", "whenDeleted": "Delete"}


# --- Bundled observability (opt-in Prometheus, ADR-0189) ----------------------------------


def _obs_docs(*set_args: str) -> list[dict[str, Any]]:
    """Render with bundledObservability on and return every YAML doc."""
    res = _template(
        "config.KDIVE_DATABASE_URL=postgresql://x/y", "bundledObservability=true", *set_args
    )
    assert res.returncode == 0, res.stderr
    return [d for d in yaml.safe_load_all(res.stdout) if isinstance(d, dict)]


def _obs_kind(docs: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    return next(d for d in docs if d.get("kind") == kind and "prometheus" in d["metadata"]["name"])


def _scrape_config(docs: list[dict[str, Any]]) -> dict[str, Any]:
    cm = next(
        d
        for d in docs
        if d.get("kind") == "ConfigMap" and d["metadata"]["name"].endswith("-prometheus-config")
    )
    return yaml.safe_load(cm["data"]["prometheus.yml"])


def test_observability_off_by_default_renders_no_prometheus() -> None:
    res = _template("config.KDIVE_DATABASE_URL=postgresql://x/y")
    assert res.returncode == 0, res.stderr
    assert "-prometheus" not in res.stdout


def test_observability_renders_rbac_deployment_and_clusterip_service() -> None:
    docs = _obs_docs()
    assert _obs_kind(docs, "ServiceAccount")
    role = _obs_kind(docs, "Role")
    rules = role["rules"][0]
    assert rules["resources"] == ["pods"]
    assert sorted(rules["verbs"]) == ["get", "list", "watch"]
    assert _obs_kind(docs, "RoleBinding")
    svc = _obs_kind(docs, "Service")
    assert svc["spec"]["type"] == "ClusterIP"
    assert {p["port"] for p in svc["spec"]["ports"]} == {9090}
    dep = _obs_kind(docs, "Deployment")
    assert dep["spec"]["template"]["spec"]["serviceAccountName"].endswith("-prometheus")


def test_observability_scrape_config_uses_annotation_relabeling() -> None:
    # helm template / yaml.safe_load_all validate only the wrapping ConfigMap; the relabel DSL
    # inside data["prometheus.yml"] is the load-bearing logic, so parse and assert on it directly
    # (a valid-but-wrong relabel rule would render fine and scrape nothing).
    cfg = _scrape_config(_obs_docs())
    job = cfg["scrape_configs"][0]
    sd = job["kubernetes_sd_configs"][0]
    assert sd["role"] == "pod"
    assert sd["namespaces"]["names"] == ["default"]  # helm template's default release namespace
    rels = job["relabel_configs"]
    keep = next(r for r in rels if r.get("action") == "keep")
    assert keep["source_labels"] == ["__meta_kubernetes_pod_annotation_prometheus_io_scrape"]
    assert keep["regex"] == "true"
    path = next(r for r in rels if r.get("target_label") == "__metrics_path__")
    assert path["source_labels"] == ["__meta_kubernetes_pod_annotation_prometheus_io_path"]
    addr = next(r for r in rels if r.get("target_label") == "__address__")
    assert "__meta_kubernetes_pod_annotation_prometheus_io_port" in addr["source_labels"]
    assert "__meta_kubernetes_pod_ip" in addr["source_labels"]


def test_observability_independent_of_bundled_backends() -> None:
    # Renders on the external path (no bundledBackends) — the targets are the app pods, which
    # exist on both paths, so observability must not pull in the demo backends.
    docs = _obs_docs()
    assert _obs_kind(docs, "Deployment")
    assert "mock-oauth2-server" not in yaml.safe_dump_all(docs)


def test_observability_scrape_config_passes_promtool() -> None:
    # Bonus semantic check: promtool is the only thing that validates the Prometheus relabel DSL
    # the chart renders. Skips cleanly when absent (it is not on every runner), like the helm gate.
    if shutil.which("promtool") is None:
        pytest.skip("promtool not installed")
    import tempfile

    cfg = _scrape_config(_obs_docs())
    with tempfile.NamedTemporaryFile("w", suffix=".yml") as fh:
        yaml.safe_dump(cfg, fh)
        fh.flush()
        res = subprocess.run(
            ["promtool", "check", "config", fh.name],
            capture_output=True,
            text=True,
            check=False,
        )
    assert res.returncode == 0, res.stdout + res.stderr
