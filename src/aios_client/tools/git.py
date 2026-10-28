"""Read-only Git inspection through the same owned process backend."""

from __future__ import annotations

from typing import Literal


class GitTools:
    def __init__(self, workspace):
        self.workspace = workspace

    def run(self, args):
        w = self.workspace
        task = w.run_command(argv=["git", *args])
        record = w.processes[task["process_id"]]
        record["done"].wait(w.command_timeout + 3)
        return w.read_process(task["process_id"])

    def git_status(self):
        return self.run(["status", "--short"]) | {
            "head": self.run(["rev-parse", "HEAD"])["output"].strip()
        }

    def git_diff(
        self,
        path: str = ".",
        staged: bool = False,
        mode: Literal["patch", "stat"] = "patch",
    ):
        self.workspace.path(path)
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
