module

public import SuperREPL.CheckingUtilities
public meta import SuperREPL.CheckingUtilities
public meta import TrainingData.Utils.Dependencies
public meta import TrainingData.Utils.Frontend
public import TrainingData.Utils.Frontend
import Std.Data.Iterators

public section

open Lean Meta Elab Command


def String.leftOverlap (s1 s2 : String) : String :=
  s1.chars.zip s2.chars |>.fold (fun (acc, stop) (c1, c2) => if c1 == c2 && !stop then (acc.push c1, false) else (acc, true)) ("", false) |>.fst

/-- Drop a possibly-incomplete trailing token: the largest prefix of `s` that
ends on a whitespace boundary (empty if `s` has no whitespace). The prefix
heuristics below compare source text character-by-character, which can cut in
the middle of a token; trimming back to whitespace ensures we never feed the
elaborator a command that was truncated mid-token (such a fragment can parse as
a valid-but-wrong command — e.g. `:= 1` out of `:= 12`). Relies on top-level
commands being whitespace-separated. -/
def String.dropPartialToken (s : String) : String :=
  String.ofList (s.toList.reverse.dropWhile (fun c => !c.isWhitespace) |>.reverse)

/-- True iff `candidate` is a prefix of `s` that lands on a command boundary:
either it is all of `s`, or the character immediately after it is whitespace.
A bare `String.startsWith` is not enough — `def n := 1` is a textual prefix of
`def n := 12` but not a command-boundary prefix. -/
def String.isBoundaryPrefix (s candidate : String) : Bool :=
  s.startsWith candidate &&
    (let rest := s.drop candidate.length; rest.isEmpty || rest.front.isWhitespace)


instance : Ord Name where
  compare n1 n2 := compare n1.toString n2.toString


