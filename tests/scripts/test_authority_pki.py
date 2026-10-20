# tests/scripts/test_authority_pki.py
"""Behavioral tests for scripts/operations/authority-pki.sh (ADR-0803, #3273).

Every case drives the real script and real openssl in a private temporary directory, then
inspects the produced certificates with ``cryptography``.
"""

from __future__ import annotations

import datetime
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from kdive.providers.external_boot_authority.protocol import authority_server_name

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "operations" / "authority-pki.sh"
BASH = shutil.which("bash")
INSTANCE = "authority-test-instance"
DAY = datetime.timedelta(days=1)


def _env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    return subprocess.run(
        [BASH, str(SCRIPT), *args], env=_env(), capture_output=True, text=True, check=False
    )


def _ok(*args: str) -> str:
    result = _run(*args)
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.fixture
def pki(tmp_path: Path) -> Path:
    directory = tmp_path / "pki"
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    return directory


def _ca(pki: Path, days: int = 30, key: str = "new", subject: str = "kdive-test-ca") -> str:
    return _ok("ca", "--dir", str(pki), "--subject", subject, "--days", str(days), "--key", key)


def _leaf(pki: Path, name: str, usage: str, days: int = 7, key: str = "new") -> str:
    extra = ["--authority-instance", INSTANCE] if usage == "server" else []
    return _ok(
        "leaf", "--dir", str(pki), "--name", name, "--usage", usage,
        "--days", str(days), "--key", key, *extra,
    )  # fmt: skip


