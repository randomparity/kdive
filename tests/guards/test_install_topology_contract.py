"""Operator documentation must describe each deployment's actual process topology."""

import re
import subprocess
from pathlib import Path

import pytest

from kdive.config.registry import RUNNABLE

_ROOT = Path(__file__).parents[2]
_INSTALL = _ROOT / "docs/operating/install.md"
_COMPOSE_DOC = _ROOT / "docs/operating/docker-compose.md"
_COMPOSE_REFERENCE = _ROOT / "deploy/compose/README.md"
_HELM_REFERENCE = _ROOT / "deploy/helm/kdive/README.md"
_KUBERNETES_DOC = _ROOT / "docs/operating/kubernetes.md"
_KUBERNETES_RUNBOOK = _ROOT / "docs/operating/runbooks/kubernetes-deploy.md"
_BUILD_USE_RECOVERY = _ROOT / "docs/operating/runbooks/build-use-recovery.md"
_HELM_VALUES = _ROOT / "deploy/helm/kdive/values.yaml"

_IMAGE_COMMAND_INVENTORY = re.compile(
    r"The image runs any of five commands \((?P<commands>[^)]+)\) via `python -m kdive <command>`\."
)


def _normalized(path: Path) -> str:
    return " ".join(path.read_text().split())


def _section(path: Path, start: str, end: str | None = None) -> str:
    text = path.read_text().split(start, maxsplit=1)[1]
    if end is not None:
        text = text.split(end, maxsplit=1)[0]
    return " ".join(text.split()).lower()


def test_install_documents_the_five_image_commands_and_kubernetes_witness_topology() -> None:
    text = _normalized(_INSTALL)

    inventory = _IMAGE_COMMAND_INVENTORY.search(text)
    assert inventory is not None
    assert frozenset(re.findall(r"`([^`]+)`", inventory.group("commands"))) == RUNNABLE
    assert "`lifecycle-witness` is Kubernetes-only" in text
    assert (
        "Kubernetes runs the dedicated `lifecycle-witness` as its fourth long-running workload."
        in text
    )
    assert "four entrypoints" not in text
    assert "three KDIVE Deployments" not in text


def test_compose_documents_its_operator_side_lifecycle_wrapper_not_a_witness_service() -> None:
    for path in (_COMPOSE_DOC, _COMPOSE_REFERENCE):
        text = path.read_text()
        assert "operator-side lifecycle wrapper" in text, path
        assert "start the lifecycle witness" not in text, path
        assert "start the lifecycle witnesses" not in text, path


def test_public_compose_guides_document_stop_and_destructive_teardown() -> None:
    for path in (_COMPOSE_DOC, _COMPOSE_REFERENCE):
        text = _normalized(path)

        assert "four supported worker lifecycle recipes" in text, path
        for recipe in (
            "`just compose-up`",
            "`just compose-stop`",
            "`just compose-recreate-worker`",
            "`just compose-down`",
        ):
            assert recipe in text, (path, recipe)
        assert "`just compose-stop` preserves named volumes" in text, path
        assert "`just compose-down` removes named volumes" in text, path


@pytest.mark.parametrize(
    ("path", "start", "end"),
    [
        (_COMPOSE_DOC, "## Upgrading worker-fence authority", "The Compose-managed bucket"),
        (_COMPOSE_REFERENCE, "## Upgrading worker-fence authority", "`docker compose up` resolves"),
        (_INSTALL, "- **Compose:**", "Verify registered"),
        (_BUILD_USE_RECOVERY, "- **Compose:**", "Verify registered"),
    ],
)
def test_compose_worker_fence_guidance_uses_the_public_stop_workflow(
    path: Path, start: str, end: str
) -> None:
    section = _section(path, start, end)
    ordered_steps = (
        "just compose-stop",
        "select the new image and configuration",
        "just compose-up",
        "migrate one-shot",
    )

    positions = []
    for step in ordered_steps:
        assert step in section, (path, step)
        positions.append(section.index(step))
    assert positions == sorted(positions), path
    assert "raw docker/compose commands" in section, path
    assert (
        "do not invoke `python -m kdive.processes.compose_worker_lifecycle` directly" in section
    ), path