def toResult (steps : Array IO.CompilationStep) : CommandElabM FullCheckResult := do
  let errors ← steps.map IO.CompilationStep.errors |>.toList.flatten.toArray |>.mapM (fun msg => do
    let text ← msg.data.toString
    let span := mkSpanFromPos msg.pos (msg.endPos.getD msg.pos)
    pure ({ message := text, span := span } : ErrorInfo)
  )

  let mut decls := #[]
  let mut axioms := #[]
  for step in steps do
    for decl in step.diff do
      let axs := getAxioms decl step.after
      decls := decls.push ({ name := decl.name, type := toString decl.type, src := step.src.toString, has_sorry := axs.contains sorryAxiom } : LeanDeclaration)

      for ax in axs do
        if ax ∉ axioms then
          axioms := axioms.push ax


  let sorries : Array SorryInfo ← if `sorryAx ∈ axioms then do
    let out := (← steps.mapM (fun step => do
      collectSorryInfos step.trees)).flatten
    if out.isEmpty then
      pure #[({ goal := "sorryAx was used somewhere in the proof; it may be hidden", span := none } : SorryInfo)]
    else pure out
  else
    pure #[]

  let axioms_ok := axioms.all (· ∈ allowedAxioms)

  let msgs ← steps.map (·.msgs.toArray) |>.flatten.mapM (fun m => Message.toMessageInfo m)

  return {
    ok := sorries.isEmpty && errors.isEmpty,
    status := if !errors.isEmpty then "error"
      else if !sorries.isEmpty then "typechecksWithSorry"
      else if !axioms_ok then "disallowedAxioms"
      else "typechecks",
    errors := errors,
    sorries := sorries,
    axiomsOk := axioms_ok,
    disallowedAxioms := axioms.filter (· ∉ allowedAxioms) |>.map toString,
    decls := decls,
    messages := msgs
  }




initialize prefixEnvironmentCache : IO.Ref (Option (Array Import × String × Environment × Array IO.CompilationStep)) ← IO.mkRef none




unsafe def compilationStepsCached (leanCode : String) : CommandElabM (Array IO.CompilationStep) := do
  enableInitializersExecution

  let imports := (← parseImports'' leanCode "<input>").imports
  let mut source := removeHeader leanCode

  let mut steps := #[]

  let mut possibleSourcePrefix : Option String := none -- The maximum possible overlap between the current and previously processed source code. We set this if we actually want to recompute the environment that will be cached for next time
  let mut actualSourcePrefix : Option String := none -- The overlap between the current and previously processed source code that we actually end up processing (which may be less than the above if half a command gets cut off or something)

  let env ← if let some (cachedImports, cachedSource, cachedEnv, cachedSteps) ← prefixEnvironmentCache.get then
    if cachedImports == imports then
      -- maybe used cached environment

      if source.isBoundaryPrefix cachedSource then
        -- can reuse cached environment (cached prefix ends on a command boundary)
        actualSourcePrefix := some cachedSource
        steps := cachedSteps
        pure cachedEnv
      else
        -- Trim any partially-overlapping final token so the incremental pass below
        -- never elaborates a command that was cut mid-token.
        let overlap := (source.leftOverlap cachedSource).dropPartialToken
        -- enough overlap to potentially reuse cached environment next round
        possibleSourcePrefix := some overlap

        freshEnvironment imports
    else
      -- different imports, can't reuse cached environment
      possibleSourcePrefix := some source
      freshEnvironment imports
  else -- no cached environment, need to create one
    possibleSourcePrefix := some source
    freshEnvironment imports

  let mut env := env

  if let some overlap := possibleSourcePrefix then
    let mut processedPos := 0
    for step in IO.processInput' overlap (some env) do
      if step.hasErrors then
        break

      steps := steps.push step
      processedPos := step.src.stopPos
      env := step.after

    if h : String.Pos.Raw.IsValid source processedPos then
      let pos : String.Pos source := ⟨processedPos, h⟩
      actualSourcePrefix := some (source.sliceTo pos |>.toString)
    else throwError "Assertion failed: processInput' somehow gave a position that isn't valid in the original source string"


  if let some actualPrefix := actualSourcePrefix then
    if !actualPrefix.isEmpty then

      prefixEnvironmentCache.set (some (imports, actualPrefix, env, steps))
      source := source.drop actualPrefix.length |>.toString

      for step in IO.processInput' source (some env) do
        steps := steps.push step

      return steps

    else -- If there is no valid overlap, we keep the entire source cached instead of caching an empty string so it doesn't get stuck
      for step in IO.processInput' source (some env) do
        steps := steps.push step
      prefixEnvironmentCache.set (some (imports, source, env, steps))
      return steps
  else
    throwError "Assertion failed: actualSourcePrefix was unset"



def getImportsFromSrc : String → CommandElabM (Array Name) := fun code => do return (← collectDependenciesCached ((← parseImports'' code "<input>").imports.map (·.module))).map Prod.fst

/-- Checks a supplied piece of Lean code, returning information about errors, `sorry` goals, nonstandard axioms, and created declarations.

Implements significant caching logic to speed up repeated checks of similar code. Works best when similar queries (those which have the same imports and begin with the same source code) are sent in sequence.
-/
@[expose_python getImportsFromSrc]
unsafe def checkLean (leanCode : String) : CommandElabM FullCheckResult := do
  let steps ← compilationStepsCached leanCode
  toResult steps



-- unsafe def solveWithAutomation (imports : Array Name) (codeWithoutImportStatements : String) : CommandElabM FullCheckResult := do
--   let importArr := #[{module := `Init}] ++ (imports.map (fun m => { module := m }))
--   let (_, result) ← withFreshCommandElabM importArr (source := codeWithoutImportStatements) do
--     let parsedRes ← (do
--       try
--         let cmds ← parseCommands codeWithoutImportStatements (← getEnv)
--         pure (Except.ok cmds.toArray)
--       catch e =>
--         pure (Except.error (← e.toMessageData.toString)))

--     match parsedRes with
--     | .error errStr =>
--         pure {
--           ok := false, status := "error",
--           errors := #[{ message := errStr, span := none }],
--           sorries := #[], axiomsOk := false, disallowedAxioms := #[],
--           problemComplete := false, decls := #[]
--         }
--     | .ok cmds =>
--         let tac ← `(by auto_solve)
--         let cmds ← cmds.mapM (fun (name, decl) =>do
--           let out ← replaceValueIfSorry decl tac
--           return (name, out))
--         checkCommandsFull (cmds.map Prod.snd) (cmds.map Prod.fst) none
--   return result
