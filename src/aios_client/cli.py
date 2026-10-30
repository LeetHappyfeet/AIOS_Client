from __future__ import annotations

import argparse
import getpass
import ipaddress
import json
import signal
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit

from .config import ConfigStore
from .journal import Journal
from .runner import Runner, agent_dir
from .ui_settings import UISettings, password_hash, remote_ui, validate_ui


def main():
    parser = argparse.ArgumentParser(description="AIOS workspace client")
    parser.add_argument(
        "--config", type=Path, default=Path.home() / ".config/aios-client/client.json"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser(
        "setup", help="Start the saved local or authenticated remote UI"
    )
    for command in (
        setup,
        sub.add_parser(
            "configure-ui", help="Save the UI address and administrator password"
        ),
    ):
        command.add_argument("--port", type=int)
        command.add_argument(
            "--host", help="Bind IP; remote listeners require a saved password"
        )
        command.add_argument(
            "--public-url", help="Exact browser origin, e.g. http://192.168.1.161:8765"
        )
    ui = sub.choices["configure-ui"]
    ui.add_argument("--reset-password", action="store_true")
    ui.add_argument("--tls-certfile", type=Path)
    ui.add_argument("--tls-keyfile", type=Path)
    ui.add_argument(
        "--workspace", type=Path, help="Default project directory displayed in setup"
    )
    doctor_parser = sub.add_parser(
        "doctor",
        help="Check configuration, credentials, connections and session discovery",
    )
    doctor_parser.add_argument("--agent", required=True)
    doctor_parser.add_argument(
        "--probe-model",
        action="store_true",
        help="Send a generation/tool probe; provider charges may apply",
    )
    service = sub.add_parser(
        "service", help="Install an optional systemd user UI or worker service"
    )
    service.add_argument("action", choices=["install"])
    service.add_argument("--kind", choices=["ui", "worker"], default="ui")
    service.add_argument("--agent")
    service.add_argument(
        "--enable", action="store_true", help="Enable and start the service now"
    )
    for name in ("run", "serve", "check", "status", "stop", "logs"):
        p = sub.add_parser(name)
        p.add_argument("--agent", required=True)
        if name == "logs":
            p.add_argument("--lines", type=int, default=100)
        if name == "run":
            p.add_argument("--task-file", type=Path, required=True)
            p.add_argument(
                "--request-id",
                help="Reuse an assignment request ID after an uncertain submission",
            )
    args = parser.parse_args()
    store = ConfigStore(args.config)
    try:
        if args.command in ("configure-ui", "setup"):
            ui_store = UISettings(store.path)
            settings = ui_store.load()
            for name in ("host", "port", "public_url", "tls_certfile", "tls_keyfile"):
                value = getattr(args, name, None)
                if value is not None:
                    settings[name] = str(value) if isinstance(value, Path) else value
            if args.command == "configure-ui":
                if args.workspace is not None:
                    settings["default_workspace"] = str(
                        args.workspace.expanduser().resolve()
                    )
                if args.public_url and args.port is None:
                    parsed = urlsplit(args.public_url)
                    settings["port"] = parsed.port or (
                        443 if parsed.scheme == "https" else 80
                    )
                host = settings.get("host", "127.0.0.1")
                remote = remote_ui(host, settings.get("public_url", ""))
                if args.reset_password or (
                    remote and not settings.get("password_hash")
                ):
                    password = getpass.getpass(
                        "Choose UI password (at least 12 characters): "
                    )
                    if password != getpass.getpass("Repeat UI password: "):
                        raise ValueError(
                            "Passwords do not match; configuration was not changed"
                        )
                    settings["password_hash"] = password_hash(password)
                ui_store.save(settings)
                print(
                    f"UI settings saved. Browser URL: {validate_ui(settings)['public_url']}"
                )
                if settings.get("password_hash"):
                    print(
                        "Sign in as admin using your UI password (separate from AIOS/model keys)."
                    )
                return
            settings = validate_ui(settings)
        if args.command == "setup":
            import uvicorn

            from .server import create_app

            print(f"AIOS Client setup: {settings['public_url']}")
            if settings["password_hash"]:
                print("UI login: admin (use the password saved by configure-ui)")
            if not ipaddress.ip_address(settings["host"]).is_loopback and settings[
                "public_url"
            ].startswith("http:"):
                print(
                    "HTTP remote access is for a trusted private network; use HTTPS or a VPN on untrusted networks."
                )
            uvicorn.run(
                create_app(store, settings["port"], ui_settings=settings),
                host=settings["host"],
                port=settings["port"],
                ssl_certfile=settings["tls_certfile"],
                ssl_keyfile=settings["tls_keyfile"],
                proxy_headers=False,
                access_log=False,
            )
            return
        if args.command == "doctor":
            from .diagnostics import doctor

            result = doctor(store, args.agent, args.probe_model)
            print(json.dumps(result, indent=2))
            if not result["ok"]:
                parser.exit(1)
            return
        if args.command == "service":
            from .service import install_service

            unit = install_service(store, args.kind, args.agent, args.enable)
            print(f"Service saved: {unit}")
            print(f"Status: systemctl --user status {unit.stem}")
            print(f"Logs: journalctl --user -u {unit.stem} -f")
            print(
                "For startup before login, an administrator can run: loginctl enable-linger "
                + getpass.getuser()
            )
            return
        if args.agent not in store.load()["agents"]:
            raise ValueError("Unknown agent profile; run aios-client setup first")
        if args.command == "logs":
            if not 1 <= args.lines <= 10000:
                raise ValueError("Log lines must be between 1 and 10000")
            path = agent_dir(store, args.agent) / "activity.log"
            print(f"Activity log: {path}")
            if path.exists():
                with path.open(encoding="utf-8") as log:
                    print("".join(deque(log, maxlen=args.lines)), end="")
            else:
                print("No activity logged yet")
            return
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
                (
                    json.loads(args.task_file.read_text())
                    if args.task_file.suffix.lower() == ".json"
                    else args.task_file.read_text()
                )
                if args.command == "run"
                else None,
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
