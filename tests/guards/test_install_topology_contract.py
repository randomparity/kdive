"""Operator documentation must describe each deployment's actual process topology."""

import os
import re
import shlex
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
_LOCAL_LIBVIRT_WALKTHROUGH = _ROOT / "docs/operating/providers/local-libvirt-walkthrough.md"
_SELF_HOSTED_KVM_RUNNER = _ROOT / "docs/operating/runbooks/self-hosted-kvm-runner.md"
_LIVE_TESTING_RUNBOOK = _ROOT / "docs/operating/runbooks/live-testing.md"
_HELM_VALUES = _ROOT / "deploy/helm/kdive/values.yaml"

_DIRECT_HOST_WORKER = re.compile(
    r"(?:^|\s)(?:\S*/)?python\s+-m\s+kdive\s+worker(?:\s|$)", re.MULTILINE
)

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


def _shell_fences(path: Path) -> str:
    source = path.read_text()
    return "\n".join(
        match.group("body")
        for match in re.finditer(
            r"^```(?:bash|sh|shell|console)\s*$\n(?P<body>.*?)^```\s*$",
            source,
            re.MULTILINE | re.DOTALL,
        )
    )


def test_active_operator_docs_do_not_instruct_direct_host_worker_launches() -> None:
    offenders = [
        path.relative_to(_ROOT).as_posix()
        for path in sorted((_ROOT / "docs/operating").rglob("*.md"))
        if _DIRECT_HOST_WORKER.search(_shell_fences(path))
    ]
    assert offenders == []


def test_local_libvirt_walkthrough_threads_the_published_uri() -> None:
    fences = _shell_fences(_LOCAL_LIBVIRT_WALKTHROUGH)
    assert "source scripts/live-stack/libvirt-uri.sh" in fences
    assert 'KDIVE_LIBVIRT_URI="$(load_published_libvirt_uri)"' in fences
    assert "export KDIVE_LIBVIRT_URI" in fences
    assert 'host_uri = \\"${KDIVE_LIBVIRT_URI}\\"' in fences
    assert "virsh -c qemu:///system" not in fences


def test_hosted_lifecycle_docs_use_the_published_libvirt_uri() -> None:
    runner = _SELF_HOSTED_KVM_RUNNER.read_text()
    assert "KDIVE_LIBVIRT_URI=qemu:///session" not in runner
    assert "source scripts/live-stack/libvirt-uri.sh" in runner
    assert 'KDIVE_LIBVIRT_URI="$(load_published_libvirt_uri)"' in runner
    assert "export KDIVE_LIBVIRT_URI" in runner

    hosted_quirk = (
        _LIVE_TESTING_RUNBOOK.read_text()
        .split("## Hard-won quirks", maxsplit=1)[1]
        .split("- **A long `XDG_CONFIG_HOME`", maxsplit=1)[0]
    )
    assert "KDIVE_LIBVIRT_URI=qemu:///session" not in hosted_quirk
    assert "load_published_libvirt_uri" in hosted_quirk


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
        "do not invoke `python -m kdive.processes.lifecycle.compose_worker_lifecycle` directly"
        in section
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


def _bash_blocks(section: str) -> list[str]:
    return re.findall(r"```bash\n(.*?)```", section, flags=re.DOTALL)


def _block_containing(section: str, needle: str) -> str:
    matches = [block for block in _bash_blocks(section) if needle in block]
    assert len(matches) == 1, needle
    return matches[0]


