"""Shared real TLS certificate fixtures for authority transport tests."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from kdive.providers.external_boot_authority.transport import authority_server_name


def _certificate(
    name: str,
    key: ec.EllipticCurvePrivateKey,
    *,
    issuer: x509.Certificate | None = None,
    issuer_key: ec.EllipticCurvePrivateKey | None = None,
    server: bool = False,
    valid: bool = True,
) -> x509.Certificate:
    now = datetime.now(UTC)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    signing_key = issuer_key or key
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer.subject if issuer else subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=2))
        .not_valid_after(now + timedelta(minutes=5) if valid else now - timedelta(minutes=1))
        .add_extension(x509.BasicConstraints(ca=issuer is None, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=issuer is None,
                crl_sign=issuer is None,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(signing_key.public_key()),
            critical=False,
        )
    )
    if issuer is not None:
        eku = ExtendedKeyUsageOID.SERVER_AUTH if server else ExtendedKeyUsageOID.CLIENT_AUTH
        builder = builder.add_extension(x509.ExtendedKeyUsage([eku]), critical=False)
    if server:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(authority_server_name(name))]), critical=False
        )
    return builder.sign(signing_key, hashes.SHA256())


def _tls_material(
    root: Path, instance: str, *, server_valid: bool = True, trusted_client: bool = True
) -> dict[str, Path]:
    keys = {
        name: ec.generate_private_key(ec.SECP256R1())
        for name in ("ca", "foreign", "server", "client")
    }
    ca = _certificate("test CA", keys["ca"])
    foreign = _certificate("foreign CA", keys["foreign"])
    certificates = {
        "server_ca": ca,
        "foreign_ca": foreign,
        "server_certificate": _certificate(
            instance,
            keys["server"],
            issuer=ca,
            issuer_key=keys["ca"],
            server=True,
            valid=server_valid,
        ),
        "client_certificate": _certificate(
            "test client",
            keys["client"],
            issuer=ca if trusted_client else foreign,
            issuer_key=keys["ca" if trusted_client else "foreign"],
        ),
    }
    values = {
        name: certificate.public_bytes(serialization.Encoding.PEM)
        for name, certificate in certificates.items()
    }
    for name in ("server", "client"):
        values[f"{name}_key"] = keys[name].private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    paths = {}
    for name, value in values.items():
        path = root / name.replace("_", "-")
        path.write_bytes(value)
        path.chmod(0o400)
        paths[name] = path
    return paths
