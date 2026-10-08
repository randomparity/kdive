"""Developer setup installs tool dependencies and cannot succeed with a partial environment."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_LIBRARY = _ROOT / "scripts/lib/setup-developer.sh"


def _stub(path: Path, body: str) -> None:
    path.write_text("#!/bin/bash\nset -eu\n" + body)
    path.chmod(0o755)


def _run(tmp_path: Path, *, failure: str = "", arch: str = "x86_64"):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "install.log"
    _stub(bindir / "uname", 'if [[ "$1" == -s ]]; then echo Linux; else echo "$TEST_ARCH"; fi\n')
    _stub(
        bindir / "uv",
        """
if [[ "$*" == 'tool dir --bin' ]]; then echo "$TEST_BIN"; exit; fi
printf 'uv %s\\n' "$*" >> "$TEST_LOG"
[[ "$TEST_FAILURE" != uv ]]
""",
    )
    _stub(bindir / "shellcheck", 'echo "version: 0.10.0"\n')
    _stub(
        bindir / "docker",
        """
if [[ "$1" == compose ]]; then echo 'Docker Compose version v2'; exit; fi
[[ "$TEST_FAILURE" != docker ]]
""",
    )
    _stub(bindir / "pkg-config", "exit 0\n")
    _stub(bindir / "go", 'echo "mod example.test/module v1.0.0"\n')
    # No host cabal must run when exercising the POWER path.
    _stub(
        bindir / "cabal",
        (
            'printf "cabal %s\\n" "$*" >> "$TEST_LOG"\n'
            'printf "#!/bin/sh\\necho version: 0.11.0\\n" > "$TEST_BIN/shellcheck"\n'
        ),
    )
    script = tmp_path / "run.sh"
    script.write_text(f"""#!/bin/bash
