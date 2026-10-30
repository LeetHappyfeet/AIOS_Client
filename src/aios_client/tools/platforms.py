"""Portable process backend seam; future PTY/ConPTY adapters belong here."""

from __future__ import annotations

import os
import platform
import shutil
import signal
import subprocess
import sys


class ProcessBackend:
    def capabilities(self):
        candidates = (
            ("cmd", "powershell", "pwsh") if os.name == "nt" else ("sh", "bash", "pwsh")
        )
        return {
            "os": platform.system(),
            "platform": sys.platform,
            "architecture": platform.machine(),
            "python": sys.executable,
            "default_shell": "cmd" if os.name == "nt" else "sh",
            "shells": ["default"] + [s for s in candidates if shutil.which(s)],
            "argv": True,
            "stdin_pipe": True,
            "pty": False,
            "scope": "workspace",
            "privilege_escalation": False,
        }

    def launch(self, command, argv, shell, cwd, env, stdin):
        if (command is None) == (argv is None):
            raise ValueError("Supply exactly one of command or argv")
        use_shell = False
        if argv is not None:
            if (
                shell != "default"
                or not argv
                or any(not isinstance(x, str) or "\0" in x for x in argv)
            ):
                raise ValueError(
                    "argv requires nonempty arguments and the default shell selector"
                )
            args = argv
        elif shell == "default":
            args, use_shell = command, True
        else:
            if shell not in self.capabilities()["shells"]:
                raise ValueError("Requested shell is unavailable on this host")
            executable = shutil.which(shell)
            if shell in ("powershell", "pwsh"):
                args = [
                    executable,
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    command,
                ]
            elif shell == "cmd":
                args = [executable, "/d", "/s", "/c", command]
            else:
                args = [executable, "-c", command]
        return subprocess.Popen(
            args,
            shell=use_shell,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )

    def stop(self, record):
        p = record["p"]
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(p.pid)],
                capture_output=True,
                check=False,
            )
        else:
            try:
                os.killpg(p.pid, signal.SIGTERM)
                if not record["done"].wait(2):
                    os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=3)
