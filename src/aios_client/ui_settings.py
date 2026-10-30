"""Persistent UI listener settings and a password hash, separate from profile exports."""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from .config import base_url


def password_hash(password: str) -> dict:
    if len(password) < 12:
        raise ValueError("Use a UI password of at least 12 characters")
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return {"salt": salt.hex(), "digest": digest.hex()}


def verify_password(password: str, saved: dict) -> bool:
    try:
        salt = bytes.fromhex(saved["salt"])
        expected = bytes.fromhex(saved["digest"])
        if len(salt) != 16 or len(expected) != 32 or len(password) > 1024:
            return False
        actual = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
        return hmac.compare_digest(actual, expected)
    except (KeyError, TypeError, ValueError):
        return False


def check_basic(header: str, saved: dict) -> bool:
    try:
        scheme, value = header.split(" ", 1)
        if scheme.lower() != "basic" or len(value) > 4096:
            return False
        username, password = base64.b64decode(value, validate=True).decode().split(":", 1)
        return username == "admin" and verify_password(password, saved)
    except (ValueError, UnicodeError):
        return False


def remote_ui(host: str, public_url: str = "") -> bool:
    if not ipaddress.ip_address(host).is_loopback:
        return True
    hostname = urlsplit(public_url).hostname
    if hostname in (None, "localhost"):
        return False
    try:
        return not ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return True


def validate_ui(data: dict) -> dict:
    host = data.get("host", "127.0.0.1")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError("UI bind host must be an IP address, such as 127.0.0.1 or 0.0.0.0") from None
    port = int(data.get("port", 8765))
    if not 1 <= port <= 65535:
        raise ValueError("UI port must be 1–65535")
    default_host = f"[{host}]" if address.version == 6 else host
    public_url = base_url(data.get("public_url") or f"http://{default_host}:{port}")
    url = urlsplit(public_url)
    if url.path not in ("", "/") or url.hostname in ("0.0.0.0", "::"):
        raise ValueError("UI browser URL must be an origin with a reachable hostname/IP, without a path")
    saved = data.get("password_hash")
    if remote_ui(host, public_url) and not saved:
        raise ValueError("Remote UI requires a password; run aios-client configure-ui first")
    cert, key = data.get("tls_certfile"), data.get("tls_keyfile")
    if bool(cert) != bool(key):
        raise ValueError("Supply both TLS certificate and private key")
    if cert and url.scheme != "https":
        raise ValueError("Use an https:// browser URL with TLS")
    return {"host": host, "port": port, "public_url": public_url.rstrip("/"),
            "password_hash": saved, "tls_certfile": cert, "tls_keyfile": key,
            "default_workspace": str(Path(data.get("default_workspace") or Path.home() / "workspace/test-project").expanduser().resolve())}


class UISettings:
    def __init__(self, config_path: Path):
        self.path = Path(str(config_path) + ".ui.json")

    def load(self) -> dict:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def save(self, data: dict):
        data = validate_ui(data)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(data, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
