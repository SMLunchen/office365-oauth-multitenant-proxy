import datetime
import ipaddress
import logging
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

log = logging.getLogger(__name__)

CERT_DIR = Path(os.environ.get("CERT_DIR", "/data/certs"))
CERT_PATH = CERT_DIR / "smtp.crt"
KEY_PATH = CERT_DIR / "smtp.key"
VALIDITY_DAYS = 3650


def ensure_cert(hostname: str) -> tuple[Path, Path]:
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    if CERT_PATH.exists() and KEY_PATH.exists():
        if _cert_still_valid(CERT_PATH):
            return CERT_PATH, KEY_PATH
        log.info("Certificate expired, regenerating")

    log.info("Generating self-signed TLS certificate for %s", hostname)
    _generate(hostname)
    return CERT_PATH, KEY_PATH


def _cert_still_valid(cert_path: Path) -> bool:
    try:
        data = cert_path.read_bytes()
        cert = x509.load_pem_x509_certificate(data)
        return cert.not_valid_after_utc > datetime.datetime.now(datetime.timezone.utc)
    except Exception:
        return False


def _generate(hostname: str) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, hostname),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "SMTP OAuth Proxy"),
    ])

    san_entries = [x509.DNSName(hostname)]
    try:
        san_entries.append(x509.IPAddress(ipaddress.ip_address(hostname)))
    except ValueError:
        pass

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=VALIDITY_DAYS))
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    KEY_PATH.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    CERT_PATH.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    log.info("Certificate written to %s", CERT_PATH)