def test_canonical_staged_helm_upgrade_stages_one_to_four_are_restart_safe() -> None:
    staged, stages = _staged_upgrade_stages()

    stage_1 = stages[_STAGED_UPGRADE_MARKERS[0]]
    for phrase in (
        "set -euo pipefail",
        ': "${NAMESPACE:?set the target namespace}"',
        "umask 077",
        'test ! -e "$TARGET_VALUES"',
        'chmod 0600 "$TARGET_VALUES_TMP"',
        'helm template "$RELEASE" "$TARGET_CHART" -n "$NAMESPACE" -f "$TARGET_VALUES_SNAPSHOT"',
        "--show-only templates/job-migrate.yaml",
        "TARGET_IMAGE",
        "TARGET_IMAGE_PULL_POLICY",
        "MIGRATION_SECRET",
        "MIGRATION_KEY",
        "TARGET_VALUES_SHA256",
        "TARGET_CHART_SHA256",
        "RECOVERY_STATE",
        "validate_nonnegative_count",
        "validate_short_name",
        'DB_CLIENT_JOB="${FULL}-fence-db-check"',
        'INCARNATION_JOB="${FULL}-fence-worker-check"',
        "%q",
        'bash -n "$output"',
        'mv -- "$RECOVERY_STATE_TMP" "$RECOVERY_STATE_FILE"',
        "kubectl create configmap",
        "verify_recovery_configmap",
        "${FULL}-fence-upgrade",
    ):
        assert phrase in stage_1, phrase
    assert "kubectl apply" in stage_1
    assert "CURRENT_IMAGE" not in staged
    assert "NAMESPACE=<namespace>" not in staged

    capture_block = _block_containing(stage_1, 'helm get values "$RELEASE"')
    assert capture_block.index("umask 077") < capture_block.index("helm get values")
    assert capture_block.index('chmod 0600 "$TARGET_VALUES_TMP"') < capture_block.index(
        'mv -- "$TARGET_VALUES_TMP" "$TARGET_VALUES"'
    )

    initial_block = _block_containing(stage_1, 'name: "${CURRENT_MIGRATION_SECRET}"')
    validation_loop = re.search(
        r'for name in ([^;]+); do\n  validate_short_name "\$name"', initial_block
    )
    assert validation_loop is not None
    for generated_name in (
        '"$RECOVERY_STATE"',
        '"$QUEUE_STATE_JOB"',
        '"$DB_CLIENT_JOB"',
        '"$INCARNATION_JOB"',
    ):
        assert generated_name in validation_loop.group(1)
    assert initial_block.index(
        'mv -- "$RECOVERY_STATE_TMP" "$RECOVERY_STATE_FILE"'
    ) < initial_block.index("render_recovery_configmap | kubectl create -f -")

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
    stage_2_block = _bash_blocks(stage_2)[0]
    witness_rollouts = [
        match.start()
        for match in re.finditer(r"rollout status deployment/\$\{FULL\}-witness", stage_2_block)
    ]
    witness_ready = [
        match.start()
        for match in re.finditer(
            re.escape(
                'kubectl wait --for=condition=Ready pod -n "$NAMESPACE" \\\n'
                '  -l "app=${FULL}-witness"'
            ),
            stage_2_block,
        )
    ]
    assert len(witness_rollouts) == len(witness_ready) == 2
    pause = stage_2_block.index("set-queue-paused --paused")
    scale = stage_2_block.index("scale statefulset/${FULL}-worker")
    drained = stage_2_block.index('wait_for_pods_deleted "app=${FULL}-worker"')
    assert witness_rollouts[0] < witness_ready[0] < pause < scale < drained
    assert drained < witness_rollouts[1] < witness_ready[1]
    for limit_contract in (
        "five-minute Kubernetes API wall-clock limit",
        "each rollout and Ready-Pod wait",
        "target namespace",
        "exits Stage 2",
        "rerun Stage 2",
    ):
        assert limit_contract in stage_2

    stage_3 = stages[_STAGED_UPGRADE_MARKERS[2]]
    for phrase in (
        'source "$RECOVERY_STATE_FILE"',
        "activeDeadlineSeconds: 60",
        "secretKeyRef:",
        'name: "${MIGRATION_SECRET}"',
        'key: "${MIGRATION_KEY}"',
        'image: "${TARGET_IMAGE}"',
        'imagePullPolicy: "${TARGET_IMAGE_PULL_POLICY}"',
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


def test_canonical_staged_helm_upgrade_pins_target_render_and_repin() -> None:
    staged, stages = _staged_upgrade_stages()
    stage_1 = stages[_STAGED_UPGRADE_MARKERS[0]]
    initial_block = _block_containing(stage_1, 'name: "${CURRENT_MIGRATION_SECRET}"')

    for phrase in (
        "sha256_chart()",
        'TARGET_VALUES_SHA256=$(sha256_file "$TARGET_VALUES")',
        'TARGET_VALUES_SNAPSHOT=$(publish_values_snapshot "$TARGET_VALUES" '
        '"$TARGET_VALUES_SHA256")',
        'TARGET_CHART_SHA256=$(sha256_chart "$CHART")',
        "TARGET_IMAGE_PULL_POLICY",
        'validate_pull_policy "$TARGET_IMAGE_PULL_POLICY"',
        "Always | IfNotPresent | Never",
        'TARGET_CHART=$(publish_chart_snapshot "$CHART" "$TARGET_CHART_SHA256")',
        "write_recovery_state",
        '--from-literal=target_values_sha256="$TARGET_VALUES_SHA256"',
        '--from-literal=target_chart_sha256="$TARGET_CHART_SHA256"',
        '--from-literal=target_image_pull_policy="$TARGET_IMAGE_PULL_POLICY"',
    ):
        assert phrase in initial_block, phrase

    for stage_index, use in (
        (1, "set-queue-paused --paused"),
        (2, "kind: Job"),
        (3, "helm upgrade"),
        (4, "kubectl get secret"),
        (5, "helm upgrade"),
        (6, "helm upgrade"),
        (7, "kind: Job"),
    ):
        block = _block_containing(stages[_STAGED_UPGRADE_MARKERS[stage_index]], use)
        assert block.index("verify_recovery_state") < block.index(use)

    final_block = _block_containing(stages[_STAGED_UPGRADE_MARKERS[7]], "ops build-uses-list")
    assert final_block.index("verify_recovery_state") < final_block.index("helm upgrade")
    assert final_block.rindex("verify_target_pins", 0, final_block.index("helm upgrade")) < (
        final_block.index("helm upgrade")
    )

    repin_block = _block_containing(stage_1, "TARGET_VALUES_NEXT:?set")
    for phrase in (
        "TARGET_VALUES_SNAPSHOT",
        "TARGET_VALUES_NEXT",
        "helm template",
        "TARGET_IMAGE_PULL_POLICY",
        "validate_secret_name",
        "validate_secret_key",
        "validate_pull_policy",
        "write_recovery_state",
        'mv -- "$RECOVERY_STATE_TMP" "$RECOVERY_STATE_FILE"',
        "kubectl apply -f -",
        "verify_recovery_configmap",
    ):
        assert phrase in repin_block, phrase
    assert "SERVER_REPLICAS=$(kubectl get" not in repin_block
    assert "PRIOR_QUEUE_PAUSED=$(kubectl" not in repin_block
    assert "rerun Stage 3 before Stage 4" in stage_1
    assert "edit the target file, and return to the hooked Stage 4" not in staged
    assert "A target-values change requires another all-zero hooked Stage 4" not in staged


def test_canonical_staged_helm_upgrade_snapshots_chart_without_toc_tou() -> None:
    staged, stages = _staged_upgrade_stages()
    initial_block = _block_containing(
        stages[_STAGED_UPGRADE_MARKERS[0]], 'name: "${CURRENT_MIGRATION_SECRET}"'
    )

    for phrase in (
        "find -P",
        "-print0",
        "sort -z",
        "read -r -d ''",
        "sha256sum --",
        'test -f "$chart" && test ! -L "$chart"',
        'test -d "$chart" && test ! -L "$chart"',
        'chmod 0700 "$RECOVERY_CHART_DIR"',
        'chmod 0600 "$temporary"',
        'mv -T -n -- "$temporary" "$target"',
        'actual=$(sha256_chart "$temporary")',
        'actual_source_chart=$(sha256_chart "$CHART")',
        'actual_target_chart=$(sha256_chart "$TARGET_CHART")',
        'actual_target_values=$(sha256_file "$TARGET_VALUES_SNAPSHOT")',
    ):
        assert phrase in initial_block, phrase

    render = initial_block.index('helm template "$RELEASE" "$TARGET_CHART"')
    snapshot = initial_block.index(
        'TARGET_CHART=$(publish_chart_snapshot "$CHART" "$TARGET_CHART_SHA256")'
    )
    live_capture = initial_block.index("SERVER_REPLICAS=$(kubectl get deployment/${FULL}-server")
    assert snapshot < render < live_capture
    assert render < initial_block.index('write_recovery_state "$RECOVERY_STATE_TMP"')
    helm_invocations = []
    for block in _bash_blocks(staged):
        lines = block.splitlines()
        helm_invocations.extend(
            "\n".join(lines[index : index + 4])
            for index, line in enumerate(lines)
            if re.search(r"helm (?:template|upgrade)", line)
        )
    assert helm_invocations
    assert all('"$TARGET_CHART"' in invocation for invocation in helm_invocations)
    assert all('"$CHART"' not in invocation for invocation in helm_invocations)
    assert all('-f "$TARGET_VALUES_SNAPSHOT"' in invocation for invocation in helm_invocations)
    assert all('-f "$TARGET_VALUES"' not in invocation for invocation in helm_invocations)


def test_canonical_staged_helm_upgrade_chart_hash_handles_both_chart_forms(
    tmp_path: Path,
) -> None:
    _, stages = _staged_upgrade_stages()
    initial_block = _block_containing(
        stages[_STAGED_UPGRADE_MARKERS[0]], 'name: "${CURRENT_MIGRATION_SECRET}"'
    )
    functions = initial_block[initial_block.index("sha256_file() {") :]
    functions = functions[: functions.index("publish_chart_snapshot() (")]
    chart = tmp_path / "chart with spaces"
    chart.mkdir()
    (chart / "Chart.yaml").write_text("name: test\n")
    templates = chart / "templates"
    templates.mkdir()
    (templates / "name with spaces.yaml").write_text("kind: ConfigMap\n")
    package = tmp_path / "chart package.tgz"
    package.write_bytes(b"packaged-chart")

    def digest(path: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "-c", functions + '\nsha256_chart "$1"\n', "_", str(path)],
            text=True,
            capture_output=True,
            check=False,
        )

    first = digest(chart)
    second = digest(chart)
    packaged = digest(package)
    assert first.returncode == second.returncode == packaged.returncode == 0
    assert first.stdout == second.stdout
    assert re.fullmatch(r"[0-9a-f]{64}\n", first.stdout)
    assert re.fullmatch(r"[0-9a-f]{64}\n", packaged.stdout)
    (templates / "name with spaces.yaml").write_text("kind: Secret\n")
    assert digest(chart).stdout != first.stdout


