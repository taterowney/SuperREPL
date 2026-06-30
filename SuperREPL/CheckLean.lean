module

public import SuperREPL.CheckingUtilities
public meta import SuperREPL.CheckingUtilities
public meta import TrainingData.Utils.Dependencies
public meta import TrainingData.Utils.Frontend
public import TrainingData.Utils.Frontend
public import TrainingData.Utils.ConstantInfo
import Std.Data.Iterators

/-! Exposes a `checkLean` function that can be called from Python to check a string
of Lean code, returning a JSON-serializable object with information about errors, sorries, axioms, and declarations.

checkLean uses all the environment caching logic defined elsewhere, plus some extra
magic to save time by not re-elaborating commands that have already been elaborated
previously. We still parse everything for robustness since matching on the raw source
code doens't deal with jankiness like `"def x := 1"` -> `"def x := 1\n + 1"`; empirically
parsing is far faster than elaboration (since most big bottlenecks come from typeclass
inference, unification, etc.), so this saves a lot of time when checking a sequence of
similar e.g. machine-generated proofs
-/


public section

open Lean Meta Elab Expr Term Command Parser Lean.Elab.Frontend IO


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

      for ax in axs do
        if ax ∉ axioms then
          axioms := axioms.push ax

      if decl.isInternal || !(decl.isTheorem || decl.isDef || decl.isAxiom || decl.isInductive || decl.isCtor) then continue

      let pretty := (← liftTermElabM <| PrettyPrinter.ppExpr decl.type).pretty'
      decls := decls.push ({ name := decl.name, type := pretty, src := step.src.toString, has_sorry := axs.contains sorryAxiom } : LeanDeclaration)




  let sorries : Array SorryInfo ← if `sorryAx ∈ axioms then do
    let out := (← steps.mapM (fun step => do
      collectSorryInfos step.trees)).flatten
    if out.isEmpty then
      pure #[({ goal := "sorryAx was used somewhere in the proof; it may be hidden", span := none } : SorryInfo)]
    else pure out
  else
    pure #[]

  let axioms_ok := axioms.all (· ∈ allowedAxioms ++ [sorryAxiom])

  let msgs ← steps.map (·.msgs.toArray) |>.flatten.mapM (fun m => Message.toMessageInfo m)

  return {
    ok := sorries.isEmpty && errors.isEmpty && axioms_ok,
    status := if !errors.isEmpty then "error"
      else if !sorries.isEmpty then "typechecksWithSorry"
      else if !axioms_ok then "disallowedAxioms"
      else "typechecks",
    errors := errors,
    sorries := sorries,
    axiomsOk := axioms_ok,
    additionalAxioms := axioms.filter (· ∉ allowedAxioms ++ [sorryAxiom]) |>.map toString,
    decls := decls,
    messages := msgs
  }




initialize prefixEnvironmentCache : IO.Ref (Option (Array Import × String.Pos.Raw × String × Array (CompilationStep × Command.State))) ← IO.mkRef none



unsafe def compilationStepsCached (leanCode : String) : CommandElabM (Array IO.CompilationStep) := do
  enableInitializersExecution

  let parsed ← parseImports'' leanCode "<input>"
  let imports := parsed.imports
  let headerPos := parsed.pos

  let mut source := removeHeader leanCode


  let env ← freshEnvironment imports

  -- Step through each command, parsing them in the appropriate environment but using the already-elaborated cached steps if it has been elaborated before. Keeps track of `Command.State`s so namespaces, etc. can be remembered
  let aux : FrontendM (Array (CompilationStep × Command.State)) := do
    let mut out := #[]
    let mut lastPos := (← get).parserState.pos

    if let some (cachedImports, cachedHeaderPos, cachedSource, cachedSteps) ← prefixEnvironmentCache.get then
      if cachedImports == imports -- If the imports are the same...
        && cachedHeaderPos == headerPos then -- ...and the headers of both are the same size (so infotrees aren't out of date)
        -- maybe use the cached environment

        for (step, cmdState) in cachedSteps do -- loop through each cached compilationStep and confirm that it fully matches with the current parsed source code. If so, use the cached one instead of elaborating again
          let s := (← get).commandState
          let before := s.env
          updateCmdPos
          let ictx ← getInputContext
          let pstate ← getParserState
          let scope := s.scopes.head!
          let pmctx := { env := s.env, options := scope.opts, currNamespace := scope.currNamespace, openDecls := scope.openDecls }
          let (cmd, ps, parserMessages) := profileit "parsing" scope.opts fun _ =>
            Parser.parseCommand ictx pmctx pstate s.messages

          let src := Substring.Raw.mk ictx.inputString lastPos ps.pos

          unless cmd == step.stx -- Same syntax...
            && src == step.src -- ...and same source code (again counting whitespace so infotrees are valid)
            do break

          out := out.push (step, cmdState)
          lastPos := ps.pos
          modify fun st => { st with commandState := cmdState }
          modify fun st => { st with commands := st.commands.push cmd }
          setParserState ps
          setMessages parserMessages
          -- `parseCommand` appends its messages to the ones passed in (`s.messages`); the new tail is this
          -- command's parse-stage messages, which `elabCommandTopLevel`'s reset would otherwise discard.
          let parseMsgs := parserMessages.toList.drop s.messages.toList.length

          if Parser.isTerminalCommand cmd then
            return out




    while true do -- Process the remaining steps until complete
      let (cmd, done) ← CompilationStep.one
      out := out.push (cmd, (← get).commandState)
      if done then break

    return out




  let ictx := mkInputContext leanCode "<input>"
  let (_, parserState, _) ← Parser.parseHeader ictx
  let opts := {}
  let commandState := { Command.mkState env {} opts with infoState.enabled := true }

  let (stepsWithStates, s) ← aux.run { inputCtx := ictx } |>.run { commandState, parserState, cmdPos := parserState.pos }

  prefixEnvironmentCache.set (some (imports, headerPos, source, stepsWithStates)) -- Store all of this round's steps (still ok if the next round only uses some of them since `aux` determines exactly how many can be used)

  return stepsWithStates.map (·.fst)





def getImportsFromSrc : String → CommandElabM (Array Name) := fun code => do return (← collectDependenciesCached ((← parseImports'' code "<input>").imports.map (·.module))).map Prod.fst

/-- Checks a supplied piece of Lean code, returning information about errors, `sorry` goals, nonstandard axioms, and created declarations.

Implements significant caching logic to speed up repeated checks of similar code. Works best when similar queries (those which have the same imports and begin with the same source code) are sent in sequence.
-/
@[expose_python getImportsFromSrc]
unsafe def checkLean (leanCode : String) : CommandElabM FullCheckResult := do
  let steps ← compilationStepsCached leanCode
  toResult steps
