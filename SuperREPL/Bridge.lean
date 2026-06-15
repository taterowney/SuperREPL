module
import Lean
public import Cli.Basic
public import Qq.Macro

public import SuperREPL.Environment
public import SuperREPL.BridgeInitializer
public import SuperREPL.Checker


public section

open Lean Meta Elab Command Qq

namespace SuperREPL.Bridge



/-- Read lines from stdin until a blank line is encountered.  Returns the
empty string at EOF. -/
partial def readRequest : IO String := do
  let line ← (← IO.getStdin).getLine
  if line.trimAscii.isEmpty then
    return line
  else
    return line.trimAsciiEnd.toString ++ (← readRequest)

/-- Print a JSON value (compressed) followed by a blank line, then flush
both stdout and stderr so the response is visible to a piped reader
immediately. -/
def emit (j : Json) : IO Unit := do
  let out ← IO.getStdout
  let err ← IO.getStderr
  out.putStrLn j.compress
  out.putStrLn ""
  out.flush
  err.flush

/-- Payload when requested command succeeds -/
def successResult (res : Json) : IO Json := do
  let (time, misses) := (← popLastImportStats).getD (0, 0)
  return Json.mkObj [("result", "success"), ("value", res), ("cachedModules", toJson <| ← getCachedModules), ("importsTimeMs", toJson time), ("importCacheMisses", toJson misses)]

/-- Payload when requested command fails -/
def errorResult (msg : String) : IO Json := do
  let (time, misses) := (← popLastImportStats).getD (0, 0)
  return Json.mkObj [("result", "error"), ("value", msg), ("cachedModules", toJson <| ← getCachedModules), ("importsTimeMs", toJson time), ("importCacheMisses", toJson misses)]


unsafe def handle (req : String) : CommandElabM Json := do
  try
    let cmdJson? := Json.parse req
    match cmdJson? with
    | .error e => errorResult s!"Invalid JSON: {e}"
    | .ok cmdJson =>
      let some methodJson := cmdJson.getObjVal? "method" |>.toOption | errorResult s!"Malformed payload: missing \"method\" field"
      let some method := methodJson.getStr? |>.toOption | errorResult s!"Malformed payload: \"method\" field must be string"
      let some argsJson := cmdJson.getObjVal? "args" |>.toOption | errorResult s!"Malformed payload: missing \"args\" field"

      let info ← liftCoreM <| findExposedMethod! method.toName

      if (cmdJson.getObjVal? "queryImports" |>.toOption) == some true then
        match ← liftTermElabM <| info.getImportsFunction with
        | some importsFn =>
          let res ← importsFn argsJson
          successResult res
        | none => errorResult s!"Method {method} does not have a registered function to query imports."

      else -- Assume it's just a normal call to the method
        let fn ← liftTermElabM <| info.getFunction
        let res ← fn argsJson
        successResult res

  catch e =>
    errorResult s!"{← e.toMessageData.format}"


def emitReady : CoreM Unit := do
  emit <| Json.arr <| (← getExposedMethodsInfo).map toJson


/-- Read-handle-write loop in `CommandElabM`. -/
unsafe def loop : CommandElabM Unit := do
  let req ← (readRequest : IO String)
  if req.trimAscii.isEmpty then return ()
  let resp ← handle req
  (emit resp : IO Unit)
  loop



/-- Resolve the import set: any `--import` value fully replaces the defaults. -/
def importsFromFlag (mods : Array String) : Array Import :=
  mods.map (fun m => { module := m.toName, importAll := true })


end SuperREPL.Bridge

open SuperREPL.Bridge Cli


/-- Cli handler: spin up a fresh environment and run the REPL loop. -/
unsafe def runRepl (p : Cli.Parsed) : IO UInt32 := do
  enableInitializersExecution
  initSearchPath (← Lean.findSysroot)
  let mods := (p.flag? "import").map (·.as! (Array String)) |>.getD #[]
  let imports := importsFromFlag mods
  discard <| withFreshCommandElabM imports do
    liftCoreM emitReady
    loop

  (← IO.getStdout).flush
  return 0

/-- The `bridge` Cli command. -/
unsafe def replCmd : Cli.Cmd := `[Cli|
  «bridge» VIA runRepl; ["0.1.0"]
  "Persistent JSON-on-stdio REPL exposing every `@[expose_python]` method over a \
   single `CommandElabM` session.\n\n\
   Framing: messages are newline-delimited JSON, each terminated by a blank line. \
   On startup the bridge emits one handshake message — a JSON array describing the \
   available methods, each \
   `{name, description, input_schema, output, uses_imports, internal}` \
   — so clients can discover the API.\n\n\
   Request (one object, then a blank line):\n\
   {\"method\": <name>, \"args\": {<argName>: <value>, ...}}\n\
   `args` keys are the method's parameter names; values are decoded via each type's \
   `FromJson`. Add \"queryImports\": true to instead run the method's imports \
   function (only for methods where `uses_imports` is true), returning the modules \
   it needs.\n\n\
   Response (one object, then a blank line); every response also reports the \
   process's import state:\n\
   {\"result\": \"success\", \"value\": <json>,  -- method's return value via `ToJson`\n\
   \"cachedModules\": [<module>, ...],  -- modules the process now has cached\n\
   \"importsTimeMs\": <number>,  -- ms spent importing for this request\n\
   \"importCacheMisses\": <int>}  -- modules that missed the import cache\n\
   {\"result\": \"error\", \"value\": <message>, ...}  -- same fields, on any failure\n\n\
   The loop runs until stdin closes or a blank request is read."

  FLAGS:
    "import" : Array String;
      "Comma-separated list of modules to import into the fresh environment."
]

/-- Entry point for `lake exe bridge`. -/
unsafe def main (args : List String) : IO UInt32 :=
  replCmd.validate args

end