@pytest.mark.parametrize(
    ("path", "start", "end"),
    [
        (_COMPOSE_DOC, "## Upgrading worker-fence authority", "The Compose-managed bucket"),
        (_COMPOSE_REFERENCE, "## Upgrading worker-fence authority", "`docker compose up` resolves"),
        (_INSTALL, "- **Compose:**", "Verify registered"),
        (_BUILD_USE_RECOVERY, "- **Compose:**", "Verify registered"),
    ],
)
def test_compose_worker_fence_guidance_scopes_local_bootstrap(
    path: Path, start: str, end: str
) -> None:
    section = _section(path, start, end)

    assert "local-bootstrap-only" in section, path
    assert "kdive_local_role_bootstrap=1" in section, path
    assert "resets fixed local development passwords" in section, path
    assert "restores the intended runtime-role memberships" in section, path
    disabled_setting = section.index("kdive_local_role_bootstrap=0")
    assert "disables local mutation" in section[disabled_setting:], path
    assert (
        "equivalent stop-old, migrate, provision credentials and memberships, and start gate"
        in section
    ), path
    assert "outside this reference workflow" in section, path
    assert "rotates" not in section, path


def test_compose_reference_describes_fixed_local_bootstrap_credentials() -> None:
    text = _normalized(_COMPOSE_REFERENCE)

    assert "resets their fixed local development passwords" in text
    assert "restores each intended runtime-role membership" in text
    assert "rotates their local-only secrets" not in text


def test_helm_confines_witness_credentials_to_its_dedicated_workload() -> None:
    text = _normalized(_HELM_REFERENCE)

    assert "confined to the dedicated lifecycle-witness workload" in text
    assert "confined to the reconciler" not in text


def test_kubernetes_page_counts_the_dedicated_witness_as_a_long_running_workload() -> None:
    text = _KUBERNETES_DOC.read_text()

    assert "four long-running Kubernetes workloads" in text


def test_helm_values_confine_credential_broker_authority_to_the_witness() -> None:
    text = _normalized(_HELM_VALUES)

    assert "Lifecycle-witness-only" in text
    assert (
        "lifecycle-witness DSN is declared separately at databaseCredentials.lifecycleWitness"
        in text
    )
    assert "reconciler receives none" in text


def test_kubernetes_monitoring_documents_the_witness_target() -> None:
    for path in (_HELM_REFERENCE, _KUBERNETES_RUNBOOK):
        text = _normalized(path)
        assert "9467" in text, path
        assert "all four" in text, path


def test_bundled_observability_counts_all_four_components() -> None:
    text = _normalized(_HELM_VALUES)

    assert "scrapes all four components" in text


def test_helm_podmonitor_explains_three_non_listening_ports() -> None:
    text = _normalized(_HELM_REFERENCE)

    assert "three ports a given pod does not listen on" in text


def test_build_use_recovery_distinguishes_kubernetes_witness_from_compose_wrapper() -> None:
    text = _normalized(_BUILD_USE_RECOVERY)
    compose_guidance = text.split("**Compose:**", maxsplit=1)[1].split(
        "Verify registered current incarnations", maxsplit=1
    )[0]

    assert "**Kubernetes:**" in text
    assert "staged worker-fence upgrade procedure" in text
    assert "**Compose:**" in text
    assert "operator-side lifecycle wrapper" in text
    assert compose_guidance.lower().index("just compose-stop") < compose_guidance.lower().index(
        "migrate one-shot"
    )


_STAGED_UPGRADE_MARKERS = (
    "#### Stage 1 — Capture restartable recovery state",
    "#### Stage 2 — Drain workers through the current witness",
    "#### Stage 3 — Stop all workloads and prove migration safety",
    "#### Stage 4 — Run the hooked all-zero target migration",
    "#### Stage 5 — Correct target credential content",
    "#### Stage 6 — Start or refresh only the witness",
    "#### Stage 7 — Restore captured core counts",
    "#### Stage 8 — Prove restored worker and recovery authority",
)


def _staged_upgrade_stages() -> tuple[str, dict[str, str]]:
    source = _KUBERNETES_RUNBOOK.read_text()

    assert "### Staged worker-fence upgrade" in source
    staged = source.split("### Staged worker-fence upgrade", maxsplit=1)[1].split(
        "**Validate", maxsplit=1
    )[0]
    positions = [staged.index(marker) for marker in _STAGED_UPGRADE_MARKERS]
    assert positions == sorted(positions)
    assert all(staged.count(marker) == 1 for marker in _STAGED_UPGRADE_MARKERS)
    boundaries = [*positions, len(staged)]
    stages = {
        marker: staged[boundaries[index] : boundaries[index + 1]]
        for index, marker in enumerate(_STAGED_UPGRADE_MARKERS)
    }
    return staged, stages


