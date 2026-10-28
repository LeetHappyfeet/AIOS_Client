from __future__ import annotations

import argparse
import json
import signal
from pathlib import Path

from .config import ConfigStore
from .journal import Journal
from .runner import Runner, agent_dir


def main():
    parser = argparse.ArgumentParser(description="AIOS workspace client")
    parser.add_argument(
        "--config", type=Path, default=Path.home() / ".config/aios-client/client.json"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("setup", help="Open local configuration and run controls")
    setup.add_argument("--port", type=int, default=8765)
    for name in ("run", "serve", "check", "status", "stop"):
        p = sub.add_parser(name)
        p.add_argument("--agent", required=True)
        if name == "run":
            p.add_argument("--task-file", type=Path, required=True)
            p.add_argument(
                "--request-id",
                help="Reuse an assignment request ID after an uncertain submission",
            )
    args = parser.parse_args()
    store = ConfigStore(args.config)
    try:
        if args.command == "setup":
            import uvicorn

            from .server import create_app

            print(f"AIOS Client setup: http://127.0.0.1:{args.port}")
            uvicorn.run(
                create_app(store, args.port),
                host="127.0.0.1",
                port=args.port,
                access_log=False,
            )
            return
        if args.agent not in store.load()["agents"]:
            raise ValueError("Unknown agent profile; run aios-client setup first")
        if args.command == "status":
            journal = Journal(agent_dir(store, args.agent) / "journal.sqlite3")
            print(json.dumps(journal.status(), indent=2))
            journal.db.close()
            return
        if args.command == "stop":
            p = agent_dir(store, args.agent) / "stop"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.touch()
            print(
                "Stop requested; active commands will be terminated at the next control check."
            )
            return
        runner = Runner(store, args.agent)
        try:
            if args.command == "check":
                binding = runner.binding()
                print(
                    json.dumps(
                        {
                            "binding": binding,
                            "model": runner.model.test_tools(runner.model_id),
                        },
                        default=str,
                        indent=2,
                    )
                )
                return
            signal.signal(signal.SIGINT, lambda *_: runner.stop())
            signal.signal(signal.SIGTERM, lambda *_: runner.stop())
            result = runner.work(
                args.task_file.read_text() if args.command == "run" else None,
                request_id=getattr(args, "request_id", None),
                serve=args.command == "serve",
            )
            print(json.dumps(result, default=str, indent=2))
        finally:
            runner.close()
    except (ValueError, KeyError, RuntimeError, OSError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