def test_canonical_staged_helm_upgrade_reuses_private_input_snapshots(tmp_path: Path) -> None:
    _, stages = _staged_upgrade_stages()
    initial_block = _block_containing(
        stages[_STAGED_UPGRADE_MARKERS[0]], 'name: "${CURRENT_MIGRATION_SECRET}"'
    )
    assert "publish_values_snapshot() (" in initial_block
    assert 'mktemp "${target}.tmp.XXXXXX"' in initial_block
    assert initial_block.index("trap cleanup_snapshot_publish EXIT") < initial_block.index(
        'temporary=$(mktemp "${target}.tmp.XXXXXX")'
    )
    capture_block = _block_containing(
        stages[_STAGED_UPGRADE_MARKERS[0]], 'helm get values "$RELEASE"'
    )
    refusal_loop = capture_block[capture_block.index("for path in ") :]
    refusal_loop = refusal_loop[: refusal_loop.index("done")]
    assert '"$RECOVERY_CHART_DIR"' not in refusal_loop
    assert '"$RECOVERY_VALUES_DIR"' not in refusal_loop
    functions = initial_block[initial_block.index("sha256_file() {") :]
    functions = functions[: functions.index("verify_target_pins() {")]
    source_chart = tmp_path / "source chart"
    source_chart.mkdir()
    (source_chart / "Chart.yaml").write_text("name: test\n")
    source_values = tmp_path / "operator values.yaml"
    source_values.write_text("image: target\n")
    chart_dir = tmp_path / "private chart snapshots"
    values_dir = tmp_path / "private values snapshots"
    script = (
        functions
        + r"""
set -euo pipefail
CHART=$1
TARGET_VALUES=$2
RECOVERY_CHART_DIR=$3
RECOVERY_VALUES_DIR=$4
chart_digest=$(sha256_chart "$CHART")
values_digest=$(sha256_file "$TARGET_VALUES")
TARGET_CHART=$(publish_chart_snapshot "$CHART" "$chart_digest")
TARGET_VALUES_SNAPSHOT=$(publish_values_snapshot "$TARGET_VALUES" "$values_digest")
printf '%s\n%s\n' "$TARGET_CHART" "$TARGET_VALUES_SNAPSHOT"
"""
    )

    def publish() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "bash",
                "-c",
                script,
                "_",
                str(source_chart),
                str(source_values),
                str(chart_dir),
                str(values_dir),
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    first = publish()
    assert first.returncode == 0, first.stderr
    snapshot_paths = [Path(path) for path in first.stdout.splitlines()]
    snapshot_inodes = [path.stat().st_ino for path in snapshot_paths]
    (chart_dir / "unrelated.tmp.leftover").write_text("ignore me\n")
    (values_dir / "unrelated.tmp.leftover").write_text("ignore me\n")
    second = publish()
    assert second.returncode == 0, second.stderr
    assert second.stdout == first.stdout
    assert [path.stat().st_ino for path in snapshot_paths] == snapshot_inodes
    assert snapshot_paths[1].stat().st_mode & 0o777 == 0o600
    snapshot_paths[1].write_text("mismatched final\n")
    assert publish().returncode != 0
    snapshot_paths[1].write_text(source_values.read_text())
    snapshot_paths[0].joinpath("Chart.yaml").write_text("mismatched final\n")
    assert publish().returncode != 0


