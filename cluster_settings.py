"""Private node provisioning and TLS trust for the ShareDownload cluster."""
from __future__ import annotations

import datetime as dt
import ipaddress
import json
import re
import secrets
import ssl
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

NODE_IDS = ("S1", "S2", "S3")
_FOLLOWERS = NODE_IDS[1:]
_DNS_LABEL = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")


class ClusterError(Exception):
    """An actionable provisioning, synchronization or publication error."""


def _host(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 253 or "%" in value:
        raise ClusterError("Địa chỉ máy chủ cụm không hợp lệ.")
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        try:
            name = value.encode("idna").decode("ascii")
        except UnicodeError as error:
            raise ClusterError("Tên máy chủ cụm không hợp lệ.") from error
        if len(name) > 253 or not all(_DNS_LABEL.fullmatch(label) for label in name.split(".")):
            raise ClusterError("Tên máy chủ cụm không hợp lệ.")
        return name.lower()


def _port(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 65535:
        raise ClusterError("Cổng TLS của cụm phải nằm trong khoảng 1–65535.")
    return value


@dataclass(frozen=True, slots=True)
class ClusterSettings:
    """One node's private configuration. Do not include it in download-client JSON."""

    node_id: str
    host: str
    port: int
    certificate_pem: str = field(repr=False)
    bind_host: str = "0.0.0.0"
    token: str | None = field(default=None, repr=False)
    private_key_pem: str | None = field(default=None, repr=False)
    follower_tokens: tuple[str, str] | None = field(default=None, repr=False)

    @property
    def is_coordinator(self) -> bool:
        return self.node_id == "S1"

    @classmethod
    def create_coordinator(cls, host: str, port: int, *, bind_host: str = "0.0.0.0") -> ClusterSettings:
        host, port, bind_host = _host(host), _port(port), _host(bind_host)
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            san = x509.DNSName(host)
        else:
            if address.is_unspecified:
                raise ClusterError("Địa chỉ để các nút kết nối không được là địa chỉ lắng nghe mọi giao diện.")
            san = x509.IPAddress(address)
        key = ec.generate_private_key(ec.SECP256R1())
        # The SAN is the configured endpoint; the subject is descriptive only.
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ShareDownload S1")])
        now = dt.datetime.now(dt.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName([san]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(key, hashes.SHA256())
        )
        return cls(
            node_id="S1",
            host=host,
            port=port,
            bind_host=bind_host,
            certificate_pem=cert.public_bytes(serialization.Encoding.PEM).decode("ascii"),
            private_key_pem=key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ).decode("ascii"),
            follower_tokens=(secrets.token_urlsafe(32), secrets.token_urlsafe(32)),
        )

    def export_enrollment(self, node_id: str) -> str:
        """Return private JSON for exactly one follower; transfer it over a trusted channel."""
        if not self.is_coordinator or self.follower_tokens is None or node_id not in _FOLLOWERS:
            raise ClusterError("Chỉ S1 có thể xuất hồ sơ riêng cho S2 hoặc S3.")
        return ClusterSettings(
            node_id, self.host, self.port, self.certificate_pem,
            token=self.follower_tokens[_FOLLOWERS.index(node_id)],
        ).to_json()

    def to_json(self) -> str:
        data = {
            "version": 1, "node_id": self.node_id, "host": self.host,
            "port": self.port, "certificate_pem": self.certificate_pem,
        }
        if self.is_coordinator:
            data.update(bind_host=self.bind_host, private_key_pem=self.private_key_pem)
            if self.follower_tokens is not None:
                data["follower_tokens"] = dict(zip(_FOLLOWERS, self.follower_tokens))
        else:
            data["token"] = self.token
        return json.dumps(data, indent=2)

    @classmethod
    def from_json(cls, text: str) -> ClusterSettings:
        """Load coordinator settings or a follower's private enrollment."""
        try:
            if not isinstance(text, str) or len(text) > 32_768:
                raise ValueError
            data = json.loads(text)
            if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
                raise ValueError
            node = data["node_id"]
            if node not in NODE_IDS:
                raise ValueError
            host, port = _host(data["host"]), _port(data["port"])
            cert = data["certificate_pem"]
            if not isinstance(cert, str) or not cert.startswith("-----BEGIN CERTIFICATE-----"):
                raise ValueError
            fields = {"version", "node_id", "host", "port", "certificate_pem"}
            if node == "S1":
                if set(data) != fields | {"bind_host", "private_key_pem", "follower_tokens"}:
                    raise ValueError
                key, tokens = data["private_key_pem"], data["follower_tokens"]
                if not isinstance(key, str) or not key.startswith("-----BEGIN PRIVATE KEY-----"):
                    raise ValueError
                if not isinstance(tokens, dict) or set(tokens) != set(_FOLLOWERS):
                    raise ValueError
                s2, s3 = (tokens[node] for node in _FOLLOWERS)
                if not _valid_token(s2) or not _valid_token(s3) or s2 == s3:
                    raise ValueError
                return cls(node, host, port, cert, _host(data["bind_host"]),
                           private_key_pem=key, follower_tokens=(s2, s3))
            if set(data) != fields | {"token"} or not _valid_token(data["token"]):
                raise ValueError
            return cls(node, host, port, cert, token=data["token"])
        except (KeyError, RecursionError, ValueError) as error:
            raise ClusterError("Hồ sơ cấu hình riêng của cụm không hợp lệ.") from error

    def _server_context(self, state_dir: Path) -> ssl.SSLContext:
        if self.private_key_pem is None or self.follower_tokens is None:
            raise ClusterError("Cấu hình S1 thiếu khóa TLS hoặc thông tin xác thực nút.")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        # Python's TLS API requires filesystem paths. Keep the private key in a
        # private temporary directory and unlink it as soon as TLS has loaded it.
        with TemporaryDirectory(dir=state_dir) as directory:
            cert_path, key_path = Path(directory) / "certificate.pem", Path(directory) / "key.pem"
            cert_path.write_text(self.certificate_pem, encoding="ascii")
            key_path.write_text(self.private_key_pem, encoding="ascii")
            context.load_cert_chain(cert_path, key_path)
        return context

    def _client_context(self) -> ssl.SSLContext:
        if not _valid_token(self.token):
            raise ClusterError("Cấu hình nút thiếu thông tin xác thực riêng.")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.hostname_checks_common_name = False
        # Do not load public CAs: only the certificate from private enrollment is trusted.
        context.load_verify_locations(cadata=self.certificate_pem)
        return context


def _valid_token(value: object) -> bool:
    return isinstance(value, str) and _TOKEN.fullmatch(value) is not None
