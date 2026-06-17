module

import Lean
-- import SafeVerify
-- public import AutoSolve
public import TrainingData.Syntax
public meta import SuperREPL.BridgeInitializer
public import TrainingData.Environment.CacheImports
public import TrainingData.Utils.Dependencies
public import SuperREPL.Environment

public section

open Lean Meta Elab Expr Term Command


initialize freshEnvCache : IO.Ref (Std.HashMap String Environment) ← IO.mkRef {}

private def freshEnvCacheKey (imports : Array Import) : String :=
  String.intercalate "," (imports.map (fun i => s!"{i.module}{i.importAll}{i.isExported}{i.isMeta}")).toList

def getEnvFromCache (imports : Array Import) : IO (Option Environment) := do
  let key := freshEnvCacheKey imports
  return (← freshEnvCache.get).get? key

def addEnvToCache (imports : Array Import) (env : Environment) : IO Unit := do
  let key := freshEnvCacheKey imports
  freshEnvCache.modify (fun m => m.insert key env)

/-- Build a fresh `Environment` containing the given `imports`. Cached. -/
def freshEnvironment (imports : Array Import) : IO Environment := do
  match ← getEnvFromCache imports with
  | some env => return env
  | none =>
    -- Caches existing imported modules to memory and saves a bunch of startup time
    let env ← importModulesCached imports {} (loadExts := true) (level := OLeanLevel.exported)
    addEnvToCache imports env
    return env


/-- Run a `CommandElabM` action against a fresh environment built from `imports`.
`source` is the text being elaborated; its `FileMap` is installed in the
elaboration context so that message and `sorry` positions resolve to real
line/column locations (an empty `source` keeps the degenerate default map). -/
unsafe def withFreshCommandElabM (imports : Array Import) (prog : CommandElabM α) (source : String := "") : IO (Environment × α) := do
  enableInitializersExecution
  let env ← freshEnvironment imports
  let state := { (Command.mkState env {} {}) with infoState.enabled := true }
  match ← prog.run { fileName := "<input>", fileMap := FileMap.ofString source, snap? := none, cancelTk? := none } |>.run state |>.toIO' with
  | .ok (result, newState) => return (newState.env, result)
  | .error err => throw <| IO.userError s!"Error elaborating commands: {← err.toMessageData.toString}"

unsafe def withCommandElabMFromEnv (env : Environment) (prog : CommandElabM α) (source : String := "") : IO (Environment × α) := do
  enableInitializersExecution
  let state := { (Command.mkState env {} {}) with infoState.enabled := true }
  match ← prog.run { fileName := "<input>", fileMap := FileMap.ofString source, snap? := none, cancelTk? := none } |>.run state |>.toIO' with
  | .ok (result, newState) => return (newState.env, result)
  | .error err => throw <| IO.userError s!"Error elaborating commands: {← err.toMessageData.toString}"

structure Span where
  startLine : Nat
  startColumn : Nat
  endLine : Nat
  endColumn : Nat


/-- Build a `Span` from a start/end `Lean.Position` pair. -/
def mkSpanFromPos (pos endPos : Position) : Span :=
  { startLine := pos.line, startColumn := pos.column,
    endLine := endPos.line, endColumn := endPos.column }


/-- Outcome of `checkCommands`. -/
inductive CheckResult
  | typechecks -- Everything typechecks; the goal problem hasn't been solved yet
  | problemComplete -- Have proved something that matches the goal problem
  | error (err : String) (loc : Span) -- Something went wrong, e.g. a parsing or typechecking error
  | disallowedAxiom (axiomName : String) (loc : Span) -- A new declaration depends on an axiom that isn't allowed
  | typechecksWithSorry (sorry_loc : Span) -- Encountered a `sorry` at the given declaration

instance : ToJson Span where
  toJson span := Json.mkObj [("startLine", span.startLine), ("startColumn", span.startColumn), ("endLine", span.endLine), ("endColumn", span.endColumn)]

/-- A single elaboration error, with the source span it was reported at (if the
message carried a position). Mirrors one `LeanError` on the Python side. -/
structure ErrorInfo where
  message : String
  span : Option Span
  deriving ToJson

/-- A single `sorry` occurrence: the pretty-printed goal state at the `sorry`
plus the source span of the `sorry` token. Mirrors one `LeanSorry` on the
Python side. There is no persistent proof-state handle (the bridge builds a
fresh environment per call), so the Python wrapper synthesizes a placeholder
`proof_state`. -/
structure SorryInfo where
  goal : String
  span : Option Span
  deriving ToJson


/-- A single message emitted while elaborating a block of Lean code: its
severity (`"error"` / `"warning"` / `"info"`), the rendered text, and the source
span it was reported at (if it carried a position). -/
structure MessageInfo where
  severity : String
  data : String
  span : Option Span
  deriving ToJson