def test_canonical_staged_helm_upgrade_retry_is_fresh_and_does_not_recapture(
    tmp_path: Path,
) -> None:
    _, stages = _staged_upgrade_stages()
    stage_1 = stages[_STAGED_UPGRADE_MARKERS[0]]
    initialization_block = _block_containing(stage_1, 'helm get values "$RELEASE"')
    capture_block = _block_containing(stage_1, 'name: "${CURRENT_MIGRATION_SECRET}"')
    assert stage_1.index(initialization_block) < stage_1.index(capture_block)
    snapshot_end = capture_block.index(
        'TARGET_CHART=$(publish_chart_snapshot "$CHART" "$TARGET_CHART_SHA256")'
    )
    first_live_capture = capture_block.index(
        "SERVER_REPLICAS=$(kubectl get deployment/${FULL}-server"
    )
    state_publication = capture_block.index('mv -- "$RECOVERY_STATE_TMP" "$RECOVERY_STATE_FILE"')
    assert snapshot_end < first_live_capture < state_publication

    source_chart = tmp_path / "source chart"
    source_chart.mkdir()
    (source_chart / "Chart.yaml").write_text("name: retry-test\n")
    source_values = tmp_path / "operator values.yaml"
    recovery_state = tmp_path / "recovery state"
    chart_dir = Path(f"{recovery_state}.charts")
    values_dir = Path(f"{recovery_state}.values")
    fake_bin = tmp_path / "fake bin"
    fake_bin.mkdir()
    command_log = tmp_path / "command log"
    desired_configmap = tmp_path / "desired configmap.json"
    published_configmap = tmp_path / "published configmap.json"
    fail_server_once = tmp_path / "fail server once"
    shell_pids = tmp_path / "capture shell pids"

    fake_helm = fake_bin / "helm"
    fake_helm.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf 'helm %s\\n' "$*" >>"$FAKE_COMMAND_LOG"
if test "$1 $2" = "get values"; then
  printf 'image:\\n  repository: example.invalid/kdive\\n  tag: retry-test\\n'
elif test "$1" = template; then
  printf 'fake rendered migration job\\n'
else
  exit 64
fi
"""
    )
    fake_helm.chmod(0o755)

    fake_kubectl = fake_bin / "kubectl"
    fake_kubectl.write_text(
        """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import re
import sys

args = sys.argv[1:]
log_path = Path(os.environ["FAKE_COMMAND_LOG"])


def log(event: str) -> None:
    with log_path.open("a") as stream:
        stream.write(event + "\\n")


def option_value(option: str) -> str:
    return args[args.index(option) + 1]


if args[0] == "create" and "--dry-run=client" in args and args[1] != "configmap":
    sys.stdin.read()
    log("target_render_tuple")
    print("example.invalid/kdive:retry-test\\tIfNotPresent\\ttarget-db\\tdsn")
