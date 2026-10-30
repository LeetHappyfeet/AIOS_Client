"""Install user services; never invoke sudo or start work implicitly."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def systemd_quote(value) -> str:
    value = str(value)
    if any(c in value for c in '\r\n\x00'):
        raise ValueError("Service arguments cannot contain newlines or NUL")
    # systemd has its own quoting plus % specifiers and $ environment expansion.
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'


def install_service(store, kind="ui", agent=None, enable=False) -> Path:
    if not sys.platform.startswith("linux"):
        raise ValueError("Service installation currently supports Linux systemd user services")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise ValueError("Install the user service as the ordinary account that owns the client")
    if kind == "worker" and (not agent or agent not in store.load()["agents"]):
        raise ValueError("Save and select an existing agent before installing its worker service")
    if kind not in ("ui", "worker"):
        raise ValueError("Service kind must be ui or worker")
    # UI and worker are separate processes. Do not run the same agent in both.
    name = "aios-client-ui" if kind == "ui" else "aios-client-worker"
    args = [sys.executable, "-m", "aios_client.cli", "--config", str(store.path)]
    args += ["setup"] if kind == "ui" else ["serve", "--agent", agent]
    directory = Path.home() / ".config/systemd/user"
    directory.mkdir(parents=True, exist_ok=True)
    unit = directory / (name + ".service")
    content = (
        "[Unit]\nDescription=AIOS Client " + kind + "\nAfter=network-online.target\n\n"
        "[Service]\nType=simple\nWorkingDirectory=%h\n"
        "ExecStart=" + " ".join(systemd_quote(a) for a in args) + "\n"
        "Restart=on-failure\nRestartSec=5\nUMask=0077\nNoNewPrivileges=true\n\n"
        "[Install]\nWantedBy=default.target\n"
    )
    if unit.exists() and unit.read_text() != content:
        raise ValueError(f"Service already exists with different settings: {unit}. Remove or edit it explicitly.")
    unit.write_text(content)
    if enable:
        try:
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
            subprocess.run(["systemctl", "--user", "enable", "--now", name], check=True)
        except (OSError, subprocess.CalledProcessError):
            raise ValueError(f"Service file saved at {unit}, but the user service manager is unavailable. Log in directly as this user; see docs/installation.md.") from None
    return unit
