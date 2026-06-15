"""Persistent Python client for the Lean ``bridge`` executable.

:class:`LeanInterface` keeps a single ``lake exe bridge`` process alive and
talks to it over its line-oriented JSON stdio REPL, exposing every Lean method
tagged ``@[expose_python]`` as a callable method. It is self-contained: construct
it, call its methods, and close it (or use it as a context manager).

The bridge protocol
-------------------
The bridge speaks newline-delimited JSON, each message terminated by a blank
line:

  * On startup, once the fresh Lean environment is ready, it emits a JSON
    **manifest array** describing the exposed methods, flushed, followed by a
    blank line. Each entry is::

        {"name": "<method>",
         "description": "<doc string>",
         "input_schema": {"<arg>": "<Lean type>", ...},
         "output": "<Lean return type>",
         "uses_imports": <bool>,
         "internal": <bool>}

    ``input_schema`` maps each argument name to a *Lean* type rendering
    (``Nat``, ``List String``, ``Option Int``, ...), not a JSON-Schema type;
    :mod:`lean_types` translates it into a JSON-Schema fragment. ``output`` is
    the rendered Lean return type, ``uses_imports`` flags methods that expose an
    "imports" function queryable via :meth:`LeanInterface.query_imports`, and
    ``internal`` (from ``@[internal expose_python]``) marks methods hidden from
    the default :meth:`LeanInterface.get_methods` listing but still callable.

  * To invoke a method, write a JSON request terminated by a blank line.
    ``args`` is a JSON **object keyed by argument name**::

        {"method": "<method>", "args": {"<arg>": <json-value>, ...}}

    Add ``"queryImports": true`` to instead run the method's imports function
    (only meaningful when ``uses_imports`` is true), returning the modules it
    needs rather than its result.

  * Each response is a single JSON object on one line, followed by a blank
    line. Alongside the result it reports the process's import state::

        {"result": "success", "value": <json-value>,
         "cachedModules": ["<module>", ...],   # modules now held by the process
         "importsTimeMs": <number>,            # ms spent importing this request
         "importCacheMisses": <int>}           # modules that missed the cache
        {"result": "error",   "value": "<message>",
         "cachedModules": [...], "importsTimeMs": ..., "importCacheMisses": ...}

    The import-accounting fields appear on both envelopes.
    :attr:`LeanInterface.cached_modules` always holds the latest ``cachedModules``
    snapshot, and the timing is fed to ``StatsTracker.submit_lean_processing_time``
    (import time split out from pure processing time).

The bridge handles one request at a time, so calls are funnelled through an
internal :class:`asyncio.PriorityQueue` drained by a single worker — the bridge
always sees exactly one request in flight. Pass ``priority=True`` to
:meth:`LeanInterface.call_method` to jump ahead of requests already queued behind
the worker (a request already mid-flight is never preempted; within a priority
level order stays FIFO). :meth:`LeanInterface.query_imports` cuts the line by
default.

Example::

    import asyncio

    async def main():
        with LeanInterface(["SuperREPL.Checker"]) as lean:
            print("Methods:", [t.name for t in lean.get_methods()])
            result = await lean.call_method(
                "checkLean",
                {"imports": [], "codeWithoutImportStatements": "def x := 1"},
            )
            print("Result:", result.content)

    asyncio.run(main())
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .lean_types import OPEN_SCHEMA, manifest_to_input_schema
from .heuristics import StatsTracker
from .basic import Request

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────
# Method value types
# ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MethodDef:
    """A Lean method exposed by the bridge.

    ``input_schema`` is the JSON-Schema translation of the method's Lean
    argument types (see :mod:`lean_types`); ``output`` is the rendered Lean
    return type; ``uses_imports`` is true when the method has an imports
    function reachable via :meth:`LeanInterface.query_imports`.

    ``internal`` flags methods tagged ``@[internal expose_python]`` on the Lean
    side: they remain callable by name but are hidden from the default
    :meth:`LeanInterface.get_methods` listing (intended for plumbing the model/UI
    should not be offered directly).
    """
    name: str
    description: str
    input_schema: dict[str, Any]
    output: str = ""
    uses_imports: bool = False
    internal: bool = False


@dataclass(frozen=True)
class MethodResult:
    """Result of calling a method. ``content`` is the decoded ``value`` from the
    bridge response; ``is_error`` is true for an error response (or a local
    failure such as a timeout or dead process)."""
    method: MethodDef
    content: Any
    is_error: bool = False


@dataclass
class _QueuedCall:
    """One pending request waiting for the worker. Enqueued as the payload of a
    ``(priority, seq, _QueuedCall)`` tuple; the unique ``seq`` orders equal
    priorities (and means this payload is never itself compared)."""
    future: "asyncio.Future[MethodResult]"
    name: str
    arguments: dict[str, Any] | None
    query_imports: bool
    timeout: float | None


# ──────────────────────────────────────────────────────────────────────────
# Lake root discovery
# ──────────────────────────────────────────────────────────────────────────


def _find_lake_root(start: Path | None = None) -> Path:
    """Locate the repo root by walking upward from ``start`` (this file's
    directory by default) until a directory containing ``lakefile.toml`` is
    found. Falls back to ``start`` if none is found, rather than guessing a
    fixed number of parents (which breaks if the package is moved/installed
    elsewhere)."""
    here = (start or Path(__file__).parent).resolve()
    for directory in (here, *here.parents):
        if (directory / "lakefile.toml").is_file():
            return directory
    logger.warning(
        "No lakefile.toml found walking up from %s; falling back to that directory.",
        here,
    )
    return here


# Repo root (where ``lakefile.toml`` lives), discovered by walking up from this
# file rather than hardcoding a parent count.
_DEFAULT_CWD = _find_lake_root()

# Modules always imported into every bridge, regardless of what the caller asks
# for. ``SuperREPL.DefaultTools`` carries the baseline ``@[expose_python]``
# methods that should be available in any session.
_ALWAYS_MODULES: tuple[str, ...] = ("SuperREPL.DefaultTools",)

# Max size of a single bridge response line. The bridge emits one compressed
# JSON object per line, and responses can be large (e.g. the ``cachedModules``
# list after importing ``Init``), so we lift asyncio's default 64 KiB
# StreamReader limit well above any plausible line.
_STREAM_LIMIT = 128 * 1024 * 1024  # 128 MiB


# ──────────────────────────────────────────────────────────────────────────
# LeanInterface — persistent client for the Lean ``bridge`` executable
# ──────────────────────────────────────────────────────────────────────────


class LeanInterface:
    """A persistent client for calling Lean methods through the ``bridge`` REPL.

    Keeps alive a single ``lake exe bridge --import mod1,mod2,...`` process on a
    private background event loop. Construction is eager and blocking: the
    background loop/thread is started, the bridge process is spawned, and we
    block until it reports its method manifest (or ``ready_timeout`` elapses).

    Call methods with :meth:`call_method` / :meth:`call_methods`; inspect the
    available methods with :meth:`get_methods` / :meth:`find_method`; and, for methods
    where ``uses_imports`` is set, ask which modules a call needs with
    :meth:`query_imports`. Release the process with :meth:`close` or by using
    the instance as a context manager.

    Requests are serviced one at a time, highest priority first (FIFO within a
    priority). Pass ``priority=True`` to :meth:`call_method` to cut ahead of
    already-queued requests; :meth:`query_imports` does so by default.

    Acts as the "process" object for the routing layer: ``id`` identifies it,
    ``cluster`` is the category it is currently pinned to (set by the policy),
    and the scheduling estimates (:meth:`load`, :meth:`eta`, ...) report its
    backlog and cache state.
    """

    def __init__(
        self,
        lean_modules: list[str],
        *,
        id: int = 0,
        cwd: str | Path | None = None,
        ready_timeout: float = 600.0,
    ) -> None:
        """Spawn the bridge for ``lean_modules`` and wait until it is ready.

        Args:
            lean_modules: Modules imported into the fresh Lean environment,
                passed to the bridge as a comma-separated list. The baseline
                methods in ``SuperREPL.DefaultTools`` (see :data:`_ALWAYS_MODULES`)
                are always prepended, deduplicated against this list.
            id: Integer identifying this process to the routing layer.
            cwd: Working directory for ``lake exe bridge``. Defaults to the
                repository root containing ``lakefile.toml``.
            ready_timeout: Seconds to wait for the bridge's manifest handshake.
                The first run may compile Lean, so this is large.
        """
        self.id = id
        self.cluster: int | None = None  # category pinned by the routing policy
        # Always include the default-tools module(s), without duplicating any the
        # caller already passed; dict.fromkeys preserves first-seen order.
        self.lean_modules = list(dict.fromkeys([*_ALWAYS_MODULES, *lean_modules]))
        self._cwd = str(cwd) if cwd is not None else str(_DEFAULT_CWD)
        self._ready_timeout = ready_timeout

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, daemon=True, name="lean-bridge-loop"
        )
        self._thread.start()

        self._proc: asyncio.subprocess.Process | None = None
        self._queue: "asyncio.PriorityQueue[tuple[int, int, _QueuedCall | None]] | None" = None
        self._worker_task: asyncio.Task | None = None
        self._seq = 0  # unique tiebreaker so equal-priority requests stay FIFO
        self._stderr_task: asyncio.Task | None = None
        self._method_defs: list[MethodDef] = []
        self._method_map: dict[str, MethodDef] = {}
        # Latest module-import snapshot reported by the bridge (updated on every
        # response): the modules the Lean process currently has cached.
        self.cached_modules: list[str] = []

        # Block until the process is up and has reported its methods.
        self._dispatch(self._async_setup())

    # ── Construction ─────────────────────────────────────────────

    async def _async_setup(self) -> None:
        self._queue = asyncio.PriorityQueue()

        # Build the requested modules first; fail loudly if any error or are
        # not found (lake returns nonzero and prints diagnostics).
        build = await asyncio.create_subprocess_exec(
            "lake", "build", *self.lean_modules, cwd=self._cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await build.communicate()
        if build.returncode != 0:
            raise RuntimeError(
                f"`lake build {' '.join(self.lean_modules)}` failed "
                f"(exit {build.returncode}):\n{out.decode(errors='replace')}"
            )

        cmd = ["lake", "exe", "bridge", "--import", ",".join(self.lean_modules)]
        logger.info("Starting Lean bridge: %s (cwd=%s)", " ".join(cmd), self._cwd)
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=self._cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_STREAM_LIMIT,  # responses (esp. `cachedModules`) exceed the 64 KiB default
        )

        # Drain stderr so the process can't block on a full pipe; surface it
        # for debugging (lake build output, Lean errors, etc.).
        self._stderr_task = self._loop.create_task(self._drain_stderr())

        try:
            manifest = await asyncio.wait_for(
                self._read_manifest(), timeout=self._ready_timeout
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeError(
                f"Lean bridge did not report its methods within {self._ready_timeout}s"
            ) from exc

        for entry in manifest:
            td = self._method_def_from_manifest(entry)
            if td is None:
                continue
            if td.name in self._method_map:
                logger.warning("Duplicate method name %r from bridge", td.name)
            self._method_map[td.name] = td
            self._method_defs.append(td)

        # Single consumer of the priority queue; nothing can enqueue until the
        # constructor returns, so starting it here (after the manifest) is safe.
        self._worker_task = self._loop.create_task(self._worker())
        logger.info("Lean bridge ready; methods: %s", sorted(self._method_map))

    @staticmethod
    def _method_def_from_manifest(entry: dict[str, Any]) -> MethodDef | None:
        """Build a :class:`MethodDef` from one bridge manifest entry, or ``None``
        if it is unusable (no name)."""
        name = str(entry.get("name", ""))
        if not name:
            logger.warning("Skipping manifest entry without a name: %r", entry)
            return None

        arg_types = entry.get("input_schema") or {}
        if not isinstance(arg_types, dict):
            logger.warning(
                "Method %r has non-object input_schema %r; treating as open",
                name, arg_types,
            )
            input_schema = dict(OPEN_SCHEMA)
        else:
            input_schema = manifest_to_input_schema(
                {k: str(v) for k, v in arg_types.items()}
            )

        return MethodDef(
            name=name,
            description=str(entry.get("description", "")),
            input_schema=input_schema,
            output=str(entry.get("output", "")),
            uses_imports=bool(entry.get("uses_imports", False)),
            internal=bool(entry.get("internal", False)),
        )

    # ── Internal: stdio helpers (run on the background loop) ──────

    async def _read_line(self) -> str:
        """Read one JSON line from the bridge, skipping the blank lines it emits
        after each message. Raises if the process has closed stdout (exited)."""
        if self._proc is None or self._proc.stdout is None:
            raise RuntimeError("Lean bridge process is not running")
        while True:
            raw = await self._proc.stdout.readline()
            if raw == b"":
                raise RuntimeError(
                    f"Lean bridge closed stdout (exited, returncode={self._proc.returncode})"
                )
            line = raw.decode(errors="replace").strip()
            if line:
                return line

    async def _read_manifest(self) -> list[dict[str, Any]]:
        line = await self._read_line()
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Expected JSON method manifest from bridge, got: {line!r} ({exc})"
            ) from exc
        if not isinstance(data, list):
            raise RuntimeError(
                f"Expected JSON method manifest (array) from bridge, got: {line!r}"
            )
        return [e for e in data if isinstance(e, dict)]

    async def _drain_stderr(self) -> None:
        if self._proc is None or self._proc.stderr is None:
            return
        while True:
            raw = await self._proc.stderr.readline()
            if raw == b"":
                break
            logger.debug("[bridge stderr] %s", raw.decode(errors="replace").rstrip())

    # ── Internal: dispatch to background loop ────────────────────

    def _dispatch(self, coro: Any) -> Any:
        """Schedule a coroutine on the background loop and block until done."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()

    async def _dispatch_async(self, coro: Any) -> Any:
        """Schedule a coroutine on the background loop and await it from the
        caller's loop without touching the bridge from the wrong loop."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return await asyncio.wrap_future(future)

    # ── Public interface ─────────────────────────────────────────

    def get_methods(self, *, include_internal: bool = False) -> list[MethodDef]:
        """Return the method definitions reported by the bridge.

        Methods tagged ``@[internal expose_python]`` (``MethodDef.internal``)
        are omitted by default; pass ``include_internal=True`` to list them too.
        Internal methods stay callable by name via :meth:`call_method` regardless.
        """
        if include_internal:
            return list(self._method_defs)
        return [t for t in self._method_defs if not t.internal]

    def find_method(self, name: str) -> MethodDef:
        """Return the method named ``name``, raising ``ValueError`` if unknown.

        Resolves internal methods as well — ``internal`` only hides a method from
        the default :meth:`get_methods` listing, not from lookup or calling.
        """
        td = self._method_map.get(name)
        if td is None:
            raise ValueError(f"Method {name!r} does not exist")
        return td

    # ── Scheduling estimates ─────────────────────────────────────
    # Ported from the dummy ``LeanProcess`` so a real bridge is a drop-in for the
    # routing policy. Structural counts (queue depth, missing modules) live here;
    # the per-unit time estimates come from :class:`StatsTracker`, which owns the
    # statistic (currently an average) so it can be swapped without touching this.

    @property
    def load(self) -> int:
        """Number of requests waiting in this process's queue."""
        return self._queue.qsize() if self._queue is not None else 0

    @property
    def mem(self) -> int:
        """Rough memory proxy: count of modules cached. DUMMY for now (not yet
        wired to a real memory estimate)."""
        return len(self.cached_modules)

    def time_to_import(self, imports: frozenset[str]) -> float:
        """Estimated seconds to import the modules in ``imports`` not yet cached:
        the number still missing times the per-module import estimate."""
        missing = set(imports) - set(self.cached_modules)
        return len(missing) * StatsTracker.get_imports_time()

    def drain_time(self) -> float:
        """Estimated seconds to clear the current queue: queue depth times the
        per-request processing estimate."""
        return self.load * StatsTracker.get_lean_processing_time()

    def eta(self, imports: frozenset[str]) -> float:
        """When this process could *finish* a request needing ``imports``: drain
        the current backlog, then import whatever is still missing."""
        return self.drain_time() + self.time_to_import(imports)

    async def call_method(
        self,
        method_name: str,
        input_data: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
        priority: bool = False,
    ) -> MethodResult:
        """Call a bridge method by name and await its result.

        ``input_data`` is forwarded verbatim as the request's ``args``.
        ``timeout`` (seconds) bounds the wait for the response; ``None``
        disables it. On timeout the call is surfaced as an error ``MethodResult``
        so a method-loop can keep going rather than stalling.

        ``priority=True`` jumps this request ahead of others already queued
        behind the worker (a request already mid-flight is not preempted).
        """
        return await self._dispatch_async(
            self._async_call(method_name, input_data, timeout=timeout, priority=priority)
        )

    async def call_methods(
        self,
        calls: list[tuple[str, dict[str, Any] | None]],
        *,
        timeout: float | None = None,
        priority: bool = False,
    ) -> list[MethodResult]:
        """Call a batch of ``(name, args)`` pairs, returning results in order.

        Calls run strictly sequentially: the bridge serves one request at a
        time, so there is nothing to gain from fanning out. ``priority`` is
        forwarded to each call (see :meth:`call_method`).
        """
        results: list[MethodResult] = []
        for name, args in calls:
            results.append(
                await self.call_method(name, args, timeout=timeout, priority=priority)
            )
        return results

    async def handle_request(
        self,
        request: Request,
        *,
        timeout: float | None = None,
        priority: bool = False,
    ) -> MethodResult:
        """Run a :class:`~basic.Request` against the bridge.

        Unpacks the request's ``method`` and ``params`` and dispatches through
        :meth:`call_method`. The ``imports``/``cluster``/``uuid`` fields describe
        routing/scheduling and are consumed by the policy layer, not by the call
        itself, so they are ignored here.
        """
        return await self.call_method(
            request.method, request.params, timeout=timeout, priority=priority
        )

    async def query_imports(
        self,
        method_name: str,
        input_data: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
        priority: bool = True,
    ) -> MethodResult:
        """Run a method's imports function and await the modules it needs.

        Only meaningful for methods whose ``uses_imports`` is true; for others the
        bridge returns an error ``MethodResult``. ``input_data`` is forwarded as
        the request's ``args`` (the imports function takes the same arguments as
        the method).

        Cuts the line by default (``priority=True``): import queries are usually
        used to decide caching/placement before issuing the real call, so they
        should not wait behind a backlog. Pass ``priority=False`` to queue it
        normally.
        """
        return await self._dispatch_async(
            self._async_call(
                method_name, input_data, timeout=timeout,
                query_imports=True, priority=priority,
            )
        )

    async def _async_call(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
        query_imports: bool = False,
        priority: bool = False,
    ) -> MethodResult:
        """Enqueue a request and await the worker's result. Runs on the
        background loop, so touching ``self._seq``/the queue here is race-free."""
        if name not in self._method_map:
            return MethodResult(
                method=MethodDef(name=name, description="", input_schema=dict(OPEN_SCHEMA)),
                content=f"Unknown method: {name!r}",
                is_error=True,
            )

        assert self._queue is not None
        future: asyncio.Future[MethodResult] = self._loop.create_future()
        self._seq += 1
        # Lower number = higher priority; the unique, increasing seq keeps
        # equal-priority requests FIFO and makes the queued tuples comparable
        # without ever comparing the `_QueuedCall` payload.
        prio = 0 if priority else 1
        self._queue.put_nowait((prio, self._seq, _QueuedCall(
            future=future, name=name, arguments=arguments,
            query_imports=query_imports, timeout=timeout,
        )))
        return await future

    async def _worker(self) -> None:
        """Single consumer of the request queue: pull the highest-priority
        queued call (FIFO within a priority) and run it to completion before
        taking the next, so the bridge only ever sees one request at a time. A
        ``None`` payload is a shutdown sentinel."""
        assert self._queue is not None
        while True:
            _prio, _seq, job = await self._queue.get()
            try:
                if job is None:
                    return
                if job.future.done():  # caller already gave up; skip the work
                    continue
                try:
                    result = await self._execute_call(
                        job.name, job.arguments,
                        query_imports=job.query_imports, timeout=job.timeout,
                    )
                    job.future.set_result(result)
                except Exception as exc:  # defensive: _execute_call returns errors
                    if not job.future.done():
                        job.future.set_exception(exc)
            finally:
                self._queue.task_done()

    async def _execute_call(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        *,
        query_imports: bool,
        timeout: float | None,
    ) -> MethodResult:
        """Perform one request/response exchange with the bridge. Only ever
        called from :meth:`_worker`, so exactly one exchange is in flight; no
        lock is needed."""
        method = self._method_map.get(name) or MethodDef(
            name=name, description="", input_schema=dict(OPEN_SCHEMA)
        )

        payload: dict[str, Any] = {
            "method": name,
            "args": arguments if arguments is not None else {},
        }
        if query_imports:
            payload["queryImports"] = True
        # Request is the JSON object followed by a blank line (the bridge reads
        # until it sees a blank line).
        request = (json.dumps(payload) + "\n\n").encode()

        if self._proc is None or self._proc.stdin is None:
            return MethodResult(
                method=method, content="Lean bridge is not running", is_error=True
            )
        # Time the round trip so the cache policy can reason about how long the
        # Lean process spends per call (import time is split out via the stats
        # the bridge reports below).
        started = time.perf_counter()
        try:
            self._proc.stdin.write(request)
            await self._proc.stdin.drain()
            read = self._read_line()
            line = (
                await asyncio.wait_for(read, timeout)
                if timeout is not None
                else await read
            )
        except asyncio.TimeoutError:
            logger.warning("Lean method %r timed out after %.1fs", name, timeout or 0.0)
            return MethodResult(
                method=method,
                content=f"Method {name!r} timed out after {timeout}s",
                is_error=True,
            )
        except Exception as exc:  # process died, broken pipe, etc.
            logger.warning("Lean method %r raised: %s", name, exc)
            return MethodResult(method=method, content=f"Method error: {exc}", is_error=True)
        total_time = time.perf_counter() - started

        try:
            resp = json.loads(line)
        except json.JSONDecodeError as exc:
            return MethodResult(
                method=method,
                content=f"Malformed response from bridge: {line!r} ({exc})",
                is_error=True,
            )

        # Both success and error envelopes carry the import accounting; record it
        # before unwrapping the value so timing/cache stats are captured either way.
        self._record_import_stats(resp, total_time)

        status = resp.get("result")
        value = resp.get("value")
        # `json.loads(line)` only decoded the bridge envelope; a method's `value`
        # may itself be a JSON-encoded string (the Lean side serialized its
        # result with `toJson`, which can double-encode), so decode it too. The
        # decoded value may be a non-string (object/array/number/bool) — those
        # pass straight through to be re-serialized for `content` below. A plain,
        # non-JSON string is left as-is.
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (json.JSONDecodeError, ValueError):
                pass
        content = value if isinstance(value, str) else json.dumps(value)
        return MethodResult(method=method, content=content, is_error=(status != "success"))

    def _record_import_stats(self, resp: dict[str, Any], total_time: float) -> None:
        """Update the cached-module snapshot and feed import timing to the stats
        tracker from one bridge response envelope.

        The bridge reports ``cachedModules`` (modules the process now holds),
        ``importsTimeMs`` (milliseconds spent importing for this request) and
        ``importCacheMisses`` (modules that actually had to be imported, i.e.
        cache misses). ``submit_lean_processing_time`` subtracts the import time
        from ``total_time`` to isolate pure processing time and, when imports
        happened, records the import time and per-miss average.
        """
        cached = resp.get("cachedModules")
        if isinstance(cached, list):
            self.cached_modules = [str(m) for m in cached]

        try:
            imports_total_time = float(resp.get("importsTimeMs") or 0) / 1000.0
        except (TypeError, ValueError):
            imports_total_time = 0.0
        try:
            num_imports = int(resp.get("importCacheMisses") or 0)
        except (TypeError, ValueError):
            num_imports = 0

        StatsTracker.submit_lean_processing_time(
            total_time, num_imports=num_imports, imports_total_time=imports_total_time
        )

    # ── Lifecycle ────────────────────────────────────────────────

    def close(self) -> None:
        """Stop the bridge process and the background loop."""
        try:
            self._dispatch(self._async_close())
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)

    async def _async_close(self) -> None:
        # Stop the worker (sentinel jumps ahead of any queued work), then fail
        # whatever is still queued so awaiters don't hang on a departing bridge.
        if self._queue is not None:
            self._queue.put_nowait((-1, -1, None))
        if self._worker_task is not None:
            try:
                await asyncio.wait_for(self._worker_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._worker_task.cancel()
        if self._queue is not None:
            while not self._queue.empty():
                _prio, _seq, job = self._queue.get_nowait()
                if job is not None and not job.future.done():
                    job.future.set_exception(RuntimeError("Lean bridge closed"))

        proc = self._proc
        if proc is None:
            return
        if proc.returncode is None:
            # A blank line makes the REPL's read return empty and the loop exit
            # gracefully.
            try:
                if proc.stdin is not None:
                    proc.stdin.write(b"\n")
                    await proc.stdin.drain()
                    proc.stdin.close()
            except Exception:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    proc.kill()
        if self._stderr_task is not None:
            self._stderr_task.cancel()

    def __enter__(self) -> "LeanInterface":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()
