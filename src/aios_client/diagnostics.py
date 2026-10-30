"""Read-only diagnostics unless an explicitly requested model probe is enabled."""
from __future__ import annotations

import getpass
import os
import subprocess
import sys
from pathlib import Path

from .config import aios_url
from .http import API, APIError, APIUnavailable, Model
from .protocol import negotiate


def installation_info() -> dict:
    root = Path(__file__).resolve().parents[2]
    info = {"os_user": getpass.getuser(), "python": sys.version.split()[0],
            "executable": sys.executable, "installation": str(root)}
    try:
        for key, args in (("branch", ["branch", "--show-current"]), ("commit", ["rev-parse", "--short", "HEAD"])):
            info[key] = subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.DEVNULL, timeout=3, text=True).strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return info


def connection_hint(exc: Exception) -> str:
    if isinstance(exc, APIError):
        if exc.status == 401:
            return "Credential rejected. Save the correct key for this connection; AIOS's worker key must match its server configuration."
        if exc.status == 403:
            return "Access forbidden. Check this provider's key, account/model permissions, or refresh setup if its request token expired."
        if exc.status == 404:
            return "Route not found. Check the base URL/path and whether the server supports the requested feature."
        if exc.status == 503:
            return "Service unavailable. For AIOS workspace routes, configure the server's worker key and check service readiness."
    if isinstance(exc, APIUnavailable):
        return "Connection or JSON response failed. Check the host, port, TLS, firewall, and API base URL."
    return str(exc)


def doctor(store, agent: str, probe_model=False) -> dict:
    checks = []
    resolved = []

    def check(name, fn):
        try:
            value = fn()
            checks.append({"check": name, "status": "passed", "detail": value})
            return value
        except Exception as exc:
            # Never include arbitrary provider response bodies or configured key values.
            detail = connection_hint(exc)
            for secret in resolved:
                detail = detail.replace(secret, "[REDACTED]")
            checks.append({"check": name, "status": "failed", "detail": detail})
            return None

    config = check("configuration", lambda: store.load())
    if config is None:
        return {"ok": False, "checks": checks}
    # Configuration is references only, but avoid dumping all profiles into diagnostics.
    checks[-1]["detail"] = str(store.path)
    profile = config["agents"].get(agent)
    if profile is None:
        checks.append({"check": "agent", "status": "failed", "detail": "Unknown profile; save an agent in setup first"})
        return {"ok": False, "checks": checks}

    def workspace():
        path = Path(profile["workspace"]).expanduser()
        if not path.is_dir() or not os.access(path, os.W_OK | os.X_OK):
            raise ValueError("Workspace must exist and be writable by the client account")
        return str(path)
    check("workspace", workspace)
    a = config["aios"][profile["aios"]]
    e = config["endpoints"][profile["endpoint"]]

    def credential(p):
        key = store.secrets.get(p.get("secret_ref", ""))
        if key:
            resolved.append(key)
        return key
    akey = check("AIOS credential", lambda: credential(a))
    checks[-1]["detail"] = "Available" if akey else checks[-1]["detail"]
    if akey == "":
        checks[-1].update(status="failed", detail="Save the AIOS worker key in setup")
    mkey = check("model credential", lambda: credential(e))
    if mkey is not None:
        checks[-1]["detail"] = "Available" if mkey else "No key configured (optional for local providers)"
    if akey:
        api = API(aios_url(a), akey, timeout=15)
        try:
            check("AIOS protocol", lambda: negotiate(api.request("GET", "workspace-client/protocol")))
            def binding():
                data = api.request("GET", "workspace-client/instances", params={"character_id": profile["character_id"]})
                fields = ("character_id", "instance_id", "session_id", "user_name", "scope_key")
                if not any(all(x.get(k) == profile[k] for k in fields) for x in data.get("instances", [])):
                    raise ValueError("Selected character/session coordinates were not found; rediscover sessions in setup")
                return "Existing session coordinates found; use Validate binding for authoritative binding validation"
            check("session discovery", binding)
        finally:
            api.close()
    if mkey is not None:
        model = Model(e["base_url"], mkey, timeout=15)
        try:
            def listing():
                models = model.models()
                selected = profile.get("model") or e.get("model", "")
                if selected not in models:
                    raise ValueError("Selected model was not returned by /models; select an available ID or explicitly probe a provider with no listing support")
                return "Selected model listed"
            check("model listing", listing)
            if probe_model:
                check("model tools (generation request)", lambda: model.test_tools(profile.get("model") or e.get("model", "")))
            else:
                checks.append({"check": "model tools", "status": "skipped", "detail": "Use --probe-model to send a generation request; provider charges may apply"})
        finally:
            model.close()
    # Credential check results must never carry the actual secret.
    return {"ok": all(c["status"] != "failed" for c in checks), "installation": installation_info(), "checks": checks}