set -euo pipefail
source '{_LIBRARY}'
distro=debian
distro_exact=ubuntu
host_arch="$TEST_ARCH"
required_packages=(libvirt-dev)
recommended_packages=(make)
command_exists() {{ [[ "$1" != promtool ]] && command -v "$1" >/dev/null 2>&1; }}
package_for() {{ printf '%s' "$1"; }}
arch_needs_rust() {{ [[ "$1" == ppc64le ]]; }}
probe_all() {{ :; }}
dev_install_packages() {{
  printf 'packages %s\\n' "$*" >> "$TEST_LOG"
  [[ "$TEST_FAILURE" != packages ]]
}}
dev_install_archive() {{
  printf 'archive %s\\n' "$*" >> "$TEST_LOG"
  [[ "$TEST_FAILURE" != checksum ]]
  if [[ "$1" == shellcheck ]]; then
    printf '#!/bin/sh\\necho version: 0.11.0\\n' > "$TEST_BIN/shellcheck"
  fi
}}
dev_install_shfmt() {{
  printf 'go shfmt %s\\n' "$*" >> "$TEST_LOG"
  [[ "$TEST_FAILURE" != go ]]
}}
dev_install_go_tool() {{
  printf 'go %s\\n' "$*" >> "$TEST_LOG"
  [[ "$TEST_FAILURE" != go ]]
}}
install_developer_dependencies
echo complete
""")
    result = subprocess.run(
        ["bash", str(script)],
        env={
            **os.environ,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "TEST_BIN": str(bindir),
            "TEST_LOG": str(log),
            "TEST_FAILURE": failure,
            "TEST_ARCH": arch,
        },
        text=True,
        capture_output=True,
        check=False,
    )
    return result, log.read_text()


def test_installs_all_developer_tool_families(tmp_path: Path) -> None:
    result, log = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "uv tool install prek==0.4.3" in log
    for tool in ("shfmt", "actionlint", "helm", "gitleaks"):
        assert f"go {tool} " in log
    assert "archive shellcheck " in log
    assert "archive promtool " in log
    assert "packages libvirt-dev make" in log
    assert "complete" in result.stdout
    assert "guestfs" not in log


@pytest.mark.parametrize("failure", ["packages", "uv", "checksum", "go", "docker"])
def test_install_or_verification_failure_prevents_success(tmp_path: Path, failure: str) -> None:
    result, _ = _run(tmp_path, failure=failure)
    assert result.returncode != 0
    assert "complete" not in result.stdout
    if failure == "docker":
        assert "unavailable to this session" in result.stderr


def test_power_builds_shellcheck_natively(tmp_path: Path) -> None:
    result, log = _run(tmp_path, arch="ppc64le")
    assert result.returncode == 0, result.stderr
    assert "cabal install ShellCheck-0.11.0" in log
    assert "archive shellcheck" not in log


def test_archive_checksum_failure_cannot_install_a_binary(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _stub(bindir / "curl", 'while [[ "$1" != --output ]]; do shift; done\necho bad > "$2"\n')
    marker = tmp_path / "extracted"
    _stub(bindir / "tar", f'touch "{marker}"\n')
    result = subprocess.run(
        [
            "bash",
            "-euo",
            "pipefail",
            "-c",
            'source "$1"; dev_bin="$2"; '
            'dev_install_archive tool https://example.test/tool "$3" tool',
            "test",
            str(_LIBRARY),
            str(bindir),
            "0" * 64,
        ],
        env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert not marker.exists()
    assert not (bindir / "tool").exists()


def test_setup_pins_match_ci_and_hooks() -> None:
    source = _LIBRARY.read_text()
    ci = (_ROOT / ".github/workflows/ci.yml").read_text()
    hooks = (_ROOT / ".pre-commit-config.yaml").read_text()
    body_scan = (_ROOT / ".github/workflows/pr-body-scan.yml").read_text()
    for version in ("v3.13.1", "v3.21.0", "0.11.0", "0.4.3"):
        assert version in source and version in ci
    assert "v1.7.12" in source and "v1.7.12" in hooks
    assert "8.30.1" in source and "8.30.1" in body_scan


def test_installed_go_tool_is_reused(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    selected = bindir / "tool"
    selected.touch()
    selected.chmod(0o755)
    _stub(bindir / "go", '[[ "$1" == version ]]\necho "mod example.test/tool v1.2.3"\n')
    result = subprocess.run(
        [
            "bash",
            "-euo",
            "pipefail",
            "-c",
            'source "$1"; dev_install_go_tool tool example.test/tool v1.2.3',
            "test",
            str(_LIBRARY),
        ],
        env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_helm_rebuilds_matching_version_without_release_flags(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _stub(bindir / "helm", "exit 0\n")
    metadata = tmp_path / "metadata"
    metadata.write_text("mod helm.sh/helm/v3 v3.21.0\n")
    _stub(
        bindir / "go",
        "\n".join(
            [
                'if [[ "$1" == version ]]; then cat "$TEST_METADATA"; exit; fi',
                '[[ "$1" == install && "$2" == -ldflags ]]',
                'printf "%s\\n" "$3" > "$TEST_METADATA"',
            ]
        ),
    )
    result = subprocess.run(
        [
            "bash",
            "-euo",
            "pipefail",
            "-c",
            'source "$1"; dev_bin="$2"; dev_install_helm',
            "test",
            str(_LIBRARY),
            str(bindir),
        ],
        env={
            **os.environ,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "TEST_METADATA": str(metadata),
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    flags = metadata.read_text()
    assert "internal/version.version=v3.21.0" in flags
    for package in ("pkg/lint/rules", "pkg/chartutil"):
        assert f"{package}.k8sVersionMajor=1" in flags
        assert f"{package}.k8sVersionMinor=35" in flags


@pytest.mark.parametrize(
    ("private", "ambient", "expected"),
    [
        ("v3.13.1", "v3.14.1", 7),
        (None, "v3.13.1", 7),
        (None, "v3.14.1", 1),
        (None, None, 1),
        ("v3.14.1", "v3.13.1", 1),
        ("nonexecutable", "v3.13.1", 1),
        ("dangling", "v3.13.1", 1),
        ("broken", "v3.13.1", 1),
    ],
)
def test_shfmt_selection_and_forwarding(
    tmp_path: Path, private: str | None, ambient: str | None, expected: int
) -> None:
    import shutil

    root = tmp_path / "checkout with spaces"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(_ROOT / "scripts/shfmt.sh", scripts / "shfmt.sh")
    bindir = tmp_path / "ambient"
    bindir.mkdir()
    (bindir / "bash").symlink_to("/bin/bash")
    for path, version in [
        (root / "build/dev-tools/bin/shfmt", private),
        (bindir / "shfmt", ambient),
    ]:
        if version is None:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if version == "dangling":
            path.symlink_to("absent")
        else:
            _stub(
                path,
                f'if [[ "$1" == --version ]]; then echo {version}; exit; fi\n'
                'printf "%s\\n" "$@"\nexit 7\n',
            )
            if version == "nonexecutable":
                path.chmod(0o644)
            elif version == "broken":
                _stub(path, "exit 9\n")
    result = subprocess.run(
        [str(scripts / "shfmt.sh"), "argument with spaces", "-d"],
        env={**os.environ, "PATH": str(bindir)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == expected, result.stderr
    if expected == 7:
        assert result.stdout.splitlines() == ["argument with spaces", "-d"]
    else:
        assert "run just setup" in result.stderr


@pytest.mark.parametrize(
    "initial,failure", [(None, ""), ("v3.14.1", ""), (None, "go"), (None, "version")]
)
def test_private_shfmt_install_preserves_host_and_reuses_exact_pin(
    tmp_path: Path, initial: str | None, failure: str
) -> None:
    shared = tmp_path / "user-bin"
    system = tmp_path / "system-bin"
    private = tmp_path / "checkout with spaces/build/dev-tools/bin"
    for path in (shared, system, private):
        path.mkdir(parents=True)
    _stub(system / "shfmt", "echo v3.14.1\n")
    original = (system / "shfmt").read_bytes()
    if initial:
        _stub(private / "shfmt", f"echo {initial}\n")
    log = tmp_path / "go.log"
    _stub(
        system / "go",
        """
