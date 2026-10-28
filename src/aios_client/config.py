from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID


def base_url(value: str) -> str:
    value = value.strip().rstrip("/")
    p = urlsplit(value)
    if (
        p.scheme not in ("http", "https")
        or not p.hostname
        or p.username
        or p.password
        or p.query
        or p.fragment
    ):
        raise ValueError(
            "Use an HTTP(S) base URL without credentials, query, or fragment"
        )
    try:
        _ = p.port
    except ValueError as exc:
        raise ValueError("Invalid port") from exc
    return value


def aios_url(profile: dict) -> str:
    host = profile["host"].strip()
    if any(c in host for c in "/@?#") or not host:
        raise ValueError("Host must be an IP address or hostname")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    scheme = profile.get("scheme", "http")
    port = int(profile.get("port", 8000))
    if not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535")
    path = profile.get("base_path", "").strip().strip("/")
    return base_url(f"{scheme}://{host}:{port}" + (f"/{path}" if path else ""))


class Secrets:
    """Never persist entered keys in profile JSON. No plaintext fallback."""

    def __init__(self, namespace: str):
        self.namespace = "aios-client:" + namespace

    def get(self, ref: str) -> str:
        if not ref:
            return ""
        if ref.startswith("env:"):
            value = os.environ.get(ref[4:], "")
            if not value:
                raise ValueError(
                    f"Configured credential environment variable {ref[4:]} is not set"
                )
            return value
        if not ref.startswith("keyring:"):
            raise ValueError("Secret reference must use env: or keyring:")
        import keyring

        try:
            return keyring.get_password(self.namespace, ref[8:]) or ""
        except Exception:
            raise ValueError(
                "Credential storage unavailable; use an environment variable reference"
            ) from None

    def set(self, name: str, value: str) -> str:
        import keyring

        try:
            keyring.set_password(self.namespace, name, value)
        except Exception:
            raise ValueError(
                "No usable OS credential store. Set an environment variable and use env:NAME"
            ) from None
        return "keyring:" + name

    def delete(self, ref: str):
        if ref.startswith("keyring:"):
            import keyring

            try:
                keyring.delete_password(self.namespace, ref[8:])
            except keyring.errors.PasswordDeleteError:
                pass


class ConfigStore:
    def __init__(self, path: Path):
        self.path = Path(path).expanduser().resolve()
        self.lock = threading.RLock()
        self.secrets = Secrets(str(self.path))

    def load(self) -> dict:
        with self.lock:
            if not self.path.exists():
                return {"version": 1, "aios": {}, "endpoints": {}, "agents": {}}
            data = json.loads(self.path.read_text())
            self.validate(data)
            return data

    @staticmethod
    def validate(data: dict):
        if data.get("version") != 1:
            raise ValueError("Unsupported configuration version")
        for kind in ("aios", "endpoints", "agents"):
            if not isinstance(data.get(kind), dict):
                raise ValueError(f"{kind} must be an object")
            for name, profile in data[kind].items():
                if not re.fullmatch(r"[\w .-]{1,80}", name) or not isinstance(
                    profile, dict
                ):
                    raise ValueError("Invalid profile name or body")
                if any(k in profile for k in ("api_key", "password", "token")):
                    raise ValueError("Use secret_ref, never store a key in profiles")
                ref = profile.get("secret_ref", "")
                if ref and not re.fullmatch(r"(?:env|keyring):[\w .:-]{1,160}", ref):
                    raise ValueError("Invalid secret reference")
        for p in data["aios"].values():
            aios_url(p)
        for p in data["endpoints"].values():
            base_url(p["base_url"])
            if not 1 <= int(p.get("concurrency", 1)) <= 16:
                raise ValueError("Endpoint concurrency must be 1–16")
            if not 1 <= float(p.get("timeout", 120)) <= 1800:
                raise ValueError("Endpoint timeout must be 1–1800 seconds")
        for p in data["agents"].values():
            for field, group in (("aios", "aios"), ("endpoint", "endpoints")):
                if p.get(field) not in data[group]:
                    raise ValueError(f"Unknown {field} profile")
            for field in ("instance_id", "session_id"):
                UUID(p[field])
            for field in (
                "username",
                "character_id",
                "user_name",
                "scope_key",
                "workspace",
            ):
                if not isinstance(p.get(field), str) or not p[field].strip():
                    raise ValueError(f"Agent requires {field}")

    def save(self, data: dict):
        data = copy.deepcopy(data)
        self.validate(data)
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent)
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(data, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)

    @property
    def data_dir(self):
        return self.path.parent / "client-data"
