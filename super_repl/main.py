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
import signal
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from time import time
from typing import TYPE_CHECKING

from .basic import Request
from .lean_interface import LeanInterface, MethodResult, _signal_all_groups
from .policy import AdaptivePolicy, RoutingPolicy
from .service import DEFAULT_HOST, DEFAULT_PORT, make_app

if TYPE_CHECKING:
    from .memory import MemoryManager


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
                 ready_timeout: float = 600.0, startup_concurrency: int | None = None,
                 memory_budget: int | None = None,
                 memory_options: dict | None = None):
        self.processes: list[LeanInterface] = self._spawn_pool(
            num_processes, lean_modules,
            ready_timeout=ready_timeout, startup_concurrency=startup_concurrency,
        )
        self.policy = policy or AdaptivePolicy(num_processes)
        self.window_size = window_size
        self._window: deque[Request] = deque()        # rolling arrival window
        self.assignment: dict[int, list[int]] = {}    # category -> [process ids]

        # Optional RAM-budget guard: verify the freshly-spawned pool fits with
        # headroom, then monitor growth and restart the hungriest bridge when the
        # aggregate nears the budget. Set up before serving so a too-small budget
        # fails fast (tearing the pool down) rather than mid-traffic.
        self.memory = self._init_memory(memory_budget, memory_options)

        # Bridges run in their own sessions (so teardown can kill the whole
        # group), which detaches them from the host's process group — a Ctrl-C
        # or SIGTERM at the host no longer reaches them by itself. Forward
        # those signals explicitly so shutdown still takes the pool down.
        self._install_signal_forwarding()

    def _install_signal_forwarding(self) -> None:
        """Forward SIGINT/SIGTERM to every live bridge process group, then chain
        to the previously-installed handler (KeyboardInterrupt for SIGINT,
        process death for SIGTERM). A bridge exiting with ``-SIGINT``/``-SIGTERM``
        is recognised by its worker as an intentional shutdown, suppressing a
        doomed respawn. Only possible from the main thread; elsewhere the
        atexit sweep in ``lean_interface`` remains the fallback. The handlers
        are left installed after :meth:`close` — with no live groups they are
        no-ops that just chain."""
        if threading.current_thread() is not threading.main_thread():
            return
        for sig in (signal.SIGINT, signal.SIGTERM):
            prev = signal.getsignal(sig)

            def _handler(signum, frame, _prev=prev):
                _signal_all_groups(signum)
                if callable(_prev):
                    _prev(signum, frame)
                elif _prev == signal.SIG_DFL:
                    signal.signal(signum, signal.SIG_DFL)
                    signal.raise_signal(signum)

            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):  # non-main interpreter, etc.
                return

        # HTTP serving state (see `serve` / `url` / `stop_serving`).
        self._http_loop: asyncio.AbstractEventLoop | None = None
        self._http_thread: threading.Thread | None = None
        self._http_runner = None                      # aiohttp.web.AppRunner
        self._url: str | None = None

    def _init_memory(self, memory_budget: int | None,
                     memory_options: dict | None) -> "MemoryManager | None":
        """Build and arm the memory-budget guard, or return ``None`` when no
        budget is requested (``None`` or 0, which callers use for "unlimited").
        On a failed startup fit the already-spawned pool is torn down before the
        error propagates so no live processes leak."""
        if not memory_budget:
            return None
        from .memory import MemoryManager

        manager = MemoryManager(self.processes, memory_budget, **(memory_options or {}))
        try:
            manager.check_startup()
        except Exception:
            for proc in self.processes:
                proc.close()
            raise
        manager.start()
        return manager

    def memory_snapshot(self) -> dict | None:
        """Current memory-vs-budget view, or ``None`` if no budget is set."""
        return self.memory.snapshot() if self.memory is not None else None

    @staticmethod
    def _spawn_pool(num_processes: int, lean_modules: list[str], *,
                    ready_timeout: float, startup_concurrency: int | None) -> list[LeanInterface]:
        """Build the Lean modules once, then bring up ``num_processes`` bridges
        concurrently.

        Each ``LeanInterface`` construction blocks for seconds (the bridge
        spawns and imports before reporting its manifest), so spawning them in a
        thread pool collapses an otherwise ``num_processes``-fold serial wait to
        roughly one process's startup. The shared ``lake build`` is done once up
        front (concurrent builds of the same targets race on ``.lake/build``);
        the per-bridge constructions then only spawn read-only ``lake exe
        bridge`` processes, which is safe in parallel.

        ``startup_concurrency`` caps how many start at once (``None`` =
        unbounded) to bound the peak import memory/CPU spike on a constrained
        box. If any bridge fails to come up, the ones that did are closed before
        re-raising so we don't leak live processes.
        """
        if num_processes <= 0:
            return []

        LeanInterface.build_modules(lean_modules)

        max_workers = num_processes if startup_concurrency is None else max(1, startup_concurrency)
        processes: list[LeanInterface | None] = [None] * num_processes
        errors: list[BaseException] = []
        # Resolve *every* future before reacting to a failure: the pool's
        # __exit__ joins all spawns anyway, so a successful-but-late bridge must
        # be captured (and closed below) rather than leaked.
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="lean-spawn") as pool:
            futures = {
                pool.submit(
                    LeanInterface, lean_modules,
                    id=i, ready_timeout=ready_timeout, build=False,
                ): i
                for i in range(num_processes)
            }
            for fut, i in futures.items():
                try:
                    processes[i] = fut.result()
                except Exception as exc:
                    errors.append(exc)
        if errors:
            for proc in processes:
                if proc is not None:
                    proc.close()
            raise errors[0]
        return [p for p in processes if p is not None]

    # ---- HTTP serving ---------------------------------------------------- #
    @property
    def url(self) -> str:
        """Base URL this server is reachable at, once :meth:`serve` has bound a
        socket. Raises if it is not currently serving."""
        if self._url is None:
            raise RuntimeError("Server is not serving; call serve(..., background=True) first")
        return self._url

    def serve(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
              *, background: bool = False) -> str | None:
        """Serve the pool over HTTP.

        ``background=False`` (default) runs on the *current* thread and blocks
        until interrupted (Ctrl-C). ``background=True`` starts the server on a
        *daemon* thread with its own event loop and returns :attr:`url`
        immediately — so the same process can host the server and talk to it
        through a :class:`~service.Client`. Pass ``port=0`` for an OS-assigned
        port (read the chosen one off :attr:`url`).

        Stop a background server with :meth:`stop_serving` (or :meth:`close`,
        which also tears down the pool). This does not close the pool itself; do
        that with :meth:`close`.
        """
        if self._http_loop is not None:
            raise RuntimeError("Server is already serving")
        if background:
            return self._serve_background(host, port)
        self._serve_blocking(host, port)
        return None

    async def _http_start(self, host: str, port: int) -> None:
        """Bring up the aiohttp runner + site on the current loop and record url."""
        from aiohttp import web

        self._http_runner = web.AppRunner(make_app(self))
        await self._http_runner.setup()
        await web.TCPSite(self._http_runner, host, port).start()
        sockaddr = self._http_runner.addresses[0]     # resolves port==0
        self._url = f"http://{sockaddr[0]}:{sockaddr[1]}"

    def _serve_background(self, host: str, port: int) -> str:
        self._http_loop = asyncio.new_event_loop()
        self._http_thread = threading.Thread(
            target=self._http_loop.run_forever, daemon=True, name="server-http"
        )
        self._http_thread.start()
        # Bind on the background loop and block only until the socket is up.
        asyncio.run_coroutine_threadsafe(
            self._http_start(host, port), self._http_loop
        ).result()
        return self.url

    def _serve_blocking(self, host: str, port: int) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._http_loop = loop
        loop.run_until_complete(self._http_start(host, port))
        print(f"serving on {self._url}  (Ctrl-C to stop)")
        try:
            loop.run_forever()                        # serves until the loop stops
        except KeyboardInterrupt:
            pass
        finally:
            loop.run_until_complete(self._http_runner.cleanup())
            asyncio.set_event_loop(None)
            loop.close()
            self._reset_http_state()

    def stop_serving(self) -> None:
        """Stop a background HTTP server (no-op if not serving in the background).
        Leaves the pool running; close it with :meth:`close`."""
        loop, thread = self._http_loop, self._http_thread
        if loop is None or thread is None:
            # Not serving, or serving blocking on another thread (which tears
            # itself down on interrupt) — just nudge the loop to stop.
            if loop is not None:
                loop.call_soon_threadsafe(loop.stop)
            return
        if self._http_runner is not None:
            try:
                asyncio.run_coroutine_threadsafe(
                    self._http_runner.cleanup(), loop
                ).result(timeout=10)
            except Exception:
                pass
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        self._reset_http_state()

    def _reset_http_state(self) -> None:
        self._http_loop = None
        self._http_thread = None
        self._http_runner = None
        self._url = None

    def check_method(self, name: str) -> tuple[int, str] | None:
        """Validate that ``name`` is a callable public method.

        Returns ``None`` if the method exists on the bridges and is not
        ``internal`` (so it may be submitted); otherwise an ``(http_status,
        message)`` pair describing why it is rejected. Internal methods are
        treated as not-found so their existence is not advertised to clients.
        """
        if not self.processes:
            return 503, "no Lean processes available"
        try:
            method = self.processes[0].find_method(name)
        except ValueError:
            return 404, f"Unknown method: {name!r}"
        if method.internal:
            return 404, f"Unknown method: {name!r}"
        return None

    async def submit(self, req: Request, *, timeout: float | None = None,
                     priority: bool = False) -> MethodResult:
        """Classify, route, and run one request, returning its result.

        Only the classify/route bookkeeping is synchronous; the actual call is
        awaited on the chosen process's bridge. Multiple `submit`s may run
        concurrently — they spread across processes by `eta` and serialize within
        each process's own queue. ``timeout``/``priority`` are forwarded to the
        chosen bridge (see :meth:`LeanInterface.handle_request`).
        """
        req.cluster = self.policy.classify(req)
        self._window.append(req)
        self._evict_window()
        proc = self.policy.route(req, self.processes, self.assignment)
        return await proc.handle_request(req, timeout=timeout, priority=priority)

    def reconfigure(self) -> None:
        """Recompute the process<->category assignment from recent demand."""
        self.assignment = self.policy.allocate(self.processes, self._demand())

    def close(self) -> None:
        """Stop serving (if active) and tear down every Lean bridge in the pool."""
        self.stop_serving()
        if self.memory is not None:
            self.memory.stop()
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


# An end-to-end exercise of this module lives in `super_repl/tests.py`
# (`python -m super_repl.tests`); the standalone HTTP entry point is
# `super_repl/service.py` (`python -m super_repl.service`).