elif args[:2] == ["create", "configmap"]:
    data = {}
    for argument in args:
        if argument.startswith("--from-literal="):
            key, value = argument.removeprefix("--from-literal=").split("=", 1)
            data[key] = value
    Path(os.environ["FAKE_CM_DESIRED"]).write_text(json.dumps(data))
    print("FAKE_CONFIGMAP")
elif args[0] == "create" and "-f" in args:
    content = sys.stdin.read()
    if content.strip() == "FAKE_CONFIGMAP":
        desired = Path(os.environ["FAKE_CM_DESIRED"]).read_text()
        Path(os.environ["FAKE_CM_PUBLISHED"]).write_text(desired)
        log("configmap_publish")
    else:
        log("queue_job_create")
elif args[0] == "get" and args[1].startswith("deployment/"):
    resource = args[1]
    if resource.endswith("-server"):
        fail_once = Path(os.environ["FAKE_FAIL_SERVER_ONCE"])
        log("server_replica_read_attempt")
        if fail_once.exists():
            fail_once.rename(fail_once.with_suffix(".used"))
            raise SystemExit(42)
        log("server_replica_read_success")
        print("2", end="")
    elif resource.endswith("-reconciler"):
        log("reconciler_replica_read_success")
        print("1", end="")
elif args[0] == "get" and args[1].startswith("statefulset/"):
    log("worker_replica_read_success")
    print("3", end="")
elif args[0] == "get" and args[1].endswith("-migrate"):
    output = option_value("-o")
    print("current-db" if "secretKeyRef.name" in output else "dsn", end="")
elif args[:2] == ["get", "job"]:
    raise SystemExit(1)
elif args[:2] == ["get", "configmap"]:
    data = json.loads(Path(os.environ["FAKE_CM_PUBLISHED"]).read_text())
    output = option_value("-o")
    if output.startswith("go-template="):
        print(len(data), end="")
    else:
        match = re.search(r"\\.data\\.([a-z0-9_]+)", output)
        if match is None:
            raise SystemExit(65)
        print(data[match.group(1)], end="")
elif args[0] == "wait":
    log("queue_job_complete")
elif args[0] == "logs":
    if "--tail=1" in args:
        log("queue_state_capture_success")
        print("false")
    else:
        print("queue diagnostic complete")
elif args[0] == "delete":
    log("queue_job_delete")
else:
    log("unhandled kubectl " + " ".join(args))
    raise SystemExit(66)
"""
    )
    fake_kubectl.chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "NAMESPACE": "retry-namespace",
        "RELEASE": "retry-release",
        "CHART": str(source_chart),
        "TARGET_VALUES": str(source_values),
        "RECOVERY_STATE_FILE": str(recovery_state),
        "FAKE_COMMAND_LOG": str(command_log),
        "FAKE_CM_DESIRED": str(desired_configmap),
        "FAKE_CM_PUBLISHED": str(published_configmap),
        "FAKE_FAIL_SERVER_ONCE": str(fail_server_once),
    }

    initialized = subprocess.run(
        ["bash", "-c", initialization_block],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert initialized.returncode == 0, initialized.stderr
    fail_server_once.write_text("fail the first live capture\n")

    def capture() -> subprocess.CompletedProcess[str]:
        wrapped = f"printf '%s\\n' \"$BASHPID\" >>{shlex.quote(str(shell_pids))}\n{capture_block}"
        return subprocess.run(
            ["bash", "-c", wrapped],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    failed = capture()
    assert failed.returncode == 42
    assert not recovery_state.exists()
    assert not published_configmap.exists()
    failed_paths = [next(chart_dir.glob("*.dir")), next(values_dir.glob("*.yaml"))]
    failed_inodes = [path.stat().st_ino for path in failed_paths]
    assert list(chart_dir.glob("*.tmp.*")) == []
    assert list(values_dir.glob("*.tmp.*")) == []

    retried = capture()
    assert retried.returncode == 0, retried.stderr
    retry_paths = [next(chart_dir.glob("*.dir")), next(values_dir.glob("*.yaml"))]
    assert retry_paths == failed_paths
    assert [path.stat().st_ino for path in retry_paths] == failed_inodes
    assert recovery_state.is_file()
    assert published_configmap.is_file()
    observed_shells = shell_pids.read_text().splitlines()
    assert len(observed_shells) == 2
    assert len(set(observed_shells)) == 2
    events = command_log.read_text().splitlines()
    assert sum(event.startswith("helm get values") for event in events) == 1
    assert events.count("server_replica_read_attempt") == 2
    assert events.count("server_replica_read_success") == 1
    assert events.count("worker_replica_read_success") == 1
    assert events.count("reconciler_replica_read_success") == 1
    assert events.count("queue_state_capture_success") == 1
    assert events.count("configmap_publish") == 1

    functions = capture_block[capture_block.index("sha256_file() {") :]
    functions = functions[: functions.index("verify_target_pins() {")]
    failure_functions = (
        functions
        + r"""
set -euo pipefail
source_input=$1
RECOVERY_CHART_DIR=$2
RECOVERY_VALUES_DIR=$3
kind=$4
digest=$(
  if test "$kind" = chart; then sha256_chart "$source_input"; else sha256_file "$source_input"; fi
)
cp() { command cp "$@"; return 19; }
if test "$kind" = chart; then
  publish_chart_snapshot "$source_input" "$digest"
else
  publish_values_snapshot "$source_input" "$digest"