def Lean.Message.toMessageInfo (msg : Message) : IO MessageInfo := do
  let sev := match msg.severity with
    | MessageSeverity.error => "error"
    | MessageSeverity.warning => "warning"
    | MessageSeverity.information => "info"
  let text ← msg.data.toString
  return {
    severity := sev, data := text,
    span := some (mkSpanFromPos msg.pos (msg.endPos.getD msg.pos))
  }


/-- The full result of `checkLean`, carrying everything needed to reconstruct a
`LeanCheckResult` (`ok`/`errors`/`sorries`) plus the axiom-soundness verdict
(`axiomsOk`/`disallowedAxioms`, feeding `check_axioms`) and the `problemComplete`
flag, in a single round-trip.

`ok` follows the REPL `check` convention: true iff there are no errors and no
`sorry`s. A declaration that uses a disallowed axiom (e.g. a literal `axiom`)
still elaborates without an error or a `sorry`, so it does NOT make `ok` false —
it is surfaced through `axiomsOk`/`disallowedAxioms` instead, matching the
REPL's separation of `check` from `check_axioms`. -/
structure FullCheckResult where
  ok : Bool
  status : String
  errors : Array ErrorInfo
  sorries : Array SorryInfo
  axiomsOk : Bool
  disallowedAxioms : Array String
  decls : Array String
  messages : Array MessageInfo
  deriving ToJson


instance : ToJson CheckResult where
  toJson
    | CheckResult.typechecks => Json.mkObj [("status", "typechecks")]
    | CheckResult.problemComplete => Json.mkObj [("status", "problemComplete")]
    | CheckResult.error err loc => Json.mkObj [("status", "error"), ("message", err), ("location", toJson loc)]
    | CheckResult.disallowedAxiom axiomName loc => Json.mkObj [("status", "disallowedAxiom"), ("axiom", axiomName), ("location", toJson loc)]
    | CheckResult.typechecksWithSorry loc => Json.mkObj [("status", "typechecksWithSorry"), ("location", toJson loc)]

instance : ToString CheckResult where
  toString
    | CheckResult.typechecks => "typechecks"
    | CheckResult.problemComplete => "problemComplete"
    | CheckResult.error err loc => s!"error: {err} (line {loc.startLine}, column {loc.startColumn})"
    | CheckResult.disallowedAxiom axiomName loc => s!"disallowedAxiom: {axiomName} (line {loc.startLine}, column {loc.startColumn})"
    | CheckResult.typechecksWithSorry loc => s!"typechecksWithSorry: (line {loc.startLine}, column {loc.startColumn})"

/-- Names of constants present in `newEnv` but not in `oldEnv`. -/
def getNewConstants (oldEnv newEnv : Environment) : List (Name × ConstantInfo) :=
  newEnv.constants.map₂.foldl (init := []) (fun acc constName ci =>
    if oldEnv.contains constName then acc else (constName, ci) :: acc)
  -- let oldConsts := oldEnv.constants.toList.map (fun (n, _) => n)
  -- let newConsts := newEnv.constants.toList.map (fun (n, _) => n)
  -- newConsts.filter (fun n => !oldConsts.contains n)



def allowedAxioms : Array Name := #[``propext, ``Quot.sound, ``Classical.choice]
def sorryAxiom := ``sorryAx



abbrev CollectM := ReaderT Environment $ StateM Unit

def runM {α : Type} (env : Environment) (x : CollectM α) : α :=
  x.run env |>.run' ()

instance : Monad CollectM where
  pure a := ReaderT.pure a

instance : MonadEnv CollectM where
  getEnv := read
  modifyEnv _ := do pure () -- Don't actually need to modify the environment since its read-only, but this is required to implement MonadEnv for collectAxioms


/-- Get ConstantInfo's axioms -/
def getAxioms (info : ConstantInfo) (env : Environment) : Array Name := Id.run do
  return runM env (Lean.collectAxioms info.name : CollectM _ )


/-- Run `prog` and re-throw any error message it accumulated as an exception. -/
def withExplicitErrors (prog : CommandElabM α) : CommandElabM α := do
  let out ← prog
  for msg in (← get).messages.toList do
    if msg.severity == MessageSeverity.error then
      throwError (s!"Error elaborating commands: {← msg.toString}")
  return out


/-- Run `prog`, restoring the prior `Command.State` if it raises an error. -/
def withRevertOnError (prog : CommandElabM α) : CommandElabM α := do
  let stateBefore ← get
  try
    withExplicitErrors prog
  catch err =>
    set stateBefore
    throw err



/- Minimal `InfoTree` `sorry`-scanning, inlined from the `repl` package's
`REPL.Lean.InfoTree` (which is not a module-system module and so cannot be
imported here). Finds every tactic- and term-mode `sorry`, each with its
`ContextInfo`, the goal it closed (or its expected type), and source span. -/
namespace SorryScan

/-- Source span of a `Syntax`, as line/column `Position`s. -/
def stxRange (fileMap : FileMap) (stx : Syntax) : Position × Position :=
  let pos := stx.getPos?.getD 0
  let endPos := stx.getTailPos?.getD pos
  (fileMap.toPosition pos, fileMap.toPosition endPos)

