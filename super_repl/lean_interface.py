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
    ``args`` is a JSON **object keyed by argument name**. An ``id`` correlates
    the request with its response (echoed back verbatim) so late replies from a
    timed-out request can't be mistaken for a later request's result::

        {"id": <int>, "method": "<method>", "args": {"<arg>": <json-value>, ...}}

    Add ``"queryImports": true`` to instead run the method's imports function
    (only meaningful when ``uses_imports`` is true), returning the modules it
    needs rather than its result.

  * Each response is a single JSON object on one line, followed by a blank
    line. It echoes the request's ``id`` and reports the process's import
    state alongside the result::

        {"id": <int>, "result": "success", "value": <json-value>,
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
import atexit
import json
import logging
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .lean_types import OPEN_SCHEMA, manifest_to_input_schema
from .heuristics import StatsTracker
from .basic import Request

logger = logging.getLogger(__name__)


def _tree_rss_bytes(pid: int) -> int | None:
    """Total resident memory (bytes) of process ``pid`` plus all its descendants,
    or ``None`` if it can't be read.

    A bridge's ``self._proc`` is the ``lake`` launcher; the heavy Lean work runs
    in its ``lean`` children (``lake -> lean -> lean``), so the meaningful figure
    is the whole tree, not just the root. ``psutil`` is imported lazily so
    importing this module stays cheap and psutil stays an optional dependency
    (returns ``None`` when it is unavailable). Safe to call from any thread."""
    try:
        import psutil
    except ImportError:
        return None
    try:
        root = psutil.Process(pid)
        procs = [root, *root.children(recursive=True)]
    except psutil.Error:
        return None
    total = 0
    for p in procs:
        try:
            total += p.memory_info().rss
        except psutil.Error:
            # A child may exit mid-walk; skip it rather than lose the whole read.
            continue
    return total


def _descendant_procs(pid: int) -> list:
    """The live descendants of ``pid`` — a bridge's actual Lean workers.

    ``self._proc`` is only the ``lake`` launcher; the heavy Lean work runs in its
    ``lean`` descendants (``lake -> lean -> lean``). Killing just the launcher
    leaves those workers orphaned (reparented to init) and still holding memory.

    This walk is only the *secondary* teardown pass: it finds nothing that has
    already been reparented (or that spawns between the snapshot and the kill),
    which is why the primary reap is the process-group kill in
    :func:`_kill_group`. It remains useful for anything that left the group.
    Must be called while ``pid`` is still alive. Returns ``[]`` when psutil is
    unavailable or the walk fails; each returned ``psutil.Process`` caches the
    pid's identity, so signalling it later is guarded against pid reuse. Safe to
    call from any thread."""
    try:
        import psutil
    except ImportError:
        return []
    try:
        return psutil.Process(pid).children(recursive=True)
    except Exception:
        return []


def _kill_procs(procs: list) -> None:
    """SIGKILL every process in ``procs``, skipping any already gone.

    Used to reap the Lean workers left behind when only the ``lake`` launcher is
    signalled. psutil verifies each process's identity before signalling, so a
    reused pid is skipped rather than mis-killed. Best-effort; safe on an empty
    list (e.g. when psutil is unavailable)."""
    for p in procs:
        try:
            p.kill()
        except Exception:
            # Already exited, reaped, or reused: nothing to kill here.
            continue


def _kill_group(pgid: int, sig: int = signal.SIGKILL) -> None:
    """Signal the whole process group led by ``pgid``.

    Bridges are spawned as their own group leaders (``start_new_session`` in
    :meth:`LeanInterface._spawn_proc`), so a bridge's pgid is its launcher's
    pid, and the group reaches every ``lean`` worker *regardless of
    reparenting* — the failure mode that makes a downward PPID walk
    insufficient. A pgid stays valid (and the pid unreusable) while any member
    of the group is alive, including members whose parent already exited.
    Best-effort: a fully-dead group raises ``ProcessLookupError``, which is the
    success case."""
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _group_alive(pgid: int) -> bool:
    """Whether process group ``pgid`` still has members (coarse: counts
    zombies awaiting reap). ``PermissionError`` means something exists but is
    not ours to signal — report it alive so verification stays loud."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _group_survivors(pgid: int) -> list[tuple[int, int | None]]:
    """``(pid, rss_bytes)`` for each live (non-zombie) process still in group
    ``pgid``. Returns ``[]`` when psutil is unavailable — pair with
    :func:`_group_alive` for a coarse existence check."""
    try:
        import psutil
    except ImportError:
        return []
    out: list[tuple[int, int | None]] = []
    for p in psutil.process_iter(["pid"]):
        pid = p.info["pid"]
        try:
            if os.getpgid(pid) != pgid or p.status() == psutil.STATUS_ZOMBIE:
                continue
        except (psutil.Error, ProcessLookupError, PermissionError, OSError):
            continue
        try:
            rss: int | None = p.memory_info().rss
        except psutil.Error:
            rss = None
        out.append((pid, rss))
    return out


# Process groups of every live bridge, registered at spawn and pruned once the
# group is verified dead. Bridges run in their own sessions, so a host-level
# Ctrl-C / SIGTERM no longer reaches them through a shared process group; this
# registry is what the host-side shutdown paths (`Server`'s signal handlers and
# the atexit sweep below) use to take them down.
_live_pgids: set[int] = set()
_live_pgids_lock = threading.Lock()


def _register_pgid(pgid: int) -> None:
    with _live_pgids_lock:
        _live_pgids.add(pgid)


def _unregister_pgid(pgid: int) -> None:
    with _live_pgids_lock:
        _live_pgids.discard(pgid)


def _signal_all_groups(sig: int) -> None:
    """Deliver ``sig`` to every registered bridge process group. Used by the
    host-side shutdown paths now that bridges no longer share the host's group.
    Async-signal-safe enough to call from a signal handler: the registry lock is
    only ever held briefly, and never by the main thread outside a handler."""
    with _live_pgids_lock:
        pgids = list(_live_pgids)
    for pgid in pgids:
        _kill_group(pgid, sig)


def _reap_groups_at_exit() -> None:
    """Last-resort sweep at interpreter exit: take down any bridge group still
    registered — with the bridges in their own sessions, nothing else will.
    SIGTERM first, so a worker thread still draining its pipe sees the
    ``-SIGTERM`` exit as an intentional shutdown (`_SHUTDOWN_RETURNCODES`) and
    does not respawn a doomed replacement mid-exit; SIGKILL whatever remains
    after a short grace."""
    with _live_pgids_lock:
        pgids = list(_live_pgids)
    if not pgids:
        return
    for pgid in pgids:
        _kill_group(pgid, signal.SIGTERM)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and any(_group_alive(p) for p in pgids):
        time.sleep(0.05)
    for pgid in pgids:
        _kill_group(pgid)


atexit.register(_reap_groups_at_exit)


class _BridgeDied(Exception):
    """Raised internally when the bridge's stdio pipe closes mid-exchange (the
    process exited/crashed). Signals the worker to restart the process."""


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


class _RestartMarker:
    """Queue payload asking the worker to restart the bridge *between* jobs.

    Enqueued (like a request) as the payload of a ``(priority, seq, marker)``
    tuple. Because the worker consumes one item at a time, the marker is only
    reached once the current in-flight request has finished — so no request is
    interrupted — and anything still queued behind it is served by the fresh
    process. ``future`` is resolved once the swap completes (or fails)."""

    __slots__ = ("future",)

    def __init__(self, future: "asyncio.Future[bool]") -> None:
        self.future = future


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

# Process exit codes (negative = killed by signal -N) that mark a *deliberate*
# teardown rather than a crash, so the worker must NOT spawn a replacement.
# Bridges run in their own sessions (see `_spawn_proc`), so a host Ctrl-C /
# `kill` no longer reaches them through a shared process group; instead
# `Server` forwards SIGINT/SIGTERM to each bridge group and the atexit sweep
# sends SIGTERM first — and the worker can see the resulting EOF before
# `close()` has run on the main thread. SIGKILL is intentionally excluded: the
# crash-recovery path (and an external `kill -9` of a wedged bridge) should
# still restart.
_SHUTDOWN_RETURNCODES: frozenset[int] = frozenset({-int(signal.SIGINT), -int(signal.SIGTERM)})


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
        build: bool = True,
        restart_on_timeout: bool = True,
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
            build: When true (default) run ``lake build`` for the modules before
                spawning the bridge. Set false when the caller has already built
                them (e.g. a pool building once up front, then spawning several
                bridges concurrently) — concurrent ``lake build`` of the same
                targets races on the shared build tree, so it must be done once.
            restart_on_timeout: When true (default) a timed-out method call
                replaces the bridge process. The bridge has no way to cancel a
                call in flight, so without a restart it keeps elaborating the
                abandoned request and every queued request burns its own timeout
                budget waiting behind it (head-of-line blocking). The restart
                costs the warm import cache but guarantees the next request a
                clean process. Set false to keep the old leave-it-running
                behaviour (late replies are discarded by id either way).
        """
        self.id = id
        self.cluster: int | None = None  # category pinned by the routing policy
        self.lean_modules = self._effective_modules(lean_modules)
        self._cwd = str(cwd) if cwd is not None else str(_DEFAULT_CWD)
        self._ready_timeout = ready_timeout
        self._build = build

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, daemon=True, name="lean-bridge-loop"
        )
        self._thread.start()

        self._proc: asyncio.subprocess.Process | None = None
        self._queue: "asyncio.PriorityQueue[tuple[int, int, _QueuedCall | None]] | None" = None
        self._worker_task: asyncio.Task | None = None
        self._seq = 0  # unique tiebreaker so equal-priority requests stay FIFO
        self._req_id = 0  # monotonic id stamped on each bridge request/response
        self._stderr_task: asyncio.Task | None = None
        self._method_defs: list[MethodDef] = []
        self._method_map: dict[str, MethodDef] = {}
        # Latest module-import snapshot reported by the bridge (updated on every
        # response): the modules the Lean process currently has cached.
        self.cached_modules: list[str] = []
        self._closed = False
        self._restarts = 0  # how many times the process has been respawned
        self._restart_on_timeout = restart_on_timeout
        # Process groups whose kill could not be verified (survivors remained
        # after SIGKILL + grace). Non-empty means memory is leaking; the
        # MemoryManager alarms on it and the atexit sweep retries them.
        self.leaked_pgids: set[int] = set()

        # Block until the process is up and has reported its methods.
        self._dispatch(self._async_setup())

    # ── Construction ─────────────────────────────────────────────

    @staticmethod
    def _effective_modules(lean_modules: list[str]) -> list[str]:
        """The modules a bridge actually imports: the always-on default tools
        (see :data:`_ALWAYS_MODULES`) prepended to the caller's list, with
        duplicates dropped. ``dict.fromkeys`` preserves first-seen order."""
        return list(dict.fromkeys([*_ALWAYS_MODULES, *lean_modules]))

    @classmethod
    def build_modules(cls, lean_modules: list[str], *, cwd: str | Path | None = None) -> None:
        """Compile ``lean_modules`` (plus the always-on defaults) once, blocking.

        Spawning several bridges concurrently must *not* each run ``lake build``
        of the same targets — concurrent builds race on the shared ``.lake/build``
        tree. A pool calls this once up front, then constructs its bridges with
        ``build=False`` so they only spawn (read-only) ``lake exe bridge``
        processes. Raises ``RuntimeError`` if the build fails or any module is
        missing (lake returns nonzero and prints diagnostics)."""
        modules = cls._effective_modules(lean_modules)
        cwd_str = str(cwd) if cwd is not None else str(_DEFAULT_CWD)
        proc = subprocess.run(
            ["lake", "build", *modules], cwd=cwd_str,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"`lake build {' '.join(modules)}` failed "
                f"(exit {proc.returncode}):\n{proc.stdout.decode(errors='replace')}"
            )

    async def _async_setup(self) -> None:
        self._queue = asyncio.PriorityQueue()
        if self._build:
            await self._build_modules()
        await self._spawn_proc()
        # Single consumer of the priority queue; nothing can enqueue until the
        # constructor returns, so starting it here (after the manifest) is safe.
        self._worker_task = self._loop.create_task(self._worker())
        logger.info("Lean bridge (id=%s) ready; methods: %s", self.id, sorted(self._method_map))

    async def _build_modules(self) -> None:
        """Build the requested modules; fail loudly if any error or are missing
        (lake returns nonzero and prints diagnostics). Done once, at startup —
        restarts reuse the already-built artifacts."""
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

    def _bridge_cmd(self) -> list[str]:
        """Command line for the bridge process. Overridable so tests can swap in
        a scriptable fake bridge without a Lean toolchain."""
        return ["lake", "exe", "bridge", "--import", ",".join(self.lean_modules)]

    async def _spawn_proc(self) -> None:
        """Spawn the bridge process, start draining its stderr, and consume the
        startup manifest. Reused for both initial setup and restarts; a fresh
        process starts with nothing cached, so the manifest tables and the
        cached-module snapshot are (re)populated here."""
        cmd = self._bridge_cmd()
        logger.info("Starting Lean bridge (id=%s): %s (cwd=%s)", self.id, " ".join(cmd), self._cwd)
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=self._cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_STREAM_LIMIT,  # responses (esp. `cachedModules`) exceed the 64 KiB default
            # Own session/process group, so teardown can `killpg` the whole tree
            # even after workers reparent (see `_kill_group`). Detaches the
            # bridge from the host's group: host SIGINT/SIGTERM must be
            # forwarded explicitly (`Server` handlers / the atexit sweep).
            start_new_session=True,
        )
        _register_pgid(self._proc.pid)

        # Drain stderr so the process can't block on a full pipe; surface it
        # for debugging (lake build output, Lean errors, etc.).
        self._stderr_task = self._loop.create_task(self._drain_stderr())
        self.cached_modules = []  # a fresh process holds nothing yet

        try:
            manifest = await asyncio.wait_for(
                self._read_manifest(), timeout=self._ready_timeout
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeError(
                f"Lean bridge did not report its methods within {self._ready_timeout}s"
            ) from exc

        self._method_defs.clear()
        self._method_map.clear()
        for entry in manifest:
            td = self._method_def_from_manifest(entry)
            if td is None:
                continue
            if td.name in self._method_map:
                logger.warning("Duplicate method name %r from bridge", td.name)
            self._method_map[td.name] = td
            self._method_defs.append(td)

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
        after each message. Raises :class:`_BridgeDied` if the process has closed
        stdout (exited/crashed)."""
        if self._proc is None or self._proc.stdout is None:
            raise _BridgeDied("bridge process is not running")
        while True:
            raw = await self._proc.stdout.readline()
            if raw == b"":
                raise _BridgeDied(
                    f"closed stdout (exited, returncode={self._proc.returncode})"
                )
            line = raw.decode(errors="replace").strip()
            if line:
                return line

    async def _read_response(self, req_id: int, timeout: float | None) -> dict[str, Any]:
        """Read the bridge's response for request ``req_id``, discarding stale
        replies that belong to earlier requests.

        The bridge echoes each request's ``id`` in its response. A request that
        times out is not cancelled bridge-side — the bridge keeps working on it
        and eventually emits its reply, which then sits in the pipe ahead of the
        next request's reply. Matching on ``id`` lets us skip those late replies
        instead of mistaking one for this request's result (which used to mix up
        responses after any timeout). ``timeout`` bounds the whole wait, not each
        individual read. Raises :class:`_BridgeDied` on EOF (via
        :meth:`_read_line`)."""
        deadline = None if timeout is None else time.perf_counter() + timeout
        while True:
            if deadline is None:
                line = await self._read_line()
            else:
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                line = await asyncio.wait_for(self._read_line(), remaining)
            try:
                resp = json.loads(line)
            except json.JSONDecodeError:
                # The bridge always emits valid JSON per line, so an unparseable
                # line is corruption we can't correlate; drop it and keep waiting
                # for our reply rather than mis-attributing it.
                logger.warning("Discarding unparseable bridge line: %r", line)
                continue
            if not isinstance(resp, dict):
                logger.warning("Discarding non-object bridge response: %r", line)
                continue
            # A missing ``id`` means a bridge that predates id-echoing; accept it
            # rather than loop forever waiting for a match.
            resp_id = resp.get("id", req_id)
            if resp_id == req_id:
                return resp
            logger.warning(
                "Discarding stale Lean response (id=%r) while awaiting id=%r",
                resp_id, req_id,
            )

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
        # Bind to this process's stream so a restart (which swaps `self._proc`)
        # can never make this task read a different process's stderr.
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        stream = proc.stderr
        while True:
            raw = await stream.readline()
            if raw == b"":
                break
            logger.debug("[bridge stderr id=%s] %s", self.id, raw.decode(errors="replace").rstrip())

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
        ``None`` payload is a shutdown sentinel.

        If the bridge dies mid-call, the worker (the only thing that touches
        stdio) restarts it and retries the request once."""
        assert self._queue is not None
        while True:
            _prio, _seq, job = await self._queue.get()
            try:
                if job is None:
                    return
                if isinstance(job, _RestartMarker):
                    await self._graceful_restart(job)
                    continue
                if job.future.done():  # caller already gave up; skip the work
                    continue
                try:
                    result = await self._execute_with_restart(job)
                    if not job.future.done():
                        job.future.set_result(result)
                except Exception as exc:  # defensive: should not normally happen
                    if not job.future.done():
                        job.future.set_exception(exc)
            finally:
                self._queue.task_done()

    async def _execute_with_restart(self, job: _QueuedCall) -> MethodResult:
        """Run one job, restarting the bridge and retrying once if its pipe dies.

        A request that reliably crashes the bridge is retried exactly once (and
        the process is restarted afterward regardless) so a poisoned request can
        never wedge the worker in a crash/restart loop — it just returns an error
        while leaving a healthy process for the next request.

        A death that is really a deliberate shutdown (we are closing, or the
        process was taken down with the group via Ctrl-C / SIGTERM) is never
        retried or restarted — see :meth:`_is_intentional_shutdown`."""
        try:
            return await self._execute_call(
                job.name, job.arguments,
                query_imports=job.query_imports, timeout=job.timeout,
            )
        except _BridgeDied as died:
            if await self._is_intentional_shutdown():
                logger.info(
                    "Lean bridge (id=%s) exited during shutdown (%s); not restarting",
                    self.id, died,
                )
                raise
            method = self._method_for(job.name)
            logger.warning("Lean bridge (id=%s) closed its pipe (%s); restarting", self.id, died)
            try:
                await self._restart()
            except Exception as exc:
                return MethodResult(
                    method=method,
                    content=f"Lean bridge crashed and could not be restarted: {exc}",
                    is_error=True,
                )

        # One retry on the freshly restarted process.
        try:
            return await self._execute_call(
                job.name, job.arguments,
                query_imports=job.query_imports, timeout=job.timeout,
            )
        except _BridgeDied as died:
            if await self._is_intentional_shutdown():
                logger.info(
                    "Lean bridge (id=%s) exited during shutdown (%s); not restarting",
                    self.id, died,
                )
                raise
            logger.error(
                "Lean bridge (id=%s) died again after restart (%s); failing this request",
                self.id, died,
            )
            # Restore a healthy process for subsequent requests; the crash was
            # most likely caused by this particular request.
            try:
                await self._restart()
            except Exception:
                pass
            return MethodResult(
                method=self._method_for(job.name),
                content=f"Lean bridge crashed repeatedly while handling this request: {died}",
                is_error=True,
            )

    async def _is_intentional_shutdown(self) -> bool:
        """Whether a just-detected bridge death is a deliberate teardown rather
        than a crash — in which case the worker must NOT restart the process.

        True when we are already closing (:attr:`_closed`), or when the process
        exited because of SIGINT/SIGTERM. The signal check closes a race: a
        host Ctrl-C/terminate is forwarded to each bridge's process group (by
        ``Server``'s signal handlers, or as SIGTERM by the atexit sweep — the
        bridges run in their own sessions and no longer share the host's
        group), and the worker (on its own background thread, immune to the
        main thread's KeyboardInterrupt) can see the resulting EOF *before*
        :meth:`close` has run — which used to look like a crash and spawn a
        doomed replacement mid-shutdown.
        """
        if self._closed:
            return True
        proc = self._proc
        if proc is None:
            return False
        rc = proc.returncode
        if rc is None:
            # EOF can arrive a beat before the child is reaped; wait briefly so
            # we can read the real exit code (and thus the killing signal).
            try:
                rc = await asyncio.wait_for(proc.wait(), timeout=2)
            except Exception:
                return False
        return rc in _SHUTDOWN_RETURNCODES

    def _method_for(self, name: str) -> MethodDef:
        return self._method_map.get(name) or MethodDef(
            name=name, description="", input_schema=dict(OPEN_SCHEMA)
        )

    async def _restart(self) -> None:
        """Replace a dead (or to-be-killed) bridge process with a fresh one. Runs
        on the worker, so no request touches stdio while it happens."""
        self._restarts += 1
        await self._kill_proc()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            self._stderr_task = None
        await self._spawn_proc()
        logger.info("Lean bridge (id=%s) restarted (restart #%d)", self.id, self._restarts)

    async def _graceful_restart(self, marker: _RestartMarker) -> None:
        """Handle a queued :class:`_RestartMarker`: swap in a fresh process and
        resolve the marker's future. Runs on the worker between jobs, so the
        in-flight request (if any) has already completed and the rest of the
        queue is untouched — it simply gets served by the new process."""
        try:
            await self._restart()
        except Exception as exc:
            logger.error("Lean bridge (id=%s) graceful restart failed: %s", self.id, exc)
            if not marker.future.done():
                marker.future.set_exception(exc)
            return
        if not marker.future.done():
            marker.future.set_result(True)

    async def _async_request_restart(self) -> None:
        """Enqueue a restart marker (on the loop, so the queue stays single-owner)
        and await its completion. Runs at top priority so memory relief happens as
        soon as the current request finishes, ahead of the queued backlog (which
        the fresh, low-memory process then serves)."""
        if self._closed or self._queue is None:
            return
        future: asyncio.Future[bool] = self._loop.create_future()
        self._seq += 1
        self._queue.put_nowait((0, self._seq, _RestartMarker(future)))
        await future

    def restart(self) -> None:
        """Gracefully restart this bridge, preserving queued requests.

        The swap is performed by the worker only after the current in-flight
        request (if any) finishes, so no request is interrupted; requests still
        queued are served by the replacement process. Blocks until the fresh
        process is ready. Intended for external callers (e.g. a memory-budget
        monitor) that want to reclaim a process's accumulated cache memory."""
        if self._closed:
            return
        self._dispatch(self._async_request_restart())

    def memory_rss(self) -> int | None:
        """Resident memory (bytes) of this bridge's whole process tree (the
        ``lake`` launcher plus its ``lean`` children), or ``None`` if it is not
        currently running or can't be read. Safe to call from any thread."""
        proc = self._proc
        if proc is None:
            return None
        return _tree_rss_bytes(proc.pid)

    async def _kill_proc(self) -> None:
        """Ensure the current process tree is dead, reaped, and *verified* dead.

        ``self._proc`` is only the ``lake`` launcher; the heavy Lean workers are
        its descendants — but a downward PPID walk misses any worker whose
        parent already exited (it reparents to init and drops out of the walk),
        which orphaned one ~15 GiB worker per restart in production. The bridge
        runs as its own process-group leader (``start_new_session`` in
        :meth:`_spawn_proc`), so the group kill reaches every worker no matter
        how it was reparented; the descendant walk is kept as a second pass for
        anything that somehow left the group. Killing precedes the launcher's
        ``wait()``, so the pgid (= launcher pid) has not been recycled.
        Afterwards :meth:`_verify_group_dead` confirms the group is empty and
        logs loudly (recording the leak) if it is not — a silent survivor is
        exactly the failure that took a cgroup census to detect."""
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        workers = _descendant_procs(proc.pid) if proc.returncode is None else []
        _kill_group(proc.pid)
        _kill_procs(workers)
        if proc.returncode is None:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:
            pass
        await self._verify_group_dead(proc.pid)

    async def _verify_group_dead(self, pgid: int) -> None:
        """Confirm the bridge's process group is empty, re-killing stragglers
        for up to ~2s. On success the group is dropped from the shutdown
        registry; on failure the survivors are logged with their RSS and the
        pgid is recorded in :attr:`leaked_pgids` so the memory controller can
        alarm and the atexit sweep retries it."""
        deadline = time.monotonic() + 2.0
        while _group_alive(pgid):
            if time.monotonic() > deadline:
                survivors = _group_survivors(pgid)
                self.leaked_pgids.add(pgid)
                logger.error(
                    "Lean bridge (id=%s): process group %d survived SIGKILL; "
                    "leaked %d process(es): %s. Their memory is NOT tracked by "
                    "the pool's accounting.",
                    self.id, pgid, len(survivors),
                    [
                        {"pid": pid, "rss": rss}
                        for pid, rss in survivors
                    ] or "pids unknown (psutil unavailable)",
                )
                return
            _kill_group(pgid)
            await asyncio.sleep(0.05)
        _unregister_pgid(pgid)
        self.leaked_pgids.discard(pgid)

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

        # Stamp a unique id on the request; the bridge echoes it back so we can
        # match the reply to this request and skip stale ones (see
        # `_read_response`). Safe to bump here: `_execute_call` only runs on the
        # single worker, so ids are handed out sequentially.
        self._req_id += 1
        req_id = self._req_id
        payload: dict[str, Any] = {
            "id": req_id,
            "method": name,
            "args": arguments if arguments is not None else {},
        }
        if query_imports:
            payload["queryImports"] = True
        # Request is the JSON object followed by a blank line (the bridge reads
        # until it sees a blank line).
        request = (json.dumps(payload) + "\n\n").encode()

        if self._proc is None or self._proc.stdin is None:
            raise _BridgeDied("bridge process is not running")
        # Time the round trip so the cache policy can reason about how long the
        # Lean process spends per call (import time is split out via the stats
        # the bridge reports below).
        started = time.perf_counter()
        try:
            self._proc.stdin.write(request)
            await self._proc.stdin.drain()
            resp = await self._read_response(req_id, timeout)
        except asyncio.TimeoutError:
            logger.warning("Lean method %r timed out after %.1fs", name, timeout or 0.0)
            if self._restart_on_timeout:
                # The bridge cannot cancel a call in flight: left alone it keeps
                # elaborating the abandoned request, and — one request at a time
                # — every queued call burns its own timeout budget waiting
                # behind it. Replace the process so the next request gets a
                # clean slate (costs the warm import cache). The timed-out
                # request itself is never retried.
                try:
                    await self._restart()
                except Exception as exc:
                    logger.error(
                        "Lean bridge (id=%s) restart after timeout failed: %s "
                        "(next request will retry the restart)",
                        self.id, exc,
                    )
            # With restart_on_timeout=False the process is left running; its
            # late reply is discarded by `_read_response` on the next call,
            # matched out by its (now stale) id.
            return MethodResult(
                method=method,
                content=f"Method {name!r} timed out after {timeout}s",
                is_error=True,
            )
        except _BridgeDied:
            raise  # closed stdout (EOF) from `_read_line`; let the worker restart
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            # Writing to a dead process's stdin -> the pipe is gone; treat as death.
            raise _BridgeDied(f"stdio pipe broke: {exc}") from exc
        total_time = time.perf_counter() - started

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
        """Stop the bridge process and the background loop. Idempotent."""
        if self._closed:
            return
        self._closed = True
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
        if proc is not None and proc.returncode is None:
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
                    pass
        # Whether the launcher exited gracefully, had to be terminated, or was
        # already gone when close() ran (the case that used to reap nothing at
        # all, orphaning its reparented workers), the group sweep in
        # `_kill_proc` reaps every worker and verifies the group is empty.
        await self._kill_proc()
        if self._stderr_task is not None:
            self._stderr_task.cancel()

    def __enter__(self) -> "LeanInterface":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()
