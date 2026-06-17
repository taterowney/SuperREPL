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

2. **Install the Python package.** 

   ```bash
   pip install "super-repl @ git+https://github.com/taterowney/SuperREPL.git"
   # pin a ref:  ...SuperREPL.git@v0.1
   ```

   Installing the package adds a `super-repl-server` console script (equivalent
   to `python -m super_repl.service`).

> **Note:** installing the Python package does **not** build the Lean side. The
> server spawns `lake exe bridge` and `super_repl` locates the project by walking
> up from its own directory to a `lakefile.toml`, so a server must be run from
> within a built Lean project (step 1) — a bare `pip install` from GitHub gives
> you the client/server code but not a runnable bridge. A pure `Client` talking
> to a remote server needs only the Python install.

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

## Exposing new methods with `@[expose_python]`

The methods a `Client` can call are not defined in Python — they are Lean
functions tagged with the `@[expose_python]` attribute (defined in
`SuperREPL/BridgeInitializer.lean`). The bridge discovers every such method in
its imported modules and advertises it over `/methods`; the Python side does no
registration of its own. To add a new callable, write a Lean `def`, tag it, and
make sure its module is imported into the bridge.

```lean
import SuperREPL.BridgeInitializer   -- must be `meta import`ed when built as the bridge exe

/-- Doubles a natural number. -/   -- the docstring becomes the method's description
@[expose_python]
def double (n : Nat) : Nat := 2 * n
```

Rules the attribute enforces:

- The declaration must be a constant with an executable value — a function, or a
  `def` taking no arguments.
- **It must have a docstring.** That docstring is surfaced as the method's
  `description` in `/methods` (and to anything inspecting the API).
- Every argument and return type must have `FromJson`/`ToJson` instances. The
  **argument names** become the JSON keys callers pass in `params`. The return
  type may be pure or wrapped in `CommandElabM`.

Once the module is imported into the bridge — either by passing it via
`--modules` / `lean_modules=[...]`, or because it is one of the always-on
baseline modules (`SuperREPL.DefaultTools`) — the method shows up in
`client.get_methods()` and is callable by name:

```python
result = await client.call("double", {"n": 21})   # -> content == 42
```

### `internal` methods

Prefix the attribute with `internal` to register a method that the bridge can
use but that is hidden from clients — it is omitted from `client.get_methods()`
and submitting it returns 404, exactly as if it did not exist:

```lean
@[internal expose_python]
def freeCachedModules (modulesToFree : Array Name) : CommandElabM Unit := ...
```

### Declaring imports for caching (`uses_imports`)

A method that elaborates user code against a set of imported modules can pass an
optional function as the attribute argument. It must take the **same arguments**
as the method and return `CommandElabM (Array Name)` — the modules the call will
import. Supplying it sets the method's `uses_imports` flag and lets the
orchestrator cache/route on those imports (the caching path assumes
`importModulesCached` / `collectDependenciesCached` from `SuperREPL.Environment`):

```lean
@[expose_python getImportsFromSrc]   -- getImportsFromSrc : String → CommandElabM (Array Name)
unsafe def checkLean (leanCode : String) : CommandElabM FullCheckResult := ...
```

After adding or changing an exposed method, rebuild the bridge (`lake build
bridge`) and restart the server so the pool picks up the new module.




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