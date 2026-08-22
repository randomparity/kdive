# tests/scripts/test_check_pr_body.py
"""Behavioral tests for scripts/check-pr-body.sh.

The checker guards pull-request and issue body text, the channel that published five live
keys in PR #2037. It runs two independent checks: gitleaks pattern rules, and a count of
bare KEY=VALUE lines that marks a pasted process environment.

Most tests stub gitleaks through KDIVE_GITLEAKS, so they exercise the environment-dump
check on a host with no gitleaks installed. The tests that prove the pattern rules fire
need the real binary and skip without it.
"""

from __future__ import annotations

import hashlib
import shutil
import string
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check-pr-body.sh"
BASH = shutil.which("bash")
GITLEAKS = shutil.which("gitleaks")

# No requires_bash mark here on purpose. The checker sticks to bash 3.2 constructs, and it is
# verified against /bin/bash 3.2.57 on macOS, so a version mark would skip this whole module on
# the one host where a developer runs it before opening a PR.

needs_gitleaks = pytest.mark.skipif(GITLEAKS is None, reason="gitleaks is not installed")


def _fake_hf_token() -> str:
    """Build a synthetic token with the Hugging Face shape: `hf_` and 34 letters.

    The value is derived at run time instead of written as a literal. A token-shaped
    literal in the tree would trip the repository's own detect-secrets hook, and every
    scanner a reader points at this checkout, for a string that is not a credential.

    The rule needs both halves of the shape to fire, and each half cost a test run to
    learn: letters only (a digit drops the match to the weaker generic-api-key rule) and
    enough entropy (a sequential alphabet does not match at all).
    """
    alphabet = string.ascii_letters
    digest = hashlib.sha512(b"kdive-check-pr-body-fixture").digest()
    return "hf_" + "".join(alphabet[byte % len(alphabet)] for byte in digest[:34])


FAKE_HF_TOKEN = _fake_hf_token()


def _stub_gitleaks(tmp_path: Path) -> Path:
    """A gitleaks that always reports clean, so a test can isolate the env-dump check."""
    stub = tmp_path / "gitleaks-stub"
    stub.write_text("#!/usr/bin/env bash\nexit 0\n")
    stub.chmod(0o755)
    return stub


