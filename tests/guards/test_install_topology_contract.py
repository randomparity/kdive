"""Operator documentation must describe each deployment's actual process topology."""

from pathlib import Path

_ROOT = Path(__file__).parents[2]
_INSTALL = _ROOT / "docs/operating/install.md"
_COMPOSE_DOC = _ROOT / "docs/operating/docker-compose.md"
_COMPOSE_REFERENCE = _ROOT / "deploy/compose/README.md"
_HELM_REFERENCE = _ROOT / "deploy/helm/kdive/README.md"
_KUBERNETES_DOC = _ROOT / "docs/operating/kubernetes.md"


def test_install_documents_the_five_image_commands_and_kubernetes_witness_topology() -> None:
    text = " ".join(_INSTALL.read_text().split())

    for command in ("server", "worker", "reconciler", "lifecycle-witness", "migrate"):
        assert f"`{command}`" in text
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
    text = " ".join(_HELM_REFERENCE.read_text().split())

    assert "confined to the dedicated lifecycle-witness workload" in text
    assert "confined to the reconciler" not in text


def test_kubernetes_page_counts_the_dedicated_witness_as_a_long_running_workload() -> None:
    text = _KUBERNETES_DOC.read_text()

    assert "four long-running Kubernetes workloads" in text