def test_canonical_staged_helm_upgrade_stages_one_to_four_are_restart_safe() -> None:
    staged, stages = _staged_upgrade_stages()

    stage_1 = stages[_STAGED_UPGRADE_MARKERS[0]]
    for phrase in (
        "set -euo pipefail",
        ': "${NAMESPACE:?set the target namespace}"',
        "umask 077",
        'test ! -e "$TARGET_VALUES"',
        'chmod 0600 "$TARGET_VALUES_TMP"',
        'helm template "$RELEASE" "$CHART" -n "$NAMESPACE" -f "$TARGET_VALUES"',
        "--show-only templates/job-migrate.yaml",
        "TARGET_IMAGE",
        "MIGRATION_SECRET",
        "MIGRATION_KEY",
        "RECOVERY_STATE",
        "validate_nonnegative_count",
        "validate_short_name",
        'DB_CLIENT_JOB="${FULL}-fence-db-check"',
        'INCARNATION_JOB="${FULL}-fence-worker-check"',
        "%q",
        'bash -n "$RECOVERY_STATE_TMP"',
        'mv -- "$RECOVERY_STATE_TMP" "$RECOVERY_STATE_FILE"',
        "kubectl create configmap",
        "verify_recovery_configmap",
        "${FULL}-fence-upgrade",
    ):
        assert phrase in stage_1, phrase
    assert "kubectl apply" not in stage_1
    assert "CURRENT_IMAGE" not in staged
    assert "NAMESPACE=<namespace>" not in staged

    stage_2 = stages[_STAGED_UPGRADE_MARKERS[1]]
    for phrase in (
        'source "$RECOVERY_STATE_FILE"',
        'test "$CM_SERVER_REPLICAS" = "$SERVER_REPLICAS"',
        'test "$SERVER_REPLICAS" -eq 0',
        "--replicas=1",
        "timeout 60s uv run kdivectl --json ops set-queue-paused --paused",
        "kubectl scale statefulset/${FULL}-worker",
        "wait_for_pods_deleted",
        "--timeout=5m",
    ):
        assert phrase in stage_2, phrase
    assert stage_2.index("set-queue-paused --paused") < stage_2.index(
        "scale statefulset/${FULL}-worker"
    )

    stage_3 = stages[_STAGED_UPGRADE_MARKERS[2]]
    for phrase in (
        'source "$RECOVERY_STATE_FILE"',
        "activeDeadlineSeconds: 60",
        "secretKeyRef:",
        "name: ${MIGRATION_SECRET}",
        "key: ${MIGRATION_KEY}",
        'image: "${TARGET_IMAGE}"',
        "pid <> pg_backend_pid()",
        "backend_type = 'client backend'",
        'RETRY_DIAGNOSTIC="${RETRY_DIAGNOSTIC:-}"',
        'kubectl delete job "$DB_CLIENT_JOB"',
        "--ignore-not-found --wait=true --timeout=2m",
        "kubectl logs job/${DB_CLIENT_JOB}",
        "--timeout=75s",
    ):
        assert phrase in stage_3, phrase
    assert "OPERATOR_DATABASE_URL" not in staged
    assert 'psql "$OPERATOR_DATABASE_URL"' not in staged

    stage_4 = stages[_STAGED_UPGRADE_MARKERS[3]]
    assert 'source "$RECOVERY_STATE_FILE"' in stage_4
    assert "lifecycleWitness.enabled=false" in stage_4
    assert "--no-hooks" not in "\n".join(re.findall(r"```bash\n(.*?)```", stage_4, flags=re.DOTALL))


