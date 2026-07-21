"""End-to-end exercises for the SuperREPL stack.

Run from the repo root::

    python -m super_repl.tests            # check the problems through the server
    python -m super_repl.tests --crash    # also exercise bridge crash-recovery

The default run spins up a :class:`~main.Server` (hosted on a background thread),
submits the problems from :func:`lean_problems` through a :class:`~service.Client`
over HTTP, logs the per-problem verdict, and logs the **total time taken**.

The exposed Lean checker is ``checkLean`` in ``SuperREPL.CheckLean``; it takes a
single ``leanCode`` string (imports are parsed from the source itself), so a
"problem" is just a complete Lean snippet.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time

from .basic import Request
from .main import Server
from .service import Client, SubmitResult

logger = logging.getLogger("super_repl.tests")

# Lean module exposing `checkLean` (plus the always-on SuperREPL.DefaultTools).
CHECK_MODULE = "SuperREPL.CheckLean"
CHECK_METHOD = "checkLean"


# ──────────────────────────────────────────────────────────────────────────
# Problems supplied to the system — edit here
# ──────────────────────────────────────────────────────────────────────────


def lean_problems() -> list[tuple[str, str]]:
    """The Lean problems to check, as ``(label, leanCode)`` pairs.

    Edit this list to change what the tests submit. Each ``leanCode`` is a
    complete snippet (it may start with ``import`` lines); ``checkLean`` parses
    the imports out of the source itself.
    """
    return [
        ("valid-def",      "def x := 1"),
        ("valid-typed",    "def y : Nat := 2"),
        ("sorry",          "theorem t : 1 = 1 := sorry"),
        ("type-error",     'def b : Nat := "not a nat"'),
        ("unknown-ident",  "def c := someUndefinedIdentifier"),
        ("with-import",    "import Init.Data.List\nimport Mathlib\ndef l := [1, 2, 3]"),
    ] * 200


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────


def _imports_of(lean_code: str) -> frozenset[str]:
    """Modules named in the snippet's ``import`` header — used only for routing/
    clustering on the Python side (the bridge re-derives the real transitive set)."""
    mods: set[str] = set()
    for line in lean_code.splitlines():
        line = line.strip()
        if line.startswith("import "):
            mods.add(line[len("import "):].strip())
        elif line and not line.startswith(("--", "/-")):
            break  # imports must lead the file; stop at the first real line
    return frozenset(mods)


def _status(res: SubmitResult) -> str:
    """Pull the ``status`` field out of a ``checkLean`` result for display."""
    if isinstance(res.content, str):
        try:
            return json.loads(res.content).get("status", "?")
        except (json.JSONDecodeError, ValueError):
            return res.content[:40]
    return "?"


def _problem_requests() -> list[tuple[str, Request]]:
    return [
        (label, Request(CHECK_METHOD, {"leanCode": code}, imports=_imports_of(code)))
        for label, code in lean_problems()
    ]


# ──────────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────────


async def check_problems(server: Server) -> None:
    """Submit every problem through the server's HTTP host and log verdicts."""
    server.serve(port=0, background=True)        # OS-assigned port; non-blocking
    logger.info("server listening on %s (%d processes)", server.url, len(server.processes))

    labelled = _problem_requests()
    async with Client(server.url) as client:
        logger.info("methods: %s", [m["name"] for m in await client.get_methods()])

        started = time.perf_counter()
        results = await asyncio.gather(
            *(client.submit(req) for _, req in labelled)
        )
        elapsed = time.perf_counter() - started

        for (label, _), res in zip(labelled, results):
            logger.info("  %-14s is_error=%-5s status=%-20s cluster=%s",
                        label, res.is_error, _status(res), res.cluster)

        server.reconfigure()
        logger.info("assignment: %s", server.assignment)
        logger.info("per-process cached modules: %s",
                    {p.id: p.mem for p in server.processes})

    n = len(labelled)
    logger.info("checked %d problem(s) in %.3fs (%.3fs/problem)",
                n, elapsed, elapsed / max(n, 1))