def _cert(path: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(path.read_bytes())


def _spki(cert: x509.Certificate) -> bytes:
    return cert.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def _verify(pki: Path, leaf: Path, purpose: str, ca: Path | None = None) -> None:
    result = subprocess.run(
        ["openssl", "verify", "-x509_strict", "-purpose", purpose,
         "-CAfile", str(ca or pki / "ca.pem"), str(leaf)],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert result.returncode == 0, result.stdout + result.stderr


def test_issues_the_carrier_profile(pki: Path) -> None:
    _ca(pki, days=30)
    _leaf(pki, "server", "server", days=7)
    _leaf(pki, "health-client", "client", days=7)

    ca = _cert(pki / "ca.pem")
    assert ca.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == "kdive-test-ca"
    constraints = ca.extensions.get_extension_for_class(x509.BasicConstraints)
    assert constraints.critical and constraints.value.ca and constraints.value.path_length == 0
    ca_usage = ca.extensions.get_extension_for_class(x509.KeyUsage)
    assert ca_usage.critical and ca_usage.value.key_cert_sign and ca_usage.value.crl_sign

    server = _cert(pki / "server.pem")
    client = _cert(pki / "health-client.pem")
    for leaf, eku in (
        (server, ExtendedKeyUsageOID.SERVER_AUTH),
        (client, ExtendedKeyUsageOID.CLIENT_AUTH),
    ):
        public_key = leaf.public_key()
        assert isinstance(public_key, ec.EllipticCurvePublicKey)
        assert public_key.curve.name == "secp256r1"
        assert not leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
        usage = leaf.extensions.get_extension_for_class(x509.KeyUsage)
        assert usage.critical and usage.value.digital_signature
        assert list(leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value) == [eku]
    names = server.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert names.get_values_for_type(x509.DNSName) == [authority_server_name(INSTANCE)]
    with pytest.raises(x509.ExtensionNotFound):
        client.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    _verify(pki, pki / "server.pem", "sslserver")
    _verify(pki, pki / "health-client.pem", "sslclient")


def test_server_name_matches_the_authority_derivation() -> None:
    out = _ok("server-name", "--authority-instance", INSTANCE)
    assert out == authority_server_name(INSTANCE) + "\n"


def test_lifetimes_follow_days(pki: Path) -> None:
    _ca(pki, days=30)
    _leaf(pki, "server", "server", days=7)
    for name, days in (("ca.pem", 30), ("server.pem", 7)):
        cert = _cert(pki / name)
        lifetime = cert.not_valid_after_utc - cert.not_valid_before_utc
        assert lifetime == days * DAY, (name, lifetime)


def test_files_are_private_and_output_carries_no_key(pki: Path) -> None:
    out = _ca(pki) + _leaf(pki, "worker-client", "client")
    assert {p.name for p in pki.iterdir()} == {
        "ca.key", "ca.pem", "ca.srl", "worker-client.key", "worker-client.pem",
    }  # fmt: skip
    for path in pki.iterdir():
        assert path.stat().st_mode & 0o777 == 0o600, path
    assert "PRIVATE KEY" not in out
    assert "issued ca.pem notAfter=" in out and "issued worker-client.pem notAfter=" in out


def test_renewal_keeps_keys_and_trust(pki: Path) -> None:
    _ca(pki, days=30)
    _leaf(pki, "server", "server", days=7)
    old_ca, old_server = _cert(pki / "ca.pem"), _cert(pki / "server.pem")
    old_ca_copy = pki.parent / "old-ca.pem"
    shutil.copy(pki / "ca.pem", old_ca_copy)

    _ca(pki, days=60, key="existing")
    _leaf(pki, "server", "server", days=14, key="existing")
    new_ca, new_server = _cert(pki / "ca.pem"), _cert(pki / "server.pem")

    assert _spki(new_ca) == _spki(old_ca)
    assert _spki(new_server) == _spki(old_server)
    assert new_ca.serial_number != old_ca.serial_number
    assert new_server.serial_number != old_server.serial_number
    old_server_copy = pki.parent / "old-server.pem"
    old_server_copy.write_bytes(old_server.public_bytes(serialization.Encoding.PEM))
    _verify(pki, old_server_copy, "sslserver")
    _verify(pki, pki / "server.pem", "sslserver", ca=old_ca_copy)


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("ca", "--subject", "kdive-test-ca", "--days", "1", "--key", "new"), "already exists"),
        (("ca", "--subject", "other-ca", "--days", "1", "--key", "existing"), "does not match"),
        (("ca", "--subject", "kdive-test-ca", "--days", "0", "--key", "existing"), "--days"),
        (("ca", "--subject", "kdive-test-ca", "--days", "1.5", "--key", "existing"), "--days"),
        (("ca", "--subject", "kdive-test-ca", "--days", "1", "--key", "maybe"), "--key"),
        (("leaf", "--name", "server", "--usage", "server", "--days", "1", "--key", "new"),
         "--authority-instance"),
        (("leaf", "--name", "c", "--usage", "client", "--days", "1", "--key", "new",
          "--authority-instance", INSTANCE), "--authority-instance"),
        (("leaf", "--name", "c", "--usage", "client", "--days", "31", "--key", "new"),
         "CA certificate expires"),
        (("leaf", "--name", "c", "--usage", "client", "--days", "1", "--key", "existing"),
         "c.key does not exist"),
        (("leaf", "--name", "../c", "--usage", "client", "--days", "1", "--key", "new"), "--name"),
        (("leaf", "--name", "ca", "--usage", "client", "--days", "1", "--key", "new"), "--name"),
        (("leaf", "--name", "c", "--usage", "peer", "--days", "1", "--key", "new"), "--usage"),
    ],
)  # fmt: skip
def test_refusals(pki: Path, args: tuple[str, ...], message: str) -> None:
    _ca(pki, days=30)
    before = {p.name: p.read_bytes() for p in pki.iterdir()}
    result = _run(args[0], "--dir", str(pki), *args[1:])
    assert result.returncode != 0
    assert message in result.stderr, result.stderr
    assert {p.name: p.read_bytes() for p in pki.iterdir()} == before


def test_existing_ca_and_leaf_require_their_keys(pki: Path) -> None:
    result = _run("ca", "--dir", str(pki), "--subject", "x", "--days", "1", "--key", "existing")
    assert result.returncode != 0 and "ca.key does not exist" in result.stderr
    result = _run("leaf", "--dir", str(pki), "--name", "c", "--usage", "client",
                  "--days", "1", "--key", "new")  # fmt: skip
    assert result.returncode != 0 and "ca.key does not exist" in result.stderr
    assert list(pki.iterdir()) == []


def test_renewed_ca_must_cover_existing_leaves(pki: Path) -> None:
    _ca(pki, days=30)
    _leaf(pki, "server", "server", days=20)
    before = (pki / "ca.pem").read_bytes()
    result = _run("ca", "--dir", str(pki), "--subject", "kdive-test-ca", "--days", "10",
                  "--key", "existing")  # fmt: skip
    assert result.returncode != 0
    assert "server.pem would outlive" in result.stderr
    assert (pki / "ca.pem").read_bytes() == before