def _run(*args: str, gitleaks: Path | str | None = None) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin"}
    if gitleaks is not None:
        env["KDIVE_GITLEAKS"] = str(gitleaks)
    return subprocess.run(
        [BASH, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_a_normal_pr_body_passes_silently(tmp_path: Path) -> None:
    """A body of ordinary prose must produce no output and no failure."""
    body = tmp_path / "body.md"
    body.write_text(
        "Closes #2036.\n\n## Fix\n\nup.sh now runs the role-bootstrap one-shot itself,\n"
        "between the migrations and host-process phases.\n"
    )
    result = _run(str(body), gitleaks=_stub_gitleaks(tmp_path))
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""


def test_a_pasted_environment_fails(tmp_path: Path) -> None:
    """The shape check must fail a body carrying a pasted `env` dump (#2037)."""
    body = tmp_path / "body.md"
    body.write_text("\n".join(f"VAR_NUMBER_{n}=value{n}" for n in range(20)) + "\n")
    result = _run(str(body), gitleaks=_stub_gitleaks(tmp_path))
    assert result.returncode == 1
    assert "pasted process environment" in result.stderr


def test_the_environment_report_withholds_every_value(tmp_path: Path) -> None:
    """CI logs on a public repository are public, so the report must name variables only.

    A checker that echoed the value would republish the credential it just caught.
    """
    body = tmp_path / "body.md"
    lines = [f"FILLER_VAR_{n}=x" for n in range(15)]
    # Base64 of "super-secret-value" — the shape of the one credential in #2037 that no
    # pattern rule caught. pragma: allowlist secret
    lines.append("ATLASSIAN_MCP_BASIC_AUTH=c3VwZXItc2VjcmV0LXZhbHVl")  # pragma: allowlist secret
    body.write_text("\n".join(lines) + "\n")
    result = _run(str(body), gitleaks=_stub_gitleaks(tmp_path))
    assert result.returncode == 1
    assert "ATLASSIAN_MCP_BASIC_AUTH" in result.stderr, "the variable name must be reported"
    assert "c3VwZXItc2VjcmV0LXZhbHVl" not in result.stderr, "the value must never be printed"
    assert "c3VwZXItc2VjcmV0LXZhbHVl" not in result.stdout, "the value must never be printed"


def test_prose_that_cites_a_variable_is_not_an_environment_dump(tmp_path: Path) -> None:
    """The count is anchored at line start, so mid-sentence mentions must not accumulate.

    Real bodies in this repository cite KDIVE_* tokens constantly. Measured over 60
    consecutive real pull-request bodies, none had a single bare KEY=VALUE line.
    """
    body = tmp_path / "body.md"
    body.write_text(
        "".join(
            f"Set KDIVE_LOCAL_ROLE_BOOTSTRAP={n} to pick arm {n} of the escape hatch.\n"
            for n in range(30)
        )
    )
    result = _run(str(body), gitleaks=_stub_gitleaks(tmp_path))
    assert result.returncode == 0, result.stderr


def test_a_few_bare_assignments_stay_under_the_limit(tmp_path: Path) -> None:
    """A short shell snippet in a body is legitimate; only a dump-sized run fails."""
    body = tmp_path / "body.md"
    body.write_text(
        "KDIVE_LOCAL_ROLE_BOOTSTRAP=1\n"  # pragma: allowlist secret
        "KDIVE_BACKEND_SERVICES=postgres\n"  # pragma: allowlist secret
    )
    result = _run(str(body), gitleaks=_stub_gitleaks(tmp_path))
    assert result.returncode == 0, result.stderr


@needs_gitleaks
def test_a_token_pattern_fails_even_without_an_environment_dump(tmp_path: Path) -> None:
    """One credential in prose must fail on the gitleaks rule alone."""
    body = tmp_path / "body.md"
    body.write_text(f"Export the token first:\n\n    HF_TOKEN={FAKE_HF_TOKEN}\n")
    result = _run(str(body), gitleaks=GITLEAKS)
    assert result.returncode == 1
    assert "huggingface" in result.stderr.lower()


@needs_gitleaks
def test_the_gitleaks_report_withholds_the_matched_value(tmp_path: Path) -> None:
    """gitleaks must run with --redact, so the matched secret stays out of the log."""
    body = tmp_path / "body.md"
    body.write_text(f"HF_TOKEN={FAKE_HF_TOKEN}\n")
    result = _run(str(body), gitleaks=GITLEAKS)
    assert result.returncode == 1
    assert FAKE_HF_TOKEN not in result.stderr, "the matched value must never be printed"
    assert FAKE_HF_TOKEN not in result.stdout, "the matched value must never be printed"


def test_the_failure_message_names_the_body_file_rule(tmp_path: Path) -> None:
    """A failure must point at the prevention, not only at the detection."""
    body = tmp_path / "body.md"
    body.write_text("\n".join(f"VAR_{n}=v" for n in range(20)) + "\n")
    result = _run(str(body), gitleaks=_stub_gitleaks(tmp_path))
    assert "--body-file" in result.stderr
    assert "rotate" in result.stderr, "an exposed key must be rotated, not only removed"


def test_a_missing_gitleaks_fails_loudly(tmp_path: Path) -> None:
    """A guard that cannot run must fail, never report a clean body it did not scan."""
    body = tmp_path / "body.md"
    body.write_text("ordinary prose\n")
    result = _run(str(body), gitleaks=tmp_path / "definitely-not-here")
    assert result.returncode == 2
    assert "gitleaks not found" in result.stderr


def test_a_crashing_gitleaks_is_an_error_not_a_leak(tmp_path: Path) -> None:
    """gitleaks exits 1 for its own startup errors too, so the verdicts must stay distinct.

    A broken scanner must exit 2 (could not run), never 1 (this body carries a secret).
    """
    stub = tmp_path / "gitleaks-broken"
    stub.write_text("#!/usr/bin/env bash\necho 'unknown flag' >&2\nexit 1\n")
    stub.chmod(0o755)
    body = tmp_path / "body.md"
    body.write_text("ordinary prose\n")
    result = _run(str(body), gitleaks=stub)
    assert result.returncode == 2
    assert "failed to run" in result.stderr


def test_a_missing_file_is_an_error_not_a_pass(tmp_path: Path) -> None:
    """A typo in the path must not read as a clean scan."""
    result = _run(str(tmp_path / "absent.md"), gitleaks=_stub_gitleaks(tmp_path))
    assert result.returncode == 2
    assert "no such file" in result.stderr


def test_no_arguments_prints_usage(tmp_path: Path) -> None:
    result = _run(gitleaks=_stub_gitleaks(tmp_path))
    assert result.returncode == 2
    assert "usage:" in result.stderr