/-- True iff `stx` is an explicit `sorry` tactic. -/
def isSorryTactic (stx : Syntax) : Bool := s!"{stx}" = "(Tactic.tacticSorry \"sorry\")"

/-- True iff `stx` is an explicit `sorry` term. -/
def isSorryTerm (stx : Syntax) : Bool := s!"{stx}" = "(Term.sorry \"sorry\")"

/-- All `Info` nodes satisfying `p`, each paired with its `ContextInfo`; descent
into a node's children stops when `stop` holds (used to avoid descending from a
tactic `sorry` into the synthetic term `sorry` it elaborates to). -/
partial def findAllInfo (t : InfoTree) (ctx? : Option ContextInfo) (p : Info → Bool)
    (stop : Info → Bool := fun _ => false) : List (Info × Option ContextInfo) :=
  match t with
  | .context ctx t => findAllInfo t (ctx.mergeIntoOuter? ctx?) p stop
  | .node i ts =>
    let info := if p i then [(i, ctx?)] else []
    let rest := if stop i then [] else ts.toList.flatMap (fun t => findAllInfo t ctx? p stop)
    info ++ rest
  | _ => []

/-- Tactic-mode `sorry` nodes with their contexts. -/
def findSorryTacticNodes (t : InfoTree) : List (TacticInfo × ContextInfo) :=
  let infos := findAllInfo t none fun i => match i with
    | .ofTacticInfo i => isSorryTactic i.stx && !i.goalsBefore.isEmpty
    | _ => false
  infos.filterMap fun p => match p with
    | (.ofTacticInfo i, some ctx) => (i, ctx)
    | _ => none

/-- Term-mode `sorry` nodes with their contexts. -/
def findSorryTermNodes (t : InfoTree) : List (TermInfo × ContextInfo) :=
  let infos := findAllInfo t none
    (fun i => match i with | .ofTermInfo i => isSorryTerm i.stx | _ => false)
    (fun i => match i with | .ofTacticInfo i => isSorryTactic i.stx | _ => false)
  infos.filterMap fun p => match p with
    | (.ofTermInfo i, some ctx) => (i, ctx)
    | _ => none

/-- Either a goal closed by a tactic `sorry`, or the expected type of a term `sorry`. -/
inductive SorryType
  | tactic : MVarId → SorryType
  | term : LocalContext → Option Expr → SorryType
  deriving Inhabited

/-- Every `sorry` in `t`: context, goal/type, and start/end position. -/
def sorries (t : InfoTree) : List (ContextInfo × SorryType × Position × Position) :=
  (findSorryTacticNodes t |>.map fun ⟨i, ctx⟩ =>
    -- HACK (from repl): give the context a child ngen so re-elaboration is fresh.
    ({ ctx with mctx := i.mctxBefore, ngen := ctx.ngen.mkChild.1 }, .tactic i.goalsBefore.head!,
      stxRange ctx.fileMap i.stx)) ++
  (findSorryTermNodes t |>.map fun ⟨i, ctx⟩ =>
    (ctx, .term i.lctx i.expectedType?, stxRange ctx.fileMap i.stx))

end SorryScan


/-- Collect every error-severity message currently in the command message log,
each tagged with the source span it was reported at. This mirrors how the REPL
turns its message log into the `errors` list of a `LeanCheckResult`. -/
def collectErrorInfos : CommandElabM (Array ErrorInfo) := do
  let mut out : Array ErrorInfo := #[]
  for msg in (← get).messages.toList do
    if msg.severity == MessageSeverity.error then
      let text ← msg.data.toString
      let span := mkSpanFromPos msg.pos (msg.endPos.getD msg.pos)
      out := out.push { message := text, span := some span }
  return out

/-- Collect every `sorry` occurrence from the elaboration info trees, rendering
the goal state at each `sorry` and recording the span of the `sorry` token.
Uses the REPL package's `InfoTree.sorries` (tactic- and term-mode `sorry`s) and
pretty-prints the goal via core `ppGoals`/`ppGoal`. -/
def collectSorryInfos (trees : List InfoTree) : CommandElabM (Array SorryInfo) := do
  let mut out : Array SorryInfo := #[]
  for tree in trees do
    for (ctx, stype, pos, endPos) in SorryScan.sorries tree do
      let goalFmt ← match stype with
        | .tactic mvarId => ctx.ppGoals [mvarId]
        | .term lctx expectedType? => ctx.runMetaM lctx do
            let ty ← match expectedType? with
              | some e => pure e
              | none => Meta.mkFreshTypeMVar
            let mv ← Meta.mkFreshExprMVar ty
            Meta.ppGoal mv.mvarId!
      out := out.push { goal := goalFmt.pretty.trimAscii.toString, span := some (mkSpanFromPos pos endPos) }
  return out


end
