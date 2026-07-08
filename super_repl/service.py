"""Network layer: expose a :class:`~main.Server` over HTTP and a :class:`Client`
to submit requests to it.

The server side uses ``aiohttp`` (the async :class:`~main.Server` plugs straight
into its event loop); the client uses ``httpx``. Both are imported lazily, so
importing this module is cheap and a process that only needs the :class:`Client`
never pulls in ``aiohttp`` (and vice-versa).

Wire protocol (JSON over HTTP)::

    GET  /health       -> {"status": "ok", "processes": <int>}
    GET  /methods      -> [{name, description, input_schema, output,
                            uses_imports, internal}, ...]
    POST /submit       <- {"method": <str>, "params": {...},
                           "imports": [<module>, ...],
                           "timeout": <float|null>, "priority": <bool>}
                       -> 200 {"content": <json>, "is_error": <bool>,
                               "method": <str>, "cluster": <int|null>}
                       -> 400 {"error": <str>}   # malformed request body
                       -> 404 {"error": <str>}   # unknown or internal method
    POST /reconfigure  -> {"assignment": {<cluster>: [<pid>, ...], ...}}

``/submit`` first checks that ``method`` names an existing, non-internal method
(internal methods are reported as unknown, 404); only then does it route the
request through the server's policy onto one of the Lean bridges and return the
bridge's :class:`~lean_interface.MethodResult` (plus the category the policy
assigned). Long Lean calls are fine: the HTTP read is unbounded, only the
optional per-call ``timeout`` (applied bridge-side) bounds a call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable

from .basic import Request

if TYPE_CHECKING:
    from .main import Server

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


# ──────────────────────────────────────────────────────────────────────────
# Shared (de)serialization
# ──────────────────────────────────────────────────────────────────────────


def _request_from_json(body: dict[str, Any]) -> Request:
    """Build a :class:`Request` from a ``/submit`` body, validating shapes."""
    method = body.get("method")
    if not isinstance(method, str) or not method:
        raise ValueError("`method` must be a non-empty string")
    params = body.get("params") or {}
    if not isinstance(params, dict):
        raise ValueError("`params` must be a JSON object")
    raw_imports = body.get("imports") or []
    if not isinstance(raw_imports, (list, tuple)):
        raise ValueError("`imports` must be a JSON array")
    return Request(method=method, params=params, imports=frozenset(map(str, raw_imports)))


def _method_def_to_json(m: Any) -> dict[str, Any]:
    return {
        "name": m.name,
        "description": m.description,
        "input_schema": m.input_schema,
        "output": m.output,
        "uses_imports": m.uses_imports,
        "internal": m.internal,
    }


# ──────────────────────────────────────────────────────────────────────────
# Server side (aiohttp)
# ──────────────────────────────────────────────────────────────────────────


def make_app(server: "Server"):
    """Build the aiohttp application exposing ``server`` (see module protocol).

    This is a pure HTTP wrapper: it does not own the pool's lifecycle. Tearing
    the app down stops serving but leaves the bridges running; close them via
    ``server.close()`` (which :meth:`Server.serve` / :func:`serve` arrange).
    """
    from aiohttp import web

    routes = web.RouteTableDef()

    @routes.get("/health")
    async def _health(request: "web.Request") -> "web.Response":
        body: dict[str, Any] = {"status": "ok", "processes": len(server.processes)}
        memory = server.memory_snapshot()
        if memory is not None:
            body["memory"] = memory
        return web.json_response(body)

    @routes.get("/methods")
    async def _methods(request: "web.Request") -> "web.Response":
        defs = server.processes[0].get_methods(include_internal=True) if server.processes else []
        return web.json_response([_method_def_to_json(m) for m in defs])

    @routes.post("/submit")
    async def _submit(request: "web.Request") -> "web.Response":
        try:
            body = await request.json()
            req = _request_from_json(body)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except Exception as exc:  # malformed JSON, etc.
            return web.json_response({"error": f"invalid request: {exc}"}, status=400)

        # Only existing, non-internal methods may be submitted.
        rejection = server.check_method(req.method)
        if rejection is not None:
            status, message = rejection
            return web.json_response({"error": message}, status=status)

        timeout = body.get("timeout")
        priority = bool(body.get("priority", False))
        result = await server.submit(req, timeout=timeout, priority=priority)
        return web.json_response({
            "content": result.content,
            "is_error": result.is_error,
            "method": result.method.name,
            "cluster": req.cluster,
        })

    @routes.post("/reconfigure")
    async def _reconfigure(request: "web.Request") -> "web.Response":
        server.reconfigure()
        return web.json_response({"assignment": server.assignment})

    app = web.Application()
    app["server"] = server
    app.add_routes(routes)
    return app


def serve(server: "Server", *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    """Serve ``server`` over HTTP on the current thread until interrupted, then
    close its pool.

    Thin wrapper over :meth:`Server.serve` for standalone/CLI use; in-process
    callers that want to keep using the server should call
    ``server.serve(background=True)`` instead and point a :class:`Client` at
    :attr:`Server.url`.
    """
    try:
        server.serve(host=host, port=port)
    finally:
        server.close()


# ──────────────────────────────────────────────────────────────────────────
# Client side (httpx)
# ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SubmitResult:
    """A ``/submit`` response: the bridge's result plus the assigned category."""
    content: Any
    is_error: bool
    method: str
    cluster: int | None