[[ "$*" == 'install mvdan.cc/sh/v3/cmd/shfmt@v3.13.1' ]]
echo install >> "$TEST_LOG"
[[ "$TEST_FAILURE" != go ]]
version=v3.13.1
[[ "$TEST_FAILURE" != version ]] || version=v3.14.1
printf '#!/bin/bash\necho %s\n' "$version" > "$GOBIN/shfmt"
chmod +x "$GOBIN/shfmt"
""",
    )
    result = subprocess.run(
        [
            "bash",
            "-euo",
            "pipefail",
            "-c",
            'source "$1"; dev_install_shfmt "$2"; dev_install_shfmt "$2"; command -v shfmt',
            "test",
            str(_LIBRARY),
            str(private),
        ],
        env={
            **os.environ,
            "PATH": f"{shared}:{system}:/usr/bin:/bin",
            "TEST_LOG": str(log),
            "TEST_FAILURE": failure,
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert (system / "shfmt").read_bytes() == original
    assert not (shared / "shfmt").exists()
    if failure:
        assert result.returncode != 0
    else:
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == str(system / "shfmt")
        assert (
            subprocess.check_output([str(private / "shfmt"), "--version"], text=True).strip()
            == "v3.13.1"
        )
        assert log.read_text().splitlines() == ["install"]


def test_lint_shell_dispatches_both_formatter_invocations(tmp_path: Path) -> None:
    import shutil

    just = shutil.which("just")
    assert just is not None
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy2(_ROOT / "scripts/shfmt.sh", scripts / "shfmt.sh")
    shutil.copy2(_ROOT / "justfile", tmp_path / "justfile")
    private = tmp_path / "build/dev-tools/bin"
    private.mkdir(parents=True)
    log = tmp_path / "calls"
    _stub(
        private / "shfmt",
        'if [[ "$1" == --version ]]; then echo v3.13.1; exit; fi\n'
        'printf "%s\\n" "$*" >> "$TEST_LOG"\n',
    )
    ambient = tmp_path / "ambient"
    ambient.mkdir()
    _stub(ambient / "shfmt", "exit 99\n")
    _stub(ambient / "shellcheck", "exit 0\n")
    result = subprocess.run(
        [just, "--justfile", str(tmp_path / "justfile"), "lint-shell"],
        env={**os.environ, "PATH": f"{ambient}:{os.environ['PATH']}", "TEST_LOG": str(log)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    calls = log.read_text().splitlines()
    assert len(calls) == 2
    assert calls[0].startswith("-f scripts deploy/compose ")
    assert calls[1].startswith("-i 2 -d scripts deploy/compose ")
