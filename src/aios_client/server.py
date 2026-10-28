from __future__ import annotations

import secrets
import threading
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .assignment import assignment
from .config import ConfigStore, aios_url
from .http import API, Model
from .journal import Journal
from .protocol import negotiate
from .runner import Runner, agent_dir


class Manager:
    def __init__(self, store):
        self.store = store
        self.lock = threading.Lock()
        self.running = {}
        self.errors = {}
        self.gates = {}

    def active(self):
        return any(t.is_alive() for r, t in self.running.values())

    def start(self, agent, task, serve=False):
        with self.lock:
            if agent in self.running and self.running[agent][1].is_alive():
                raise ValueError("Agent already running")
            c = self.store.load()
            p = c["agents"][agent]
            # First pass intentionally permits one active local profile. Profile schema and
            # per-endpoint gates are ready for multiple agents once workspace concurrency is tested.
            if self.active():
                raise ValueError("First pass supports one active agent at a time")
            e = c["endpoints"][p["endpoint"]]
            self.gates[p["endpoint"]] = threading.BoundedSemaphore(
                int(e.get("concurrency", 1))
            )
            runner = Runner(self.store, agent, self.gates)

            def work():
                try:
                    runner.work(task or None, serve=serve)
                except Exception as exc:
                    self.errors[agent] = str(exc)[:1000]
                finally:
                    runner.close()

            thread = threading.Thread(target=work, daemon=True)
            self.running[agent] = (runner, thread)
            self.errors.pop(agent, None)
            thread.start()

    def stop(self, agent):
        if agent in self.running:
            self.running[agent][0].stop()

    def close(self):
        for r, t in self.running.values():
            r.stop()
        for r, t in self.running.values():
            t.join(timeout=3)


def create_app(store: ConfigStore, port=8765):
    from contextlib import asynccontextmanager

    manager = Manager(store)

    @asynccontextmanager
    async def lifespan(app):
        yield
        manager.close()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.manager = manager
    csrf = secrets.token_urlsafe(32)
    origin = f"http://127.0.0.1:{port}"

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        if request.headers.get("host") != f"127.0.0.1:{port}":
            return JSONResponse(
                {"error": "Use the printed loopback URL"}, status_code=403
            )
        if request.headers.get("origin") not in (None, origin):
            return JSONResponse({"error": "Foreign origin denied"}, status_code=403)
        if request.method != "GET" and not secrets.compare_digest(
            request.headers.get("x-client-token", ""), csrf
        ):
            return JSONResponse({"error": "Missing setup token"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; connect-src 'self'"
        )
        return response

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({"error": "Unknown or incomplete profile"}, status_code=400)

    @app.exception_handler(RuntimeError)
    async def unavailable(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=502)

    @app.get("/", response_class=HTMLResponse)
    def home():
        html = Path(__file__).with_name("static").joinpath("setup.html").read_text()
        return html.replace("__CSRF__", csrf)

    @app.get("/api/config")
    def config():
        return store.load()

    @app.post("/api/config")
    def save(data: dict):
        if manager.active():
            raise ValueError("Stop the active agent before editing configuration")
        store.save(data)
        return {"ok": True}

    @app.post("/api/secret")
    def secret(data: dict):
        if manager.active():
            raise ValueError("Stop the agent before changing credentials")
        if data.get("remove"):
            store.secrets.delete(data.get("ref", ""))
            return {"secret_ref": ""}
        name = data.get("name", "")
        if not name or len(name) > 100 or not isinstance(data.get("value"), str):
            raise ValueError("Secret name and value required")
        return {"secret_ref": store.secrets.set(name, data["value"])}

    @app.post("/api/test-aios")
    def test_aios(data: dict):
        p = data["profile"]
        api = API(aios_url(p), store.secrets.get(p.get("secret_ref", "")))
        try:
            result = api.request("GET", "workspace-client/protocol")
            version = negotiate(result)
            return {
                "ok": True,
                "url": aios_url(p),
                "protocol": result,
                "negotiated_version": version,
            }
        finally:
            api.close()

    @app.post("/api/models")
    def models(data: dict):
        from .config import base_url

        p = data["profile"]
        base_url(p["base_url"])
        model = Model(
            p["base_url"],
            store.secrets.get(p.get("secret_ref", "")),
            float(p.get("timeout", 120)),
        )
        try:
            return {"models": model.models()}
        finally:
            model.close()

    @app.post("/api/test-model")
    def test_model(data: dict):
        from .config import base_url

        p = data["profile"]
        base_url(p["base_url"])
        model = Model(
            p["base_url"],
            store.secrets.get(p.get("secret_ref", "")),
            float(p.get("timeout", 120)),
        )
        try:
            return model.test_tools(p["model"])
        finally:
            model.close()

    @app.post("/api/sessions")
    def sessions(data: dict):
        p = store.load()["aios"][data["aios"]]
        api = API(aios_url(p), store.secrets.get(p.get("secret_ref", "")))
        try:
            return api.request(
                "GET",
                "workspace-client/instances",
                params={"character_id": data["character_id"]},
            )
        finally:
            api.close()

    @app.post("/api/test-agent")
    def test_agent(data: dict):
        runner = Runner(store, data["agent"])
        try:
            b = runner.binding()
            return {
                "ok": True,
                "binding_id": b["binding_id"],
                "instance_id": b["instance_id"],
                "protocol_version": runner.protocol_version,
                "link_id": runner.profile.get("link_id"),
            }
        finally:
            runner.close()

    @app.post("/api/start")
    def start(data: dict):
        task = (
            None
            if data.get("serve")
            else assignment(data.get("assignment") or data.get("task", "")).model_dump(
                mode="json"
            )
        )
        manager.start(data["agent"], task, bool(data.get("serve")))
        return {"ok": True}

    @app.post("/api/stop")
    def stop(data: dict):
        manager.stop(data["agent"])
        return {"ok": True}

    @app.get("/api/status")
    def status():
        result = []
        for name in store.load()["agents"]:
            journal = Journal(agent_dir(store, name) / "journal.sqlite3")
            try:
                result.append(
                    {
                        "agent": name,
                        "active": name in manager.running
                        and manager.running[name][1].is_alive(),
                        "error": manager.errors.get(name),
                        "runs": journal.status(),
                    }
                )
            finally:
                journal.db.close()
        return result

    @app.get("/api/events")
    def events(agent: str, run: str):
        if agent not in store.load()["agents"]:
            raise ValueError("Unknown agent")
        journal = Journal(agent_dir(store, agent) / "journal.sqlite3")
        try:
            return journal.events(run)
        finally:
            journal.db.close()

    return app
