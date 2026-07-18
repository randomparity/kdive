import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _grafana_supports_arch(arch: str) -> bool:
    """Source lib.sh and return the exit status of `grafana_supports_arch <arch>` as a bool."""
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{ROOT}/scripts/live-stack/lib.sh" && grafana_supports_arch "$1"',
            "_",
            arch,
        ],
        check=False,
    )
    return result.returncode == 0


def test_live_stack_env_exports_required_defaults() -> None:
    env = (ROOT / "scripts/live-stack/env.sh").read_text()
    required = [
        "KDIVE_DATABASE_URL",
        "KDIVE_OIDC_ISSUER",
        "KDIVE_OIDC_JWKS_URI",
        "KDIVE_OIDC_AUDIENCE",
        "KDIVE_S3_ENDPOINT_URL",
        "KDIVE_S3_BUCKET",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "KDIVE_BUILD_WORKSPACE",
        "KDIVE_BUILD_COMPONENT_ROOTS",
        "KDIVE_INSTALL_STAGING",
        "KDIVE_STACK_BASE_URL",
    ]
    for name in required:
        assert f"export {name}=" in env


def test_live_stack_scripts_are_strict_bash() -> None:
    for name in ("env.sh", "apply-migrations.sh", "up.sh", "down.sh", "status.sh"):
        text = (ROOT / "scripts/live-stack" / name).read_text()
        assert text.startswith("#!/usr/bin/env bash\n"), f"{name}: missing bash shebang"
        assert "\nset -euo pipefail\n" in text, f"{name}: missing 'set -euo pipefail'"


def test_restart_host_processes_starts_all_three() -> None:
    text = (ROOT / "scripts/live-stack/lib.sh").read_text()
    assert "restart_host_processes" in text
    assert "-m kdive server" in text
    assert "-m kdive reconciler" in text
    assert "-m kdive worker" in text


def test_grafana_gate_skips_ppc64le_and_keeps_other_arches() -> None:
    """The arch gate must skip grafana only where it has no manifest (ppc64le), not elsewhere.

    Executes the real predicate so an inverted or gutted gate fails, unlike a substring check.
    """
    assert _grafana_supports_arch("ppc64le") is False, "grafana has no ppc64le manifest (ADR-0356)"
    assert _grafana_supports_arch("x86_64") is True
    assert _grafana_supports_arch("aarch64") is True
    # An empty/unknown arch (no `uname`) must not silently skip grafana — attempt it best-effort.
    assert _grafana_supports_arch("") is True


def test_up_starts_prometheus_independently_of_grafana() -> None:
    """Prometheus comes up in its own `compose up`, so a grafana failure can't abort it (#1261)."""
    text = (ROOT / "scripts/live-stack/up.sh").read_text()
    assert "up -d prometheus" in text, "prometheus must be brought up on its own"
    assert "grafana_supports_arch" in text, "grafana must be gated on host arch"
    assert "#1261" in text, "the skip must be traceable to its tracking issue"
