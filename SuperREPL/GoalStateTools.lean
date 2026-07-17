module


public import SuperREPL.CheckLean
public meta import SuperREPL.CheckLean
public import TrainingData.Utils.FindSource
public import TrainingData.InfoTree.Basic
public import TrainingData.Trace
public import TrainingData.Normalize
import Mathlib.Lean.Elab.Tactic.Meta


public section
open Lean Meta Elab Expr Term Command Parser Lean.Elab.Frontend IO


structure TacticGoalState where
  module : Name
  declaration : Name
  tactic : String
  tactic_kind : String
  context : Array String -- premises
  goal_before : String
  goal_after : String
  src_with_sorry : String -- the source of the declaration, with the current tactic replaced by `sorry`
deriving ToJson


def getImportsFromModule (mod : Name) : CommandElabM (Array Name) := do
  initMetaSearchPath
  let src ← moduleSource mod
  getImportsFromSrc src


/-- Returns information about each tactic used in declarations in the given module. -/
@[expose_python getImportsFromModule]
unsafe def traceTactics (mod : Name) : CommandElabM <| Array TacticGoalState := do
  initMetaSearchPath
  let src ← moduleSource mod

  let steps ← compilationStepsCached src

  let mut out := #[]

  for step in steps do
    for tree in step.trees do
      for (ti, ctx) in tree.tactics do
        try
          let (context, goal_before, goal_after, src_with_sorry) ← ti.pretty' step.stx ctx
          let tactic := (← ti.pp ctx).pretty (width := 100000000)
          let kind := (ti.name?.getD .anonymous).toString
          out := out.push {
            module := mod,
            declaration := ctx.parentDecl?.getD .anonymous,
            tactic := tactic,
            tactic_kind := kind,
            context := context,
            goal_before := goal_before,
            goal_after := goal_after,
            src_with_sorry := src_with_sorry
          }
        catch _ => continue
  return out

-- def tryTacticsAtEachStep (mod : Name) (tactics : Array String) (tacticModules : Array Name) : CommandElabM <| Array TacticGoalState := do
--   initMetaSearchPath
--   let src ← moduleSource mod

--   let imports := (← getImportsFromSrc src) ++ tacticModules
--   let body := removeHeader src

--   let env ← freshEnvironment (imports.map fun n => { module := n})

--   let steps ← processInput' body (env? := some env)

--   sorry


def String.extractRaw (s : String) (start : String.Pos.Raw) (end_ : String.Pos.Raw) : String :=
  s.toRawSubstring.extract start end_ |>.toString


/-- Attempts to solve each `sorry` in the given Lean code by trying each of the provided tactics. Returns
the source code with tactics filled in place of `sorry`s wherever they have succeeded.

