from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
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
    """Credential references; local plaintext storage is an explicit POSIX option."""

    def __init__(self, namespace: str):
        self.namespace = "aios-client:" + namespace
        path = Path(namespace)
        self.directory = path.parent / (path.name + ".credentials")

    @staticmethod
    def _private(info, directory=False):
        kind = stat.S_ISDIR if directory else stat.S_ISREG
        if not kind(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError(
                "Local credentials must be owned by this user with directory mode 700 and file mode 600"
            )

    def _local_path(self, name: str, create=False) -> Path:
        if os.name != "posix":
            raise ValueError("Private local files require POSIX permissions; use OS keyring or env:NAME")
        if not re.fullmatch(r"[\w .:-]{1,160}", name):
            raise ValueError("Invalid credential name")
        if create:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._private(self.directory.lstat(), directory=True)
        return self.directory / (hashlib.sha256(name.encode()).hexdigest() + ".json")

    def _local_get(self, name: str) -> str:
        try:
            path = self._local_path(name)
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd) as f:
                self._private(os.fstat(f.fileno()))
                try:
                    value = json.load(f)
                except (ValueError, UnicodeError):
                    raise ValueError("Local credential file is invalid; replace the saved key") from None
            if not isinstance(value, str) or not value:
                raise ValueError("Local credential file is empty or invalid; replace the saved key")
            return value
        except FileNotFoundError:
            raise ValueError("Saved local credential is missing; enter and save the key again") from None
        except OSError:
            raise ValueError("Cannot read local credential file; check ownership and permissions") from None

    def _local_set(self, name: str, value: str) -> str:
        try:
            path = self._local_path(name, create=True)
            if path.exists() or path.is_symlink():
                self._private(path.lstat())
            fd, tmp = tempfile.mkstemp(dir=self.directory)
            try:
                with os.fdopen(fd, "w") as f:
                    os.fchmod(f.fileno(), 0o600)
                    json.dump(value, f)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        except OSError:
            raise ValueError("Cannot save local credential file; check ownership and permissions") from None
        return "file:" + name

    def get(self, ref: str) -> str:
        if not ref:
            return ""
        if ref.startswith("file:"):
            return self._local_get(ref[5:])
        if ref.startswith("env:"):
            value = os.environ.get(ref[4:], "")
            if not value:
                raise ValueError(
                    f"Configured credential environment variable {ref[4:]} is not set"
                )
            return value
        if not ref.startswith("keyring:"):
            raise ValueError("Credential reference must use env:, keyring:, or file:. Enter API keys in the password field, then save the profile.")
        import keyring

        try:
            return keyring.get_password(self.namespace, ref[8:]) or ""
        except Exception:
            raise ValueError(
                "Credential storage unavailable; use an environment variable reference"
            ) from None

    def set(self, name: str, value: str, storage="keyring") -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Enter a non-empty API key")
        if storage == "file":
            return self._local_set(name, value)
        if storage != "keyring":
            raise ValueError("Choose OS keyring or private local file storage")
        import keyring

        try:
            keyring.set_password(self.namespace, name, value)
        except Exception:
            raise ValueError(
                "No usable OS credential store. Choose Private local file on Linux, or use env:NAME"
            ) from None
        return "keyring:" + name

    def delete(self, ref: str):
        if ref.startswith("file:"):
            try:
                path = self._local_path(ref[5:])
                self._private(path.lstat())
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                raise ValueError("Cannot remove local credential file; check permissions") from None
            return
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
                if ref and not re.fullmatch(r"(?:env|keyring|file):[\w .:-]{1,160}", ref):
                    raise ValueError("Credential reference must use env:, keyring:, or file:. Enter the actual key in the password field, then save the profile.")
        for p in data["aios"].values():
            aios_url(p)
            if p.get("secret_ref") in {
                "env:AIOS_WORKSPACE_REVIEW_KEY",
                "env:AIOS_WORKSPACE_OPERATOR_KEY",
            }:
                raise ValueError("Configure only the worker credential in the client")
        for p in data["endpoints"].values():
            base_url(p["base_url"])
            if not 1 <= int(p.get("concurrency", 1)) <= 16:
                raise ValueError("Endpoint concurrency must be 1–16")
            if not 1 <= float(p.get("timeout", 120)) <= 1800:
                raise ValueError("Endpoint timeout must be 1–1800 seconds")
        for p in data["agents"].values():
            from .tools.network import canonical_origin

            origins = p.get("http_origins", [])
            if type(origins) is not list or len(origins) > 20 or any(type(x) is not str for x in origins):
                raise ValueError("http_origins must be a list of at most 20 HTTP(S) origins")
            for origin in origins:
                canonical_origin(origin, origin_only=True)
            if p.get("protocol_version", "auto") not in ("auto", 2, 3):
                raise ValueError("protocol_version must be auto, 2, or 3")
            routing = p.get('routing_mode', 'manual')
            if routing not in ('manual', 'automatic'):
                raise ValueError('routing_mode must be manual or automatic')
            if routing == 'automatic' and (p.get('link_id') or p.get('origin_instance_id') or p.get('share_memory')):
                raise ValueError('Automatic routing does not use manual links or memory sharing')
            if routing == 'automatic' and p.get('protocol_version') == 2:
                raise ValueError('Automatic routing requires protocol v3')
            for field in ("origin_instance_id", "link_id"):
                if p.get(field):
                    UUID(p[field])
            if bool(p.get("origin_instance_id")) != bool(p.get("link_id")):
                raise ValueError(
                    "Save both the operator-created link_id and origin_instance_id"
                )
            if p.get("link_id") and p.get("protocol_version") == 2:
                raise ValueError("Environment links require protocol v3")
            if type(p.get("share_memory", False)) is not bool:
                raise ValueError("share_memory must be a boolean")
            if p.get("share_memory") and not p.get("link_id"):
                raise ValueError(
                    "Memory sharing requires an operator-created environment link"
                )
            for field, group in (("aios", "aios"), ("endpoint", "endpoints")):
                if p.get(field) not in data[group]:
                    raise ValueError(f"Unknown {field} profile")
            if routing == 'manual':
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