fi
"""
    )

    for kind, source_input in (("chart", source_chart), ("values", source_values)):
        failure_root = tmp_path / f"{kind} ordinary failure"
        failure_root.mkdir()
        failed_publish = subprocess.run(
            [
                "bash",
                "-c",
                failure_functions,
                "_",
                str(source_input),
                str(failure_root / "charts"),
                str(failure_root / "values"),
                kind,
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        assert failed_publish.returncode == 19
        assert list(failure_root.rglob("*.tmp.*")) == []


def test_canonical_staged_helm_upgrade_pin_guard_rejects_input_drift(tmp_path: Path) -> None:
    _, stages = _staged_upgrade_stages()
    initial_block = _block_containing(
        stages[_STAGED_UPGRADE_MARKERS[0]], 'name: "${CURRENT_MIGRATION_SECRET}"'
    )
    functions = initial_block[initial_block.index("validate_secret_name() {") :]
    functions = functions[: functions.index("render_recovery_configmap() {")]
    source_chart = tmp_path / "source chart"
    target_chart = tmp_path / "snapshot chart"
    for chart in (source_chart, target_chart):
        chart.mkdir()
        (chart / "Chart.yaml").write_text("name: test\n")
    values = tmp_path / "target values.yaml"
    values.write_text("image: target\n")
    values_snapshot = tmp_path / "snapshot values.yaml"
    values_snapshot.write_text("image: target\n")
    script = (
        functions
        + r"""
CHART=$1
TARGET_CHART=$2
TARGET_VALUES=$3
TARGET_VALUES_SNAPSHOT=$4
mode=$5
TARGET_VALUES_SHA256=$(sha256_file "$TARGET_VALUES")
TARGET_CHART_SHA256=$(sha256_chart "$CHART")
TARGET_IMAGE=example.invalid/kdive:test
TARGET_IMAGE_PULL_POLICY=IfNotPresent
MIGRATION_SECRET=true
MIGRATION_KEY=null
case "$mode" in
  clean) ;;
  values) printf 'changed\n' >>"$TARGET_VALUES" ;;
  values_snapshot) printf 'changed\n' >>"$TARGET_VALUES_SNAPSHOT" ;;
  source) printf 'changed\n' >>"$CHART/Chart.yaml" ;;
  snapshot) printf 'changed\n' >>"$TARGET_CHART/Chart.yaml" ;;
  tuple) TARGET_IMAGE_PULL_POLICY=sometimes ;;
esac
verify_target_pins
"""
    )

    def verify(mode: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "bash",
                "-c",
                script,
                "_",
                str(source_chart),
                str(target_chart),
                str(values),
                str(values_snapshot),
                mode,
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    assert verify("clean").returncode == 0
    for mode in ("values", "values_snapshot", "source", "snapshot", "tuple"):
        source_chart.joinpath("Chart.yaml").write_text("name: test\n")
        target_chart.joinpath("Chart.yaml").write_text("name: test\n")
        values.write_text("image: target\n")
        values_snapshot.write_text("image: target\n")
        assert verify(mode).returncode != 0, mode


def test_canonical_staged_helm_upgrade_persists_exact_target_contract() -> None:
    _, stages = _staged_upgrade_stages()
    initial_block = _block_containing(
        stages[_STAGED_UPGRADE_MARKERS[0]], 'name: "${CURRENT_MIGRATION_SECRET}"'
    )
    persisted = (
        "TARGET_CHART",
        "TARGET_VALUES_SNAPSHOT",
        "TARGET_VALUES_SHA256",
        "TARGET_CHART_SHA256",
        "TARGET_IMAGE",
        "MIGRATION_SECRET",
        "MIGRATION_KEY",
        "TARGET_IMAGE_PULL_POLICY",
        "STAGE3_VALUES_SHA256",
        "STAGE3_CHART_SHA256",
    )
    for name in persisted:
        assert f"printf '{name}=%q\\n'" in initial_block

    literals = re.findall(r"--from-literal=([a-z0-9_]+)=", initial_block)
    assert literals == [
        "server_replicas",
        "worker_replicas",
        "reconciler_replicas",
        "prior_queue_paused",
        "target_values_sha256",
        "target_chart_sha256",
        "target_image",
        "migration_secret",
        "migration_key",
        "target_image_pull_policy",
        "stage3_values_sha256",
        "stage3_chart_sha256",
    ]
    assert 'test "$key_count" = 12' in initial_block
    assert "KDIVE_DATABASE_URL=%q" not in initial_block
    assert "secret_value" not in initial_block.lower()


def test_canonical_staged_helm_upgrade_guards_every_fresh_stage_block() -> None:
    _, stages = _staged_upgrade_stages()

    for marker in _STAGED_UPGRADE_MARKERS[1:]:
        for block in _bash_blocks(stages[marker]):
            source = 'source "$RECOVERY_STATE_FILE"'
            assert source in block, marker
            source_position = block.index(source)
            assert block.rindex('bash -n "$RECOVERY_STATE_FILE"', 0, source_position) < (
                source_position
            )
            assert block.index("verify_recovery_state", source_position) > source_position


def test_canonical_staged_helm_upgrade_stage_three_proof_gates_helm() -> None:
    _, stages = _staged_upgrade_stages()
    stage_3_block = _block_containing(stages[_STAGED_UPGRADE_MARKERS[2]], "kind: Job")
    job_proof = stage_3_block.index("kubectl logs job/${DB_CLIENT_JOB}")
    marker_values = stage_3_block.index('STAGE3_VALUES_SHA256="$TARGET_VALUES_SHA256"')
    marker_chart = stage_3_block.index('STAGE3_CHART_SHA256="$TARGET_CHART_SHA256"')
    assert job_proof < marker_values < marker_chart

    stage_4_block = _block_containing(stages[_STAGED_UPGRADE_MARKERS[3]], "helm upgrade")
    helm = stage_4_block.index("helm upgrade")
    assert stage_4_block.index('test "$STAGE3_VALUES_SHA256" = "$TARGET_VALUES_SHA256"') < helm
    assert stage_4_block.index('test "$STAGE3_CHART_SHA256" = "$TARGET_CHART_SHA256"') < helm

    marker_guard = stage_4_block[
        stage_4_block.index('test "$STAGE3_VALUES_SHA256"') : stage_4_block.index(
            "verify_target_pins"
        )
    ]
    marker_script = (
        "TARGET_VALUES_SHA256=values\n"
        "TARGET_CHART_SHA256=chart\n"
        "STAGE3_VALUES_SHA256=$1\n"
        "STAGE3_CHART_SHA256=$2\n" + marker_guard
    )
    for values_marker, chart_marker, expected in (
        ("values", "chart", 0),
        ("old-values", "chart", 2),
        ("values", "old-chart", 2),
    ):
        checked = subprocess.run(
            ["bash", "-c", marker_script, "_", values_marker, chart_marker],
            text=True,
            capture_output=True,
            check=False,
        )
        assert checked.returncode == expected


def test_canonical_staged_helm_upgrade_configmap_guard_rejects_digest_drift() -> None:
    _, stages = _staged_upgrade_stages()
    initial_block = _block_containing(
        stages[_STAGED_UPGRADE_MARKERS[0]], 'name: "${CURRENT_MIGRATION_SECRET}"'
    )
    verifier = initial_block[initial_block.index("verify_captured_configmap() {") :]
    verifier = verifier[: verifier.index("verify_recovery_state() {")]
    script = (
        verifier
        + r"""
