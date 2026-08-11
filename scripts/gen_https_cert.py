"""Generate a LAN self-signed TLS cert for Antigravity Butler on Windows (.105)."""
from __future__ import annotations

import datetime as dt
import ipaddress
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).resolve().parent.parent
CERT_DIR = ROOT / "certs"
KEY_PATH = CERT_DIR / "butler.key"
CERT_PATH = CERT_DIR / "butler.crt"

HOSTS = [
    "localhost",
    "127.0.0.1",
    "192.168.1.105",
    "kiddlaptop",
]


def main() -> None:
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "TW"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "PastMemory Homelab"),
        x509.NameAttribute(NameOID.COMMON_NAME, "192.168.1.105"),
    ])
    alt_names: list[x509.GeneralName] = []
    for h in HOSTS:
        try:
            alt_names.append(x509.IPAddress(ipaddress.ip_address(h)))
        except ValueError:
            alt_names.append(x509.DNSName(h))

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1))
        .not_valid_after(dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName(alt_names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    KEY_PATH.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    CERT_PATH.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    print(f"Wrote {KEY_PATH}")
    print(f"Wrote {CERT_PATH}")


if __name__ == "__main__":
    main()