class Client:
    """Submit requests to a remote :class:`~main.Server` over HTTP.

    Async-first (the server and bridges are async). Reuses one ``httpx.AsyncClient``
    with no read timeout — Lean calls can run for a while; bound them with the
    per-call ``timeout`` instead, which is applied bridge-side. Use as an async
    context manager, or call :meth:`aclose` when done.
    """

    def __init__(self, url: str = f"http://{DEFAULT_HOST}:{DEFAULT_PORT}",
                 *, connect_timeout: float = 30.0) -> None:
        self._url = url.rstrip("/")
        self._connect_timeout = connect_timeout
        self._client = None  # lazily created httpx.AsyncClient

    def _http(self):
        import httpx

        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._url,
                timeout=httpx.Timeout(None, connect=self._connect_timeout),
            )
        return self._client

    async def health(self) -> dict[str, Any]:
        resp = await self._http().get("/health")
        resp.raise_for_status()
        return resp.json()

    async def get_methods(self) -> list[dict[str, Any]]:
        """Public method descriptors exposed by the server's bridges.

        Methods flagged ``internal`` are omitted — they are implementation
        details not meant for external callers.
        """
        resp = await self._http().get("/methods")
        resp.raise_for_status()
        return [m for m in resp.json() if not m.get("internal")]

    async def reconfigure(self) -> dict[str, Any]:
        """Ask the server to recompute its process<->category assignment."""
        resp = await self._http().post("/reconfigure")
        resp.raise_for_status()
        return resp.json()

    async def submit(self, request: Request, *, timeout: float | None = None,
                     priority: bool = False) -> SubmitResult:
        """Submit a :class:`Request` and await its result."""
        payload = {
            "method": request.method,
            "params": request.params,
            "imports": sorted(request.imports),
            "timeout": timeout,
            "priority": priority,
        }
        resp = await self._http().post("/submit", json=payload)
        resp.raise_for_status()
        data = resp.json()
        return SubmitResult(
            content=data.get("content"),
            is_error=bool(data.get("is_error", True)),
            method=str(data.get("method", request.method)),
            cluster=data.get("cluster"),
        )

    async def call(self, method: str, params: dict[str, Any] | None = None, *,
                   imports: Iterable[str] = (), timeout: float | None = None,
                   priority: bool = False) -> SubmitResult:
        """Convenience: build a :class:`Request` and :meth:`submit` it."""
        req = Request(method=method, params=params or {}, imports=frozenset(imports))
        return await self.submit(req, timeout=timeout, priority=priority)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "Client":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()


# ──────────────────────────────────────────────────────────────────────────
# CLI entry: `python -m super_repl.service --processes N --modules a,b --port P`
# ──────────────────────────────────────────────────────────────────────────


def _main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Serve a SuperREPL Server over HTTP.")
    parser.add_argument("--processes", type=int, default=2, help="number of Lean bridges")
    parser.add_argument("--modules", default="SuperREPL.Checker",
                        help="comma-separated Lean modules to import into each bridge")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--memory-budget-gb", type=float, default=None,
                        help="aggregate RAM budget (GiB) for the Lean pool; the "
                             "hungriest bridge is restarted as the total nears it")
    args = parser.parse_args(argv)

    from .main import Server  # local import: keeps the Client free of server deps

    modules = [m.strip() for m in args.modules.split(",") if m.strip()]
    memory_budget = (
        int(args.memory_budget_gb * 1024 ** 3)
        if args.memory_budget_gb is not None else None
    )
    server = Server(args.processes, modules, memory_budget=memory_budget)
    serve(server, host=args.host, port=args.port)


if __name__ == "__main__":
    _main()