def test_failed_issuance_keeps_the_previous_certificate(pki: Path, tmp_path: Path) -> None:
    _ca(pki, days=30)
    _leaf(pki, "server", "server", days=7)
    before = (pki / "server.pem").read_bytes()
    other = tmp_path / "other"
    other.mkdir(mode=0o700)
    _ca(other, days=30)
    shutil.copy(other / "ca.key", pki / "ca.key")
    result = _run("leaf", "--dir", str(pki), "--name", "server", "--usage", "server",
                  "--days", "7", "--key", "existing", "--authority-instance", INSTANCE)  # fmt: skip
    assert result.returncode != 0
    assert (pki / "server.pem").read_bytes() == before
    assert sorted(p.name for p in pki.iterdir()) == sorted(
        ["ca.key", "ca.pem", "ca.srl", "server.key", "server.pem"]
    )


_CA_NEW = ("ca", "--subject", "x", "--days", "1", "--key", "new")


def _ca_result(directory: Path) -> subprocess.CompletedProcess[str]:
    return _run(*_CA_NEW, "--dir", str(directory))


def test_refuses_shared_or_linked_directories(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o750)
    assert "mode 0700" in _ca_result(shared).stderr
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real)
    assert "not a symlink" in _ca_result(link).stderr
    assert "not a symlink" in _run(*_CA_NEW, "--dir", f"{link}/").stderr
    for dotted in (f"{link}/.", f"{link}/../real/../link"):
        assert ". or .. components" in _run(*_CA_NEW, "--dir", dotted).stderr
    assert "not a symlink" in _ca_result(tmp_path / "absent").stderr
    assert list(real.iterdir()) == [] and list(shared.iterdir()) == []


@pytest.mark.skipif(os.geteuid() != 0, reason="needs root to chown to another uid")
def test_refuses_a_directory_owned_by_someone_else(tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir(mode=0o700)
    os.chown(other, 65534, 65534)
    assert "owned by the invoking user" in _ca_result(other).stderr


def test_refuses_directories_inside_git(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], env=_env(), check=True)
    inside = repo / "pki"
    inside.mkdir(mode=0o700)
    admin = repo / ".git" / "pki"
    admin.mkdir(mode=0o700)
    for directory in (inside, admin):
        result = _ca_result(directory)
        assert result.returncode != 0
        assert "Git" in result.stderr, result.stderr
        assert list(directory.iterdir()) == []


def test_git_failure_fails_closed(tmp_path: Path) -> None:
    pki = tmp_path / "pki"
    pki.mkdir(mode=0o700)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "git"
    fake.write_text("#!/bin/sh\necho 'fatal: detected dubious ownership' >&2\nexit 128\n")
    fake.chmod(0o755)
    assert BASH is not None
    result = subprocess.run(
        [BASH, str(SCRIPT), *_CA_NEW, "--dir", str(pki)],
        env={**_env(), "PATH": f"{bindir}:{os.environ['PATH']}"},
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert result.returncode != 0
    assert "dubious ownership" in result.stderr
    assert list(pki.iterdir()) == []


def test_git_check_is_locale_independent(pki: Path) -> None:
    german = {**_env(), "LC_ALL": "de_DE.UTF-8", "LANGUAGE": "de"}
    probe = subprocess.run(
        ["git", "-C", str(pki), "rev-parse"], env=german, capture_output=True, text=True
    )
    if "not a git repository" in probe.stderr:
        pytest.skip("this host has no translated git messages to exercise the locale pin")
    assert BASH is not None
    result = subprocess.run(
        [BASH, str(SCRIPT), *_CA_NEW, "--dir", str(pki)],
        env={**_env(), "LC_ALL": "de_DE.UTF-8", "LANGUAGE": "de"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_ignores_exported_git_directory(tmp_path: Path, pki: Path) -> None:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], env=_env(), check=True)
    assert BASH is not None
    result = subprocess.run(
        [BASH, str(SCRIPT), *_CA_NEW, "--dir", str(pki)],
        env={**_env(), "GIT_DIR": str(repo / ".git"), "GIT_WORK_TREE": str(tmp_path)},
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
