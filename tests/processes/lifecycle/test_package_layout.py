"""Deployment lifecycle modules live under one process subpackage."""

import importlib

from kdive.processes.lifecycle.lifecycle_witness import run_lifecycle_witness_body
from kdive.processes.lifecycle.worker_incarnation import worker_incarnation_id

_LIFECYCLE_MODULES = (
    "compose.compose_worker_lifecycle",
    "compose.docker_death_api",
    "kubernetes.kubernetes_credential_broker",
    "kubernetes.kubernetes_credential_init",
    "kubernetes.kubernetes_termination_witness",
    "lifecycle_witness",
    "worker_incarnation",
)


def test_deployment_lifecycle_modules_use_the_lifecycle_package() -> None:
    for leaf in _LIFECYCLE_MODULES:
        module_name = f"kdive.processes.lifecycle.{leaf}"
        assert importlib.import_module(module_name).__name__ == module_name

    assert run_lifecycle_witness_body.__module__ == ("kdive.processes.lifecycle.lifecycle_witness")
    assert worker_incarnation_id.__module__ == "kdive.processes.lifecycle.worker_incarnation"