verify_captured_configmap() { :; }
TARGET_VALUES_SHA256=values
TARGET_CHART_SHA256=chart
TARGET_IMAGE=image
MIGRATION_SECRET=secret
MIGRATION_KEY=key
TARGET_IMAGE_PULL_POLICY=IfNotPresent
STAGE3_VALUES_SHA256=stage-values
STAGE3_CHART_SHA256=stage-chart
CM_TARGET_VALUES_SHA256=$1
kubectl() {
  case "$*" in
    *'{{len .data}}'*) printf 12 ;;
    *target_values_sha256*) printf %s "$CM_TARGET_VALUES_SHA256" ;;
    *target_chart_sha256*) printf %s "$TARGET_CHART_SHA256" ;;
    *target_image_pull_policy*) printf %s "$TARGET_IMAGE_PULL_POLICY" ;;
    *target_image*) printf %s "$TARGET_IMAGE" ;;
    *migration_secret*) printf %s "$MIGRATION_SECRET" ;;
    *migration_key*) printf %s "$MIGRATION_KEY" ;;
    *stage3_values_sha256*) printf %s "$STAGE3_VALUES_SHA256" ;;
    *stage3_chart_sha256*) printf %s "$STAGE3_CHART_SHA256" ;;
  esac
}
verify_recovery_configmap
"""
    )
    for mirror_digest, expected in (("values", 0), ("old-values", 2)):
        checked = subprocess.run(
            ["bash", "-c", script, "_", mirror_digest],
            text=True,
            capture_output=True,
            check=False,
        )
        assert checked.returncode == expected


def test_canonical_staged_helm_upgrade_repin_preserves_live_capture() -> None:
    _, stages = _staged_upgrade_stages()
    stage_1 = stages[_STAGED_UPGRADE_MARKERS[0]]
    repin = _block_containing(stage_1, "TARGET_VALUES_NEXT:?set")

    assert "STAGE3_VALUES_SHA256=" in repin
    assert "STAGE3_CHART_SHA256=" in repin
    assert "render_recovery_configmap | kubectl apply -f -" in repin
    assert repin.count("kubectl apply -f -") == 1
    local_publish = 'mv -- "$RECOVERY_STATE_TMP" "$RECOVERY_STATE_FILE"'
    cluster_publish = "render_recovery_configmap | kubectl apply -f -"
    assert repin.index(local_publish) < repin.index(cluster_publish)
    assert "SELECT queue_paused" not in repin
    assert "SERVER_REPLICAS=$(kubectl get" not in repin
    assert "WORKER_REPLICAS=$(kubectl get" not in repin
    assert "RECONCILER_REPLICAS=$(kubectl get" not in repin
    assert "printf 'PRIOR_QUEUE_PAUSED=%q\\n'" not in repin


def test_canonical_staged_helm_upgrade_validates_and_quotes_secret_refs() -> None:
    _, stages = _staged_upgrade_stages()
    stage_1 = stages[_STAGED_UPGRADE_MARKERS[0]]
    initial_block = _block_containing(stage_1, 'name: "${CURRENT_MIGRATION_SECRET}"')

    for phrase in (
        'validate_secret_name "$MIGRATION_SECRET"',
        'validate_secret_key "$MIGRATION_KEY"',
        'validate_secret_name "$CURRENT_MIGRATION_SECRET"',
        'validate_secret_key "$CURRENT_MIGRATION_KEY"',
    ):
        assert phrase in initial_block

    function_source = initial_block[initial_block.index("validate_secret_name() {") :]
    function_source = function_source[: function_source.index("validate_pull_policy() {")]
    for ambiguous in ("null", "true"):
        call = (
            f"validate_secret_name {shlex.quote(ambiguous)}\n"
            f"validate_secret_key {shlex.quote(ambiguous)}\n"
        )
        parsed = subprocess.run(
            ["bash", "-c", function_source + call],
            text=True,
            capture_output=True,
            check=False,
        )
        assert parsed.returncode == 0, parsed.stderr
    for invalid_name in ("a.-b", "a-.b", "a..b"):
        parsed = subprocess.run(
            [
                "bash",
                "-c",
                function_source + f"validate_secret_name {shlex.quote(invalid_name)}\n",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        assert parsed.returncode != 0, invalid_name

    diagnostic_blocks = (
        initial_block,
        _block_containing(stages[_STAGED_UPGRADE_MARKERS[2]], "kind: Job"),
        _block_containing(stages[_STAGED_UPGRADE_MARKERS[7]], "kind: Job"),
    )
    assert 'name: "${CURRENT_MIGRATION_SECRET}"' in diagnostic_blocks[0]
    assert 'key: "${CURRENT_MIGRATION_KEY}"' in diagnostic_blocks[0]
    for block in diagnostic_blocks[1:]:
        assert 'name: "${MIGRATION_SECRET}"' in block
        assert 'key: "${MIGRATION_KEY}"' in block
    for block in diagnostic_blocks:
        assert 'imagePullPolicy: "${TARGET_IMAGE_PULL_POLICY}"' in block


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
        'mv -- "$RECOVERY_CHART_DIR" "$COMPLETED_CHART_DIR"',
        'mv -- "$RECOVERY_VALUES_DIR" "$COMPLETED_VALUES_DIR"',
        'kubectl delete configmap "$RECOVERY_STATE"',
    ):
        assert phrase in stage_8, phrase
    final_block = _block_containing(stage_8, "ops build-uses-list --limit 1")
    completion_assignment = 'COMPLETED_STATE="${RECOVERY_STATE_FILE}.complete"'
    assert completion_assignment in final_block
    assert final_block.index(completion_assignment) < final_block.index(
        'test ! -e "$COMPLETED_STATE"'
    )

    proof_end = stage_8.index("ops build-uses-list --limit 1")
    assert stage_8.index("set-queue-paused --paused") < stage_8.index(
        'kubectl create -n "$NAMESPACE" -f - <<EOF'
    )
    assert proof_end < stage_8.index("set-queue-paused --no-paused")
    assert proof_end < stage_8.rindex("set-queue-paused --paused")
    assert stage_8.index('mv -- "$RECOVERY_STATE_FILE" "$COMPLETED_STATE"') < stage_8.rindex(
        'kubectl delete configmap "$RECOVERY_STATE"'
    )
    cleanup = stage_8.split('if test -e "$COMPLETED_STATE"', maxsplit=1)[1].split(
        "  exit 0\nfi", maxsplit=1
    )[0]
    assert 'source "$COMPLETED_STATE"' in cleanup
    assert cleanup.count("--ignore-not-found --wait=true --timeout=2m") >= 2
    assert "-l " not in cleanup


def test_canonical_staged_helm_upgrade_restores_the_captured_queue_state() -> None:
    _, stages = _staged_upgrade_stages()
    stage_1 = stages[_STAGED_UPGRADE_MARKERS[0]]
    initial_block = _block_containing(stage_1, 'name: "${CURRENT_MIGRATION_SECRET}"')
    final_block = _block_containing(stages[_STAGED_UPGRADE_MARKERS[7]], "ops build-uses-list")

    for phrase in (
        "SELECT queue_paused FROM ops_control WHERE singleton = true",
        'case "$PRIOR_QUEUE_PAUSED" in',
        "true | false",
        "printf 'PRIOR_QUEUE_PAUSED=%q\\n'",
        '--from-literal=prior_queue_paused="$PRIOR_QUEUE_PAUSED"',
        'cm_queue=$(kubectl get configmap "$RECOVERY_STATE"',
        'test "$cm_queue" = "$PRIOR_QUEUE_PAUSED"',
    ):
        assert phrase in initial_block, phrase

    equality = 'test "$CM_PRIOR_QUEUE_PAUSED" = "$PRIOR_QUEUE_PAUSED"'
    queue_case = 'case "$PRIOR_QUEUE_PAUSED" in'
    assert equality in final_block
    assert final_block.index(equality) < final_block.index(queue_case)
    restore = final_block[final_block.index(queue_case) : final_block.index("esac")]
    assert re.search(r"true\)\n\s+timeout 60s .* --paused\n\s+;;", restore)
    assert re.search(r"false\)\n\s+timeout 60s .* --no-paused\n\s+;;", restore)


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
