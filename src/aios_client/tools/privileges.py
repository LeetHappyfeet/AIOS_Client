"""Explicit operator opt-in and fixed, noninteractive privilege observations."""
from __future__ import annotations

import os
import shutil
import subprocess


class Privileges:
    def __init__(self, enabled=False):
        self.enabled = enabled
        self.probe = {"status": "not_tested"}

    def status(self):
        uid = os.geteuid() if hasattr(os, "geteuid") else None
        return {"administration_enabled": self.enabled, "effective_uid": uid,
                "is_root": uid == 0 if uid is not None else None,
                "sudo_path": shutil.which("sudo"), "sudo_probe": dict(self.probe),
                "limitations": ["Administration is a tool preference, not a shell sandbox. OS permissions remain authoritative.",
                                "A successful sudo probe verifies id at that time only; other commands may have different policy and cached authentication can expire."]}

    def inspect(self):
        if not self.enabled:
            self.probe = {"status": "disabled", "reason": "Enable administration in the saved agent profile to test sudo."}
        elif os.name != "posix":
            self.probe = {"status": "unsupported"}
        elif os.geteuid() == 0:
            self.probe = {"status": "already_root"}
        elif not shutil.which("sudo") or not shutil.which("id"):
            self.probe = {"status": "unavailable"}
        else:
            # Fixed argv only; never execute model-provided input or request a password.
            command = [shutil.which("sudo"), "-n", "--", shutil.which("id"), "-u"]
            try:
                result = subprocess.run(command, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        timeout=5, check=False)
                self.probe = {"status": "available" if result.returncode == 0 and result.stdout.strip() == b"0" else "denied",
                              "exit_code": result.returncode}
            except subprocess.TimeoutExpired:
                self.probe = {"status": "timeout"}
            except OSError:
                self.probe = {"status": "unavailable"}
        return self.status()

    def elevated_argv(self, command, argv, shell):
        if not self.enabled:
            raise ValueError("Administration is disabled; enable it in the saved agent profile")
        if os.name != "posix":
            raise ValueError("Command elevation currently supports POSIX sudo only")
        if (command is None) == (argv is None):
            raise ValueError("Supply exactly one of command or argv")
        if argv is not None:
            if not argv or any(not isinstance(arg, str) or "\0" in arg for arg in argv):
                raise ValueError("argv requires nonempty arguments without NUL characters")
            if shell != "default":
                raise ValueError("argv requires the default shell selector")
            args = argv
        else:
            if shell not in ("default", "sh", "bash"):
                raise ValueError("Elevated shell commands support sh or bash; use argv for other programs")
            executable = shutil.which("sh" if shell == "default" else shell)
            if not executable:
                raise ValueError("Requested shell is unavailable")
            args = [executable, "-c", command]
        if os.geteuid() == 0:
            return args
        sudo = shutil.which("sudo")
        if not sudo:
            raise ValueError("sudo is unavailable on this host")
        return [sudo, "-n", "--", *args]
