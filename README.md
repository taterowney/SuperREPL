# SuperREPL

SuperREPL is intended to provide fast, batched Lean 4 utilites for checking large numbers of machine-generated proofs. 

## The goals
- Is as fast and parallel as possible
- Functions as a client/server setup so that it is compatible with distributed computing
- Functions fully online, and the server can sort out how best to route requests to various Lean processes based on heuristics
- Manages its memory usage and that of its Lean processes 

## Installation

SuperREPL has a Lean side (the `bridge` executable that runs proofs) and a Python
side (`super_repl`, the pool/server/client that drives it).

1. **Build the Lean bridge.** From the repo root (where `lakefile.toml` lives):

   ```bash
   lake exe cache get   # optional, pulls prebuilt deps if available
   lake build bridge
   ```

   This produces the `bridge` executable the Python side spawns via
   `lake exe bridge`. The pinned toolchain is in `lean-toolchain`.

2. **Install the Python dependencies.**

   ```bash
   pip install -m requirements.txt
   ```

Run everything from the repo root so the Python side can find `lakefile.toml`
(it walks up from `super_repl/` to locate it) and so `python -m super_repl.*`
resolves.

## Starting a server

A `Server` owns a fixed pool of persistent `lake exe bridge` processes and
routes requests onto them. The simplest way to stand one up is the module CLI:

```bash
python -m super_repl.service --processes 4 \
    --modules SuperREPL.CheckLean \
    --host 127.0.0.1 --port 8765
```

- `--processes` — number of Lean bridges in the pool.
- `--modules` — comma-separated Lean modules imported into every bridge.
- `--host` / `--port` — where to serve (`--port 0` lets the OS pick one).

This blocks until interrupted (Ctrl-C), then tears down the pool.

To host a server *in the same process* that talks to it, construct one directly
and serve it on a background thread:

```python
from super_repl.main import Server

server = Server(num_processes=4, lean_modules=["SuperREPL.CheckLean"])
server.serve(port=0, background=True)   # non-blocking; OS-assigned port
print(server.url)                       # e.g. http://127.0.0.1:54123
...
server.close()                          # stop serving + tear down the pool
```

`Server` is also a context manager, so `with Server(...) as server:` will close
the pool (and stop serving) on exit.

## Using the Client

`Client` (in `super_repl.service`) submits requests to a running server over
HTTP. It is **async-first** — the server and bridges are async — and reuses a
single connection with no read timeout, so long Lean calls are fine; bound an
individual call with the per-call `timeout` instead. Use it as an async context
manager:

```python
import asyncio
from super_repl.service import Client

async def main():
    async with Client("http://127.0.0.1:8765") as client:
        # What methods does the server expose? (internal methods are hidden)
        methods = await client.get_methods()

        # Check a Lean snippet. `imports` is used for routing/clustering;
        # the bridge re-derives the real transitive import set itself.
        result = await client.call(
            "checkLean",
            {"leanCode": "def x : Nat := 1"},
            imports=["Init.Data.List"],
            timeout=30.0,
        )
        print(result.is_error, result.content, result.cluster)

asyncio.run(main())
```

Key methods:

- `await client.call(method, params, *, imports=(), timeout=None, priority=False)`
  — build and submit a request in one step; returns a `SubmitResult`.
- `await client.submit(request, *, timeout=None, priority=False)` — submit a
  pre-built `super_repl.basic.Request`.
- `await client.get_methods()` — list the public method descriptors.
- `await client.health()` — liveness check + process count.
- `await client.reconfigure()` — ask the server to recompute its
  process↔category routing from recent demand.
- `await client.aclose()` — close the underlying HTTP client (handled for you by
  the `async with` form).

A `SubmitResult` carries `content` (the bridge's result), `is_error`, the
`method` name, and the `cluster` the routing policy assigned.

For a runnable end-to-end example (server + client over HTTP), see
`super_repl/tests.py` (`python -m super_repl.tests`).




TODO:
- [X] Lean bridge + "uses imports" flag
- [ ] DSLean for type translation?
- [ ] Custom monads can be used by exposed functions so we don't have to deal with refs?
- [X] Imports processing
- [X] Modularize Python side
- [ ] Benchmark + improve caching policy
- [ ] Send import diff instead of every time
- [ ] Edge cases in Lean-side environment caching; what if the next input extends the current command instead of making a new one?
- [ ] Dynlib loading to support arbitrary unsafe code (Lean.loadDynlib)