"""Operator documentation must describe each deployment's actual process topology."""

import re
from pathlib import Path

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
    assert "dedicated lifecycle-witness" in text
    assert "**Compose:**" in text
    assert "operator-side lifecycle wrapper" in text
    assert compose_guidance.lower().index("stop old workers") < compose_guidance.lower().index(
        "migrating the roles"
    )


def test_kubernetes_migrations_keep_the_witness_alive_until_workers_terminate() -> None:
    for path in (_INSTALL, _HELM_REFERENCE, _BUILD_USE_RECOVERY, _KUBERNETES_RUNBOOK):
        text = _normalized(path).lower()
        assert "scale workers to zero while the lifecycle-witness remains healthy" in text, path
        assert "wait until worker pods and their finalizers are gone" in text, path
        assert "then stop the lifecycle-witness" in text, path
        assert "start and verify the lifecycle-witness before starting workers" in text, path
