"""End-to-end compose smoke for the built image (ADR-0359).

Where :mod:`tests.image.test_image_smoke` asserts the entrypoint dispatches past
argparse without any backend, this test proves the *built image runs*: it brings up the
repo ``docker-compose.yml`` app tier against real Postgres/MinIO/OIDC backends, runs the
``migrate`` one-shot, and waits for the ``server`` healthcheck — which polls ``/readyz`` —
to report healthy. A green ``docker compose up --wait server`` therefore attests the exact
acceptance chain: *migrate completes, then server reaches ``/readyz``* on ``$KDIVE_IMAGE``.

Opt-in: set ``KDIVE_IMAGE`` to a built image tag and have ``docker`` (with the compose v2
plugin) on PATH. The image is used as-is via the compose ``KDIVE_IMAGE`` override, so it is
never rebuilt here. The CI ``image-build`` job runs this against the amd64 ``kdive:ci`` it
just built. It is *arch-native*: the server's ``/readyz`` gates on the OIDC mock, which
publishes no ppc64le image (ADR-0356, mirror tracked #1183), so the ppc64le runtime proof
is deferred to that mirror; ppc64le is gated by the buildx build-proof until then.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_COMPOSE_FILE = Path(__file__).resolve().parents[2] / "docker-compose.yml"
#: Drops host port publishing so the smoke never collides with a locally-bound port; /readyz
#: is asserted inside the compose network, so no published port is needed (see the override).
_OVERRIDE_FILE = Path(__file__).resolve().parent / "compose.smoke.override.yml"


def _compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(
        ["docker", "compose", "version"],
        capture_output=True,
        text=True,
    )
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(
    os.environ.get("KDIVE_IMAGE") is None or not _compose_available(),
    reason="set KDIVE_IMAGE and have docker + the compose v2 plugin to run the compose smoke",
)

#: Isolate this run's containers/network/volumes from any local `docker compose up`.
_PROJECT = "kdive-smoke"


def _compose(*args: str, timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(_COMPOSE_FILE),
            "-f",
            str(_OVERRIDE_FILE),
            "-p",
            _PROJECT,
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_migrate_then_server_reaches_readyz() -> None:
    # `up --wait server` starts the self-contained graph (postgres → migrate one-shot →
    # minio + bucket-init → oidc → server) and blocks until the server healthcheck passes
    # or a dependency fails. The healthcheck GETs /readyz, so a zero exit proves migrate
    # completed and the server reached readiness on the built image.
    try:
        up = _compose(
            "up",
            "--detach",
            "--wait",
            "--wait-timeout",
            "240",
            "server",
            timeout=360,
        )
        if up.returncode != 0:
            logs = _compose("logs", "--no-color", timeout=60)
            pytest.fail(
                "compose up --wait server did not reach a healthy /readyz\n"
                f"--- up stdout ---\n{up.stdout}\n--- up stderr ---\n{up.stderr}\n"
                f"--- service logs ---\n{logs.stdout}\n{logs.stderr}"
            )
    finally:
        # `down -v` drops the containers, network, and the named build/install volumes so a
        # rerun starts clean; run even on failure so a broken attempt leaves nothing behind.
        _compose("down", "--volumes", "--remove-orphans", timeout=120)
