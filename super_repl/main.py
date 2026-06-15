"""SuperREPL request server.

Routes a live, unscheduled stream of proof-check requests onto a fixed set of
persistent Lean processes (:class:`LeanInterface`). All of the *decision* logic
(clustering, warm-set, allocation, routing) lives in `policy.py` behind the
`RoutingPolicy` interface; this module owns the process pool and orchestration
only, so a different policy can be dropped in by passing it to `Server`.

Each request carries the full set of transitive imports it needs
(``Request.imports``); the policy clusters and routes on that set directly.
Execution is async: a process is a bridge with its own internal queue, so
`submit` just routes a request to a process and awaits its result — concurrent
`submit`s fan out across processes and queue up within one.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from time import time

from .basic import Request
from .lean_interface import LeanInterface, MethodResult
from .policy import AdaptivePolicy, RoutingPolicy


# --------------------------------------------------------------------------- #
# Server: ingest, periodic reconfigure, async dispatch
# --------------------------------------------------------------------------- #

class Server:
    """Owns the process pool and the rolling arrival window; defers every policy
    decision to `self.policy`.

    `submit` ingests one request (classify -> route -> run) and awaits its
    result. `reconfigure` is the slow loop that recomputes the
    process<->category assignment; run it on whatever cadence you like. Both are
    decoupled so the caller controls timing. Construction eagerly spawns every
    Lean bridge, so it blocks until the pool is ready.
    """

    def __init__(self, num_processes: int, lean_modules: list[str],
                 *, window_size: float = 10.0, policy: RoutingPolicy | None = None,
                 ready_timeout: float = 600.0):
        self.processes: list[LeanInterface] = [
            LeanInterface(lean_modules, id=i, ready_timeout=ready_timeout)
            for i in range(num_processes)
        ]
        self.policy = policy or AdaptivePolicy(num_processes)
        self.window_size = window_size
        self._window: deque[Request] = deque()        # rolling arrival window
        self.assignment: dict[int, list[int]] = {}    # category -> [process ids]

    async def submit(self, req: Request) -> MethodResult:
        """Classify, route, and run one request, returning its result.

        Only the classify/route bookkeeping is synchronous; the actual call is
        awaited on the chosen process's bridge. Multiple `submit`s may run
        concurrently — they spread across processes by `eta` and serialize within
        each process's own queue.
        """
        req.cluster = self.policy.classify(req)
        self._window.append(req)
        self._evict_window()
        proc = self.policy.route(req, self.processes, self.assignment)
        return await proc.handle_request(req)

    def reconfigure(self) -> None:
        """Recompute the process<->category assignment from recent demand."""
        self.assignment = self.policy.allocate(self.processes, self._demand())

    def close(self) -> None:
        """Tear down every Lean bridge in the pool."""
        for proc in self.processes:
            proc.close()

    def __enter__(self) -> "Server":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ---- internals ------------------------------------------------------- #
    def _evict_window(self) -> None:
        cutoff = time() - self.window_size
        while self._window and self._window[0].time_received < cutoff:
            self._window.popleft()

    def _demand(self) -> dict[int, float]:
        """Per-category arrival rate over the window -- the rolling average that
        keeps hotter categories provisioned."""
        self._evict_window()
        counts: dict[int, float] = {}
        for req in self._window:
            if req.cluster is not None:
                counts[req.cluster] = counts.get(req.cluster, 0.0) + 1.0
        span = max(self.window_size, 1e-9)
        return {c: n / span for c, n in counts.items()}


# --------------------------------------------------------------------------- #
# Tiny demo: route a few real checkLean requests across two bridges
# --------------------------------------------------------------------------- #

def _status(res: MethodResult) -> str:
    """Pull the ``status`` out of a checkLean result for display."""
    if isinstance(res.content, str):
        try:
            return json.loads(res.content).get("status", "?")
        except (json.JSONDecodeError, ValueError):
            return res.content[:40]
    return "?"


async def _demo(server: Server) -> None:
    cases = [
        ("def x := 1", frozenset({"Init.Prelude"})),
        ("def y : Nat := 2", frozenset({"Init.Prelude"})),
        ("theorem t : 1 = 1 := sorry", frozenset({"Init.Prelude"})),
        ("def broken := someUndefinedIdentifier", frozenset({"Init.Prelude"})),
    ]
    requests = [
        Request("checkLean",
                {"imports": sorted(imports), "codeWithoutImportStatements": code},
                imports=imports)
        for code, imports in cases
    ]

    # Concurrent submits: routed across processes, serialized within each.
    results = await asyncio.gather(*(server.submit(r) for r in requests))
    for req, res in zip(requests, results):
        code = req.params["codeWithoutImportStatements"]
        print(f"cluster={req.cluster}  is_error={res.is_error}  "
              f"status={_status(res)}  | {code}")

    # Now that the window holds demand, recompute the pinning.
    server.reconfigure()
    print("per-process load:  ", {p.id: p.load for p in server.processes})
    print("per-process cached:", {p.id: p.mem for p in server.processes})
    print("per-process pinned:", {p.id: p.cluster for p in server.processes})
    print("assignment:        ", server.assignment)


if __name__ == "__main__":
    # Spawn the pool before starting the event loop (construction blocks while
    # the bridges compile/import), then drive it.
    srv = Server(num_processes=2, lean_modules=["SuperREPL.Checker"])
    try:
        asyncio.run(_demo(srv))
    finally:
        srv.close()
