from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from datetime import datetime, timedelta
from pathlib import Path


OUT_DIR = Path("certs")
OUT_DIR.mkdir(exist_ok=True)


def generate_key():
    return rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048
    )


def save_key(key, path):
    with open(path, "wb") as f:
        f.write(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption()
        ))


def save_cert(cert, path):
    with open(path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))


def create_ca():
    key = generate_key()

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "IN"),
        x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, "Karnataka"),
        x509.NameAttribute(NameOID.LOCALITY_NAME, "Bangalore"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "MyProject"),
        x509.NameAttribute(NameOID.COMMON_NAME, "MyRootCA"),
    ])

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.utcnow())
        .not_valid_after(datetime.utcnow() + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    save_key(key, OUT_DIR / "ca.key")
    save_cert(cert, OUT_DIR / "ca.pem")

    return key, cert


def create_cert(common_name, ca_key, ca_cert, prefix):
    key = generate_key()

    subject = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "IN"),
        x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, "Karnataka"),
        x509.NameAttribute(NameOID.LOCALITY_NAME, "Bangalore"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "MyProject"),
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
    ])

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.utcnow())
        .not_valid_after(datetime.utcnow() + timedelta(days=365))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost")]),
            critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )

    save_key(key, OUT_DIR / f"{prefix}.key")
    save_cert(cert, OUT_DIR / f"{prefix}.pem")


def main():
    print("Generating CA...")
    ca_key, ca_cert = create_ca()

    print("Generating server cert...")
    create_cert("localhost", ca_key, ca_cert, "server")

    print("Generating client cert...")
    create_cert("client", ca_key, ca_cert, "client")

    print("\nDone. Files created in ./certs/")
    print(" - ca.pem")
    print(" - server.pem / server.key")
    print(" - client.pem / client.key")


if __name__ == "__main__":
    main()