from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


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