def _descendants(pid: int) -> list[int]:
    try:
        kids = [int(x) for x in subprocess.check_output(["pgrep", "-P", str(pid)]).split()]
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    out = list(kids)
    for k in kids:
        out += _descendants(k)
    return out


async def crash_recovery() -> None:
    """Kill a bridge mid-life and confirm the next call auto-restarts it."""
    from .lean_interface import LeanInterface

    label, code = lean_problems()[0]
    iface = LeanInterface([CHECK_MODULE], id=0)
    try:
        r1 = await iface.call_method(CHECK_METHOD, {"leanCode": code})
        assert not r1.is_error, r1.content
        logger.info("pre-crash check ok (restarts=%d)", iface._restarts)

        tree = [iface._proc.pid] + _descendants(iface._proc.pid)
        logger.warning("SIGKILLing bridge process tree: %s", tree)
        for p in reversed(tree):
            try:
                os.kill(p, signal.SIGKILL)
            except ProcessLookupError:
                pass

        r2 = await iface.call_method(CHECK_METHOD, {"leanCode": code})
        assert not r2.is_error, r2.content
        assert iface._restarts >= 1, "expected at least one restart"
        logger.info("post-crash check ok (restarts=%d) — recovery works", iface._restarts)
    finally:
        iface.close()


# ──────────────────────────────────────────────────────────────────────────
# Unit tests (fast, no Lean toolchain needed): `python -m super_repl.tests --unit`
#
# These encode the process-orphaning and timeout-cascade failure modes from the
# 2026-07 production OOM (see the fix in `lean_interface._kill_proc` /
# `_execute_call`): teardown must reap workers that were reparented away from
# the launcher, must reap them even when the launcher died first, and a
# timed-out call must not head-of-line-block the next request.
# ──────────────────────────────────────────────────────────────────────────


# A stand-in for `lake exe bridge` speaking the same stdio protocol: manifest on
# startup, then one JSON reply (echoing the request id) per blank-line-terminated
# request. `slow` sleeps for `args.seconds`, simulating a runaway elaboration.
_FAKE_BRIDGE = r'''
import json, sys, time
def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n\n")
    sys.stdout.flush()
emit([
    {"name": "echo", "description": "", "input_schema": {}, "output": "String"},
    {"name": "slow", "description": "", "input_schema": {}, "output": "String"},
])
buf = []
for line in sys.stdin:
    stripped = line.strip()
    if stripped:
        buf.append(stripped)
        continue
    if not buf:
        break  # a lone blank line is the graceful-shutdown nudge
    req = json.loads("".join(buf))
    buf = []
    if req.get("method") == "slow":
        time.sleep(float(req.get("args", {}).get("seconds", 60)))
    emit({"id": req.get("id"), "result": "success", "value": "ok",
          "cachedModules": [], "importsTimeMs": 0, "importCacheMisses": 0})
'''


def _make_bare_iface() -> "object":
    """A LeanInterface shell with just the state `_kill_proc` touches, so the
    reap path can be exercised on a synthetic process tree without a bridge."""
    from .lean_interface import LeanInterface

    iface = LeanInterface.__new__(LeanInterface)
    iface.id = 999
    iface.leaked_pgids = set()
    return iface


async def _spawn_orphan_tree() -> "asyncio.subprocess.Process":
    """A launcher whose grandchild is reparented to init (its parent exits
    immediately) while staying in the launcher's process group — the tree shape
    that escaped the old PPID-walk teardown and leaked ~15 GiB per restart."""
    from .lean_interface import _register_pgid

    proc = await asyncio.create_subprocess_exec(
        "sh", "-c", 'sh -c "sleep 300 & " ; exec sleep 300',
        start_new_session=True,
    )
    _register_pgid(proc.pid)
    await asyncio.sleep(0.3)  # let the middle shell spawn the orphan and exit
    return proc


def _assert_group_dead(pgid: int) -> None:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return
    os.killpg(pgid, signal.SIGKILL)  # don't leave the fixture behind
    raise AssertionError(f"process group {pgid} survived teardown")