def test_canonical_staged_helm_upgrade_stages_five_to_seven_restore_safely() -> None:
    _, stages = _staged_upgrade_stages()

    stage_5 = stages[_STAGED_UPGRADE_MARKERS[4]]
    assert 'source "$RECOVERY_STATE_FILE"' in stage_5
    assert "bundled backends" in stage_5.lower()
    assert "external backends" in stage_5.lower()

    stage_6 = stages[_STAGED_UPGRADE_MARKERS[5]]
    for phrase in (
        'source "$RECOVERY_STATE_FILE"',
        "--no-hooks",
        "lifecycleWitness.enabled=true",
        'kubectl delete pod "$WITNESS_POD"',
        "--wait=true",
        "kubectl rollout status deployment/${FULL}-witness",
        "kubectl wait --for=condition=Ready pod",
        "--timeout=5m",
    ):
        assert phrase in stage_6, phrase
    assert "rollout restart" not in stage_6

    stage_7 = stages[_STAGED_UPGRADE_MARKERS[6]]
    for phrase in (
        'source "$RECOVERY_STATE_FILE"',
        "kubectl get configmap",
        "validate_nonnegative_count",
        'test "$CM_SERVER_REPLICAS" = "$SERVER_REPLICAS"',
        'test "$CM_WORKER_REPLICAS" = "$WORKER_REPLICAS"',
        'test "$CM_RECONCILER_REPLICAS" = "$RECONCILER_REPLICAS"',
        "VERIFY_SERVER_REPLICAS=1",
        "--no-hooks",
        "server.replicas=${VERIFY_SERVER_REPLICAS}",
        "worker.replicas=${WORKER_REPLICAS}",
        "reconciler.replicas=${RECONCILER_REPLICAS}",
        "--timeout=5m",
    ):
        assert phrase in stage_7, phrase


def test_canonical_staged_helm_upgrade_stage_eight_proves_and_cleans_up() -> None:
    _, stages = _staged_upgrade_stages()

    stage_8 = stages[_STAGED_UPGRADE_MARKERS[7]]
    for phrase in (
        'source "$RECOVERY_STATE_FILE"',
        "credential_acknowledged_at IS NOT NULL",
        "credential_envelope IS NULL",
        "state = 'active'",
        "CURRENT_WORKER_FENCE_PROTOCOL",
        "WORKER_REPLICAS",
        "secretKeyRef:",
        '"ops.build_uses_list"',
        '"ops.recover_build_use"',
        "BearerAuth",
        "list_tools",
        "timeout 60s uv run kdivectl --json ops build-uses-list --limit 1",
        "KDIVE_SERVER_URL",
        "KDIVE_TOKEN",
        "CURRENT_SERVER_REPLICAS",
        'kubectl delete job "$INCARNATION_JOB"',
        "--ignore-not-found --wait=true --timeout=2m",
        "set-queue-paused --no-paused",
        "set-queue-paused --paused",
        'test "$SERVER_REPLICAS" -eq 0',
        "wait_for_server_deleted",
        'COMPLETED_STATE="${RECOVERY_STATE_FILE}.complete"',
        'mv -- "$RECOVERY_STATE_FILE" "$COMPLETED_STATE"',
        'kubectl delete configmap "$RECOVERY_STATE"',
    ):
        assert phrase in stage_8, phrase
    proof_end = stage_8.index("ops build-uses-list --limit 1")
    assert stage_8.index("set-queue-paused --paused") < stage_8.index(
        'kubectl create -n "$NAMESPACE" -f - <<EOF'
    )
    assert proof_end < stage_8.index("set-queue-paused --no-paused")
    assert proof_end < stage_8.rindex("set-queue-paused --paused")
    assert stage_8.index('mv -- "$RECOVERY_STATE_FILE" "$COMPLETED_STATE"') < stage_8.rindex(
        'kubectl delete configmap "$RECOVERY_STATE"'
    )
    cleanup = stage_8.split('if test -e "$COMPLETED_STATE"', maxsplit=1)[1].split("fi", maxsplit=1)[
        0
    ]
    assert 'source "$COMPLETED_STATE"' in cleanup
    assert cleanup.count("--ignore-not-found --wait=true --timeout=2m") >= 2
    assert "-l " not in cleanup


def test_canonical_staged_helm_upgrade_bash_blocks_parse() -> None:
    staged, _ = _staged_upgrade_stages()

    for block in re.findall(r"```bash\n(.*?)```", staged, flags=re.DOTALL):
        parsed = subprocess.run(
            ["bash", "-n", "-c", block], text=True, capture_output=True, check=False
        )
        assert parsed.returncode == 0, parsed.stderr


def test_worker_fence_summaries_link_to_the_canonical_staged_runbook() -> None:
    assert "runbooks/kubernetes-deploy.md#staged-worker-fence-upgrade" in _INSTALL.read_text()
    assert "kubernetes-deploy.md#staged-worker-fence-upgrade" in _BUILD_USE_RECOVERY.read_text()

    assert (
        "../../../docs/operating/runbooks/kubernetes-deploy.md#staged-worker-fence-upgrade"
        in _HELM_REFERENCE.read_text()
    )
