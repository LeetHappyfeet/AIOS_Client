"""Read-only Git inspection through the same owned process backend."""

from __future__ import annotations

import re
from typing import Literal


class GitTools:
    def __init__(self, workspace):
        self.workspace = workspace
        self._availability = None

    def availability(self):
        """Cache absence for this run; Git is optional in a plain workspace."""
        if self._availability is None:
            try:
                probe = self.run(["rev-parse", "--is-inside-work-tree"])
            except FileNotFoundError:
                self._availability = {
                    "is_repository": None,
                    "reason": "git_unavailable",
                }
            else:
                if probe["exit_code"] == 0 and probe["output"].strip() == "true":
                    self._availability = {"is_repository": True}
                elif "not a git repository" in probe["output"].lower():
                    self._availability = {
                        "is_repository": False,
                        "reason": "not_a_git_repository",
                    }
                else:
                    # A permission, timeout or other Git error is not evidence of absence.
                    return {
                        "is_repository": None,
                        "status": "failed",
                        "head": None,
                        "output": probe["output"],
                        "exit_code": probe["exit_code"],
                    }
        if self._availability["is_repository"] is not True:
            return {
                **self._availability,
                "status": "skipped",
                "head": None,
                "output": "",
            }
        return self._availability

    def run(self, args):
        w = self.workspace
        task = w.run_command(argv=["git", *args])
        record = w.processes[task["process_id"]]
        record["done"].wait(w.command_timeout + 3)
        return w.read_process(task["process_id"])

    def git_status(self):
        available = self.availability()
        if available["is_repository"] is not True:
            return available
        status = self.run(["status", "--short"])
        if status["exit_code"] != 0:
            return status | {"is_repository": True, "status": "failed", "head": None}
        head = self.run(["rev-parse", "--verify", "--quiet", "HEAD"])
        value = head["output"].strip()
        valid = head["exit_code"] == 0 and re.fullmatch(
            r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value
        )
        unborn = head["exit_code"] == 1 and not value
        return status | {
            "is_repository": True,
            "status": "available" if valid or unborn else "failed",
            "head": value if valid else None,
            "head_status": "available" if valid else "unborn" if unborn else "failed",
        }

    def git_diff(
        self,
        path: str = ".",
        staged: bool = False,
        mode: Literal["patch", "stat"] = "patch",
    ):
        self.workspace.path(path)
        available = self.availability()
        if available["is_repository"] is not True:
            return available
        args = ["diff", "--no-ext-diff", "--no-textconv"] + (
            ["--cached"] if staged else []
        )
        stat = self.run([*args, "--stat", "--", path])
        if mode == "stat":
            return stat
        return stat | {"diff": self.run([*args, "--", path])["output"]}

    def git_show(self, revision: str = "HEAD", path: str = "."):
        if (
            not revision
            or revision.startswith("-")
            or any(
                c
                not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./~^-"
                for c in revision
            )
        ):
            raise ValueError("Use a simple commit, branch or revision expression")
        self.workspace.path(path)
        available = self.availability()
        if available["is_repository"] is not True:
            return available
        return self.run(
            [
                "show",
                "--no-ext-diff",
                "--no-textconv",
                "--format=fuller",
                revision,
                "--",
                path,
            ]
        )


def register(registry, git):
    for name, description in {
        "git_status": "Inspect Git status and HEAD without staging or committing",
        "git_diff": "Inspect tracked changes, optionally staged or limited to a path",
        "git_show": "Inspect a commit diff and metadata for a bounded path",
    }.items():
        registry.add(name, getattr(git, name), description, "workspace.git")