async def reap_orphaned_grandchild() -> None:
    """`_kill_proc` must take down a worker the descendant walk cannot see."""
    iface = _make_bare_iface()
    iface._proc = await _spawn_orphan_tree()
    pgid = iface._proc.pid
    await iface._kill_proc()
    _assert_group_dead(pgid)
    assert not iface.leaked_pgids, iface.leaked_pgids
    logger.info("reap-orphaned-grandchild ok")


async def reap_after_launcher_already_dead() -> None:
    """close()-style teardown when the launcher exited first — the case that
    previously reaped nothing at all (`workers = []` when returncode is set)."""
    iface = _make_bare_iface()
    proc = await _spawn_orphan_tree()
    os.kill(proc.pid, signal.SIGKILL)
    await proc.wait()  # launcher reaped; its workers live on in the group
    iface._proc = proc
    await iface._kill_proc()
    _assert_group_dead(proc.pid)
    assert not iface.leaked_pgids, iface.leaked_pgids
    logger.info("reap-after-launcher-dead ok")


async def timeout_restarts_and_unblocks() -> None:
    """A timed-out call must not consume the next request's timeout budget:
    the bridge is restarted, so a follow-up trivial call succeeds promptly
    instead of queueing behind the abandoned 60s elaboration."""
    from .lean_interface import LeanInterface

    class _FakeBridgeInterface(LeanInterface):
        def _bridge_cmd(self) -> list[str]:
            return [sys.executable, "-u", "-c", _FAKE_BRIDGE]

    iface = _FakeBridgeInterface([], id=998, build=False)
    try:
        r1 = await iface.call_method("slow", {"seconds": 60}, timeout=1.0)
        assert r1.is_error, r1.content
        started = time.perf_counter()
        r2 = await iface.call_method("echo", {}, timeout=10.0)
        elapsed = time.perf_counter() - started
        assert not r2.is_error, f"echo failed after a timeout: {r2.content}"
        assert iface._restarts == 1, f"expected 1 restart, saw {iface._restarts}"
        assert elapsed < 10, f"echo blocked {elapsed:.1f}s behind the abandoned call"
    finally:
        iface.close()
    logger.info("timeout-restarts-and-unblocks ok (follow-up served in %.2fs)", elapsed)


async def unit_tests() -> None:
    await reap_orphaned_grandchild()
    await reap_after_launcher_already_dead()
    await timeout_restarts_and_unblocks()
    logger.info("all unit tests passed")


# ──────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────


# Third-party libraries that chatter at INFO (per-request access/HTTP logs).
# Kept at WARNING unless debugging so the test output stays readable.
_NOISY_LOGGERS = ("aiohttp", "aiohttp.access", "aiohttp.server", "httpx", "httpcore")


def _env_debug() -> bool:
    return os.environ.get("DEBUG", "").strip().lower() in ("1", "true", "yes", "on")


def _configure_logging(debug: bool) -> None:
    """Set up logging, silencing aiohttp/httpx unless ``debug`` is set."""
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.DEBUG if debug else logging.WARNING)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Exercise the SuperREPL stack.")
    parser.add_argument("--processes", type=int, default=2, help="bridges in the pool")
    parser.add_argument("--budget", type=int, default=0, help="max memory per bridge (GiB, 0=unlimited)")
    parser.add_argument("--crash", action="store_true",
                        help="also run the bridge crash-recovery test")
    parser.add_argument("--unit", action="store_true",
                        help="run only the fast process-reaping/timeout unit "
                             "tests (no Lean toolchain needed)")
    parser.add_argument("--debug", action="store_true",
                        help="verbose logging, incl. aiohttp/httpx (or set DEBUG=1)")
    args = parser.parse_args(argv)

    _configure_logging(args.debug or _env_debug())

    if args.unit:
        asyncio.run(unit_tests())
        return

    total_start = time.perf_counter()
    server = Server(num_processes=args.processes, lean_modules=[CHECK_MODULE], memory_budget=args.budget * 1024**3)
    try:
        asyncio.run(check_problems(server))
        if args.crash:
            asyncio.run(crash_recovery())
    finally:
        server.close()
        logger.info("total time taken: %.3fs", time.perf_counter() - total_start)


if __name__ == "__main__":
    main()