Works fine with `sorry` used as a term (`def foo := sorry`) or as a tactic (`theorem bar : True := by sorry`),
as well as macros (`admit`, etc.). Remember that the tactics that are used must be imported by importing the
relevant module at the top of the provided source code. -/
@[expose_python fun s _ => getImportsFromSrc s]
unsafe def fillAutomation (leanCode : String) (tactics : Array String) : CommandElabM String := do
  -- Compile the code with sorries; don't use all caching so infotrees arent out of date
  let steps ← compilationStepsCached leanCode

  -- Collect all sorry-d goals from the compilation steps, along with their contexts and source spans. The `SorryScan` visitor runs on the raw syntax trees, so the positions are byte offsets into the original source string.
  let goals := steps.toList.flatMap fun step =>
    step.trees.flatMap fun tree =>
      (SorryScan.sorries tree).map fun (ctx, type, startpos, endpos) =>
        ({ ctx with env := step.before }, type, startpos, endpos)

  let mut replacements : Array (Position × Position × String) := #[]

  for (ctx, type, startpos, endpos) in goals do
    -- The two `sorry` flavors differ only in how we enter `MetaM` and obtain the
    -- goal metavariable; everything after (parse, run, soundness-check) is shared.
    let runInGoalCtx (k : MVarId → MetaM Bool) : IO Bool :=
      match type with
      | .tactic id => ctx.runMetaM {} <| withMCtx ctx.mctx <| k id
      | .term lctx goal? =>
        match goal? with
        | some goal => ctx.runMetaM lctx do k (← mkFreshExprMVar goal).mvarId!
        | none => pure false

    -- Try a single tactic string against this goal. Parsing happens inside
    -- `MetaM` so it sees the processed code's imported tactics, not this module's.
    -- `true` iff it honestly closed the goal (no goals left, assigned, sorry-free).
    let tryTactic (tac : String) : CommandElabM Bool := do
      try
        runInGoalCtx fun mvarId => do
          let tacticStx ←
            match Parser.runParserCategory (← getEnv) `tactic tac with
            | .ok stx => pure stx
            | .error err => throwError "could not parse tactic {repr tac}: {err}"
          let remaining ← runTactic' mvarId tacticStx (ctx := { errToSorry := false })
          let proof ← instantiateMVars (.mvar mvarId)
          return remaining.isEmpty && (← mvarId.isAssigned) && !proof.hasSorry
      catch _ => pure false

    -- Run the candidate tactics in order; record the first that closes the goal.
    let mut solvedBy : Option String := none
    for tac in tactics do
      if ← tryTactic tac then
        solvedBy := some tac
        break

    if let some tac := solvedBy then
      -- If successfully solved, record the success so that the sorry can be replaced by the tactic itself.
      -- When `sorry` is used as a term not a tactic, we need an extra `by`
      match type with
      | .tactic _ => replacements := replacements.push (startpos, endpos, tac)
      | .term ..  => replacements := replacements.push (startpos, endpos, s!"by {tac}")

  -- Splice the replacements back into the source. The spans are non-overlapping
  -- (everything parsed before we solved anything), so we convert each to byte
  -- offsets, sort by start, and stitch together the untouched gaps between them.


  let header := leanCode.toRawSubstring.extract leanCode.rawStartPos (← parseImports'' leanCode "<input>").pos |>.toString
  -- let leanCode := String.join <| (steps.map (fun s => s.src.toString)).toList
  let leanCode := removeHeader leanCode -- Positions don't account for the header

  let fileMap := leanCode.toFileMap
  let spans := replacements.map fun (startpos, endpos, repl) =>
    (fileMap.ofPosition startpos, fileMap.ofPosition endpos, repl)
  let sorted := spans.qsort fun a b => a.1.byteIdx < b.1.byteIdx
  let mut out := ""
  let mut cursor := leanCode.rawStartPos
  for (s, e, repl) in sorted do
    out := out ++ leanCode.extractRaw cursor s ++ repl
    cursor := e
  return header ++ (out ++ leanCode.extractRaw cursor leanCode.rawEndPos)




@[expose_python fun s _ => getImportsFromSrc s]
unsafe def getSuccessfulAutomation (leanCode : String) (tactics : Array String) : CommandElabM <| Array String := do
  -- Compile the code with sorries; don't use all caching so infotrees arent out of date
  let steps ← compilationStepsCached leanCode

  -- Collect all sorry-d goals from the compilation steps, along with their contexts and source spans. The `SorryScan` visitor runs on the raw syntax trees, so the positions are byte offsets into the original source string.
  let goals := steps.toList.flatMap fun step =>
    step.trees.flatMap fun tree =>
      (SorryScan.sorries tree).map fun (ctx, type, startpos, endpos) =>
        ({ ctx with env := step.before }, type, startpos, endpos)

  match goals with
  | [(ctx, type, _, _)] =>
    -- The two `sorry` flavors differ only in how we enter `MetaM` and obtain the
    -- goal metavariable; everything after (parse, run, soundness-check) is shared.
    let runInGoalCtx (k : MVarId → MetaM Bool) : IO Bool :=
      match type with
      | .tactic id => ctx.runMetaM {} <| withMCtx ctx.mctx <| k id
      | .term lctx goal? =>
        match goal? with
        | some goal => ctx.runMetaM lctx do k (← mkFreshExprMVar goal).mvarId!
        | none => pure false

    -- Try a single tactic string against this goal. Parsing happens inside
    -- `MetaM` so it sees the processed code's imported tactics, not this module's.
    -- `true` iff it honestly closed the goal (no goals left, assigned, sorry-free).
    let tryTactic (tac : String) : CommandElabM Bool := do
      try
        runInGoalCtx fun mvarId => do
          let tacticStx ←
            match Parser.runParserCategory (← getEnv) `tactic tac with
            | .ok stx => pure stx
            | .error err => throwError "could not parse tactic {repr tac}: {err}"
          let remaining ← runTactic' mvarId tacticStx (ctx := { errToSorry := false })
          let proof ← instantiateMVars (.mvar mvarId)
          return remaining.isEmpty && (← mvarId.isAssigned) && !proof.hasSorry
      catch _ => pure false

    -- Run the candidate tactics in order; record the first that closes the goal.
    let mut out := #[]
    for tac in tactics do
      if ← tryTactic tac then
        out := out.push tac

    return out

  | _ => return #[]



/-- Returns information about each tactic used in declarations in the given module. -/
@[expose_python fun x _ _ => getImportsFromModule x]
unsafe def successfulAutomationAtEachGoal (mod : Name) (tactics : Array String) (tacticImports : Array Name) : CommandElabM <| Array (Array String) := do
  initMetaSearchPath
  let src ← moduleSource mod

  let steps ← compilationStepsCached src (additionalImports := tacticImports.map (fun m => { module := m}))

  let mut out := #[]

  for step in steps do
    for tree in step.trees do
      for (ti, ctx) in tree.tactics do
        -- dbg_trace "Trying to solve tactic {ti.name?.getD .anonymous} in declaration {ctx.parentDecl?.getD .anonymous} with tactics {tactics}"
        let mut acc := #[]
        for tac in tactics do
          acc := acc.append <| ← ti.runMetaM ctx <| fun mvarId => do
            try
              let tacticStx ←
                match Parser.runParserCategory (← getEnv) `tactic tac with
                | .ok stx => pure stx
                | .error err => throwError "could not parse tactic {repr tac}: {err}"
              let remaining ← runTactic' mvarId tacticStx (ctx := { errToSorry := false })
              let proof ← instantiateMVars (.mvar mvarId)
              if remaining.isEmpty && (← mvarId.isAssigned) && !proof.hasSorry then
                return #[tac]
              return #[]
            catch _ =>
              -- dbg_trace "Error while trying tactic {tac} on goal {ti.name?.getD .anonymous} in declaration {ctx.parentDecl?.getD .anonymous}: {← e.toMessageData.format}"
              return #[]
        out := out.push acc
  return out
