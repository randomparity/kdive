#!/usr/bin/env python3
"""Exercise the real live_vm_host TLS expiry tasks against generated certificates."""

import datetime
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).resolve().parents[3]
ANSIBLE = ROOT / "deploy/ansible"
TASKS = ANSIBLE / "roles/live_vm_host/tasks/tls_expiry.yml"
MARGIN = 43200
NOW = datetime.datetime.now(datetime.UTC)


def certificate(path: Path, expires_in: datetime.timedelta) -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, path.stem)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW - datetime.timedelta(days=2))
        .not_valid_after(NOW + expires_in)
        .sign(key, hashes.SHA256())
    )
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return str(path)


def run_case(
    name: str,
    certificates: dict[str, datetime.timedelta | None],
    *,
    margin: object = MARGIN,
    failure: str | None = None,
    passing: tuple[str, ...] = (),
    openssl: bool = True,
) -> None:
    with tempfile.TemporaryDirectory(prefix="kdive-tls-expiry-test-") as directory:
        work = Path(directory)
        entries = []
        for credential, expires_in in certificates.items():
            path = work / f"{credential}.pem"
            if expires_in is None:
                path.write_text("not a certificate\n")
            else:
                certificate(path, expires_in)
            entries.append({"name": credential, "path": str(path)})
        empty = work / "empty-path"
        empty.mkdir()
        play = work / "play.yml"
        play.write_text(
            yaml.safe_dump(
                [
                    {
                        "hosts": "localhost",
                        "connection": "local",
                        "gather_facts": False,
                        "environment": {} if openssl else {"PATH": str(empty)},
                        "vars": {
                            "ansible_python_interpreter": sys.executable,
                            "live_vm_host_authority_tls_expiry_margin_seconds": margin,
                            "live_vm_host_tls_expiry_certificates": entries,
                        },
                        "tasks": [
                            {"ansible.builtin.import_tasks": str(TASKS)},
                            {"ansible.builtin.debug": {"msg": "tls-expiry-completed"}},
                        ],
                    }
                ]
            )
        )
        result = subprocess.run(
            ["ansible-playbook", "-i", "localhost,", str(play)],
            env={**os.environ, "ANSIBLE_CONFIG": str(ANSIBLE / "ansible.cfg")},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
        text = result.stdout + result.stderr
        assert (result.returncode != 0) == bool(failure), (name, text)
        assert ("tls-expiry-completed" in text) != bool(failure), (name, text)
        if failure:
            assert failure in text, (name, text)
        for credential in passing:
            assert f"TLS credential {credential} " not in text, (name, text)
        print(f"ok {name}")


DAY = datetime.timedelta(days=1)
HOUR = datetime.timedelta(hours=1)
# Six hours left is inside the 12-hour margin.
run_case("valid", {"server-certificate": 2 * DAY})
run_case(
    "inside-margin",
    {"server-certificate": 6 * HOUR},
    failure="TLS credential server-certificate",
)
run_case("expired", {"server-certificate": -DAY}, failure="notAfter=")
run_case(
    "mixed",
    {"server-ca": 2 * DAY, "health-client-certificate": -DAY},
    failure="TLS credential health-client-certificate",
    passing=("server-ca",),
)
run_case("not-a-certificate", {"server-ca": None}, failure="is not a readable PEM certificate")
run_case(
    "bad-margin",
    {"server-ca": 2 * DAY},
    margin="-1",
    failure="must be a non-negative whole number",
)
run_case(
    "openssl-absent",
    {"server-ca": 2 * DAY},
    failure="could not be checked with openssl",
    openssl=False,
)
print("ok authority-tls-expiry: 7 cases")
