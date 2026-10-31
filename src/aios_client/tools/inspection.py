"""Fixed host observations; never launch model-controlled code or write probes."""
from __future__ import annotations

import getpass
import os
import platform
import shutil
import sys
from pathlib import Path


def inspect_environment(root):
    limitations = [
        "Writable flags use os.access only; no write test was performed.",
        "sudo availability reports executable presence only; sudo privileges/passwordless access were not tested.",
        "Tool paths establish presence, not versions or successful execution.",
    ]
    result = {"cwd": str(root), "os": platform.system(),
              "os_release": platform.release(), "kernel": platform.version(),
              "runtime": {"executable": sys.executable, "version": platform.python_version()}}
    if hasattr(platform, "freedesktop_os_release") and sys.platform.startswith("linux"):
        try:
            release = platform.freedesktop_os_release()
            result["distribution"] = {k: release[k] for k in ("ID", "VERSION_ID", "PRETTY_NAME") if k in release}
        except OSError:
            limitations.append("Linux distribution metadata could not be read.")
    try:
        result["user"] = getpass.getuser()
    except (OSError, KeyError):
        result["user"] = None
        limitations.append("User name could not be determined.")
    result["uid"] = os.getuid() if hasattr(os, "getuid") else None
    result["effective_uid"] = os.geteuid() if hasattr(os, "geteuid") else None
    result["gid"] = os.getgid() if hasattr(os, "getgid") else None
    if result["effective_uid"] is not None:
        try:
            import pwd
            result["user"] = pwd.getpwuid(result["effective_uid"]).pw_name
        except (ImportError, KeyError, OSError):
            limitations.append("Effective user name unavailable; user name may reflect environment variables.")
    result["is_root"] = result["effective_uid"] == 0 if result["effective_uid"] is not None else None
    tools = ("bash", "sh", "pwsh", "cmd", "sudo", "apt", "apt-get", "dnf", "yum", "apk", "pacman",
             "python3", "python", "node", "npm", "ruby", "go", "java", "gcc", "git", "jq", "curl")
    result["tools"] = {name: shutil.which(name) for name in tools}
    result["sudo_privileges"] = "not tested"
    candidates = [Path(root)]
    try:
        candidates.append(Path.home())
    except RuntimeError:
        limitations.append("Home directory could not be determined.")
    # Do not call tempfile.gettempdir(): it creates a test file to choose a path.
    candidates.extend(Path(p) for p in (os.environ.get("TMPDIR"), os.environ.get("TEMP"), "/tmp") if p)
    directories = []
    for path in dict.fromkeys(candidates):
        entry = {"path": str(path)}
        try:
            entry.update(exists=path.is_dir(), writable=os.access(path, os.W_OK | os.X_OK))
            if entry["exists"]:
                entry["disk_bytes"] = dict(zip(("total", "used", "free"), shutil.disk_usage(path)))
        except OSError as exc:
            entry["error"] = type(exc).__name__
        directories.append(entry)
    result["directories"] = directories
    result["limitations"] = limitations
    return result
