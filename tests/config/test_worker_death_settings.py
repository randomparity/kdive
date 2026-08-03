"""Central worker-incarnation and death-authority configuration."""

import pytest

import kdive.config as config
from kdive.config.core_settings import (
    DOCKER_DEATH_API,
    KUBERNETES_WITNESS_ORDINAL_CEILING,
    POD_NAME,
    POD_NAMESPACE,
    POD_UID,
    WORKER_DEATH_VERIFIER,
    WORKER_INCARNATION_KIND,
)
from kdive.domain.errors import CategorizedError


def test_worker_death_settings_have_fail_closed_defaults() -> None:
    config.load({})
    assert config.require(WORKER_INCARNATION_KIND) == "local"
    assert config.require(WORKER_DEATH_VERIFIER) == "disabled"
    assert config.require(DOCKER_DEATH_API) == "http://worker-death-api:2375"
    assert config.get(POD_NAMESPACE) is None
    assert config.get(POD_NAME) is None
    assert config.get(POD_UID) is None


@pytest.mark.parametrize(
    ("setting", "value"),
    [(WORKER_INCARNATION_KIND, "heartbeat"), (WORKER_DEATH_VERIFIER, "heartbeat")],
)
def test_worker_death_mode_rejects_non_authoritative_values(setting, value: str) -> None:
    config.load({setting.name: value})
    with pytest.raises(CategorizedError):
        config.require(setting)


def test_kubernetes_worker_identity_requires_all_downward_api_fields() -> None:
    config.load({"KDIVE_WORKER_INCARNATION_KIND": "kubernetes"})
    with pytest.raises(CategorizedError, match="KDIVE_POD_NAMESPACE"):
        config.validate("worker")

    config.load(
        {
            "KDIVE_DATABASE_URL": "postgresql://db/kdive",
            "KDIVE_S3_ENDPOINT_URL": "http://minio:9000",
            "KDIVE_S3_BUCKET": "kdive",
            "KDIVE_WORKER_INCARNATION_KIND": "kubernetes",
            "KDIVE_POD_NAMESPACE": "kdive",
            "KDIVE_POD_NAME": "kdive-worker-0",
            "KDIVE_POD_UID": "pod-uid",
        }
    )
    config.validate("worker")


@pytest.mark.parametrize("ceiling", ["-1", "1001"])
def test_kubernetes_witness_ceiling_is_bounded_to_one_thousand_rows(ceiling: str) -> None:
    """The witness rejects a configured pass outside its hard row ceiling."""
    config.load(
        {
            "KDIVE_DATABASE_URL": "postgresql://db/kdive",
            "KDIVE_S3_ENDPOINT_URL": "http://minio:9000",
            "KDIVE_S3_BUCKET": "kdive",
            KUBERNETES_WITNESS_ORDINAL_CEILING.name: ceiling,
        }
    )

    with pytest.raises(CategorizedError):
        config.validate("lifecycle-witness")


def test_kubernetes_witness_ceiling_help_states_the_complete_pass_contract() -> None:
    """The generated operator reference cannot omit this witness pass's limits or recovery."""
    help_text = KUBERNETES_WITNESS_ORDINAL_CEILING.help.lower()

    for fragment in (
        "pod",
        "kubernetes api",
        "no reference clock",
        "each witness pass observes the kubernetes api",
        "per witness pass",
        "1,000",
        "valid inclusive range is 0..1,000",
        "every out-of-range value (negative or above 1,000) is rejected at witness startup",
        "no cursor",
        "remaining terminal pods",
        "next scheduled invocation",
        "set kdive_kubernetes_witness_ordinal_ceiling to an "
        "integer in the inclusive range 0..1,000",
        "restart the lifecycle witness",
    ):
        assert fragment in help_text
    assert "remaining finalized pods" not in help_text
