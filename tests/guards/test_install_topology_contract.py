"""Operator documentation must describe each deployment's actual process topology."""

import re
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


def test_canonical_staged_helm_upgrade_preserves_the_fence_boundary() -> None:
    source = _KUBERNETES_RUNBOOK.read_text()

    assert "### Staged worker-fence upgrade" in source
    section = _section(_KUBERNETES_RUNBOOK, "### Staged worker-fence upgrade", "**Validate")

    required = (
        "helm get values",
        "deployment/${full}-server",
        "statefulset/${full}-worker",
        "deployment/${full}-reconciler",
        "keep the current witness and credentials healthy",
        "pod or its finalizer remains",
        "deployment/${full}-witness",
        "all four kdive workloads have no running pods",
        "pg_stat_activity",
        "pid <> pg_backend_pid()",
        "operator-authorized backend sql client",
        "operator_database_url",
        "--timeout=5m",
        "do not remove finalizers or hide an error",
        "lifecyclewitness.replicas=0",
        "democredentials.postgresql.serverpassword",
        "democredentials.postgresql.workerpassword",
        "democredentials.postgresql.reconcilerpassword",
        "democredentials.postgresql.lifecyclewitnesspassword",
        "external backends",
        "--no-hooks",
        "lifecyclewitness.replicas=1",
        "wait for the witness rollout and readiness",
        "server.replicas=${server_replicas}",
        "worker.replicas=${worker_replicas}",
        "reconciler.replicas=${reconciler_replicas}",
        "--reuse-values",
        "forward recovery",
    )
    for phrase in required:
        assert phrase in section, phrase

    assert section.count("--no-hooks") >= 2
    assert "do not use `--reuse-values`, `--atomic`, or a rollback" in section
    assert "|| true" not in section


def test_worker_fence_summaries_link_to_the_canonical_staged_runbook() -> None:
    assert "runbooks/kubernetes-deploy.md#staged-worker-fence-upgrade" in _INSTALL.read_text()
    assert "kubernetes-deploy.md#staged-worker-fence-upgrade" in _BUILD_USE_RECOVERY.read_text()

    assert (
        "../../../docs/operating/runbooks/kubernetes-deploy.md#staged-worker-fence-upgrade"
        in _HELM_REFERENCE.read_text()
    )
