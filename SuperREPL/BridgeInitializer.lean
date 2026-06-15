module
public import Lean.Attributes
public import Lean.Elab.Command
public import Lean.Meta.Eval
public import Qq.Macro
public import Qq.Typ

/-! IMPORTANT: when running this as an executable, this module must be `meta import`ed for the attribute to be available.-/


open Lean Meta Elab Term Command Qq

public section

/-- Information about a method registered for exposure to Python -/
structure RegisteredMethodInfo where
  /-- Name of the method -/
  name : Name
  /-- Description of the method (viewable to people/AIs on the Python side) -/
  description : String
  /-- Function to be called with JSON input and produce JSON output -/
  fn : Q(Json → CommandElabM Json)
  /-- Optional function to handle imports. If provided, the orchestrator will assume that this function requires importing modules to run, and will cache accordingly; otherwise, no special caching will occur. -/
  importsFn : Option (Q(Json → CommandElabM Json)) := none
  /-- List of arguments for the method -/
  args : List (Name × Expr) := []
  /-- Return type of the method (excluding monad application) -/
  returnType : Expr
  isInternal : Bool := false
deriving Inhabited


instance : ToJson RegisteredMethodInfo where
  toJson info := Json.mkObj
    [ ("name", toJson info.name)
    , ("input_schema", Json.mkObj <| info.args.map (fun (n, t) => (n.toString, toString t)))
    , ("output", toString info.returnType)
    , ("description", toJson info.description)
    , ("uses_imports", info.importsFn.isSome)
    , ("internal", info.isInternal)
    ]






instance : ToJson Unit where
  toJson := fun _ => Json.mkObj []


/-- Uniformly call any registered function with a list of `Json` arguments,
returning a `Json` result. All the per-argument `FromJson`/`ToJson` plumbing is
handled by instance resolution, so `materializeFunction` never has to build
syntax — it just builds `@applyJson <fnType> <inst> <theConst>`, which always
has the fixed type `List Json → CommandElabM Json`. -/
class JsonCallable (α : Type) where
  applyJson : α → List Json → CommandElabM Json

/-- Inductive case: peel off one argument. -/
instance (priority := 200) {α β : Type} [FromJson α] [JsonCallable β] :
    JsonCallable (α → β) where
  applyJson f
    | []          => throwError "Not enough arguments provided to exposed method"
    | (a :: rest) =>
      match fromJson? (α := α) a with
      | .ok x    => JsonCallable.applyJson (f x) rest
      | .error e => throwError s!"Could not parse argument: {e}" -- TODO: better error messages

/-- Base case for effectful results: run the action, then serialize. -/
instance (priority := 100) {β : Type} [ToJson β] : JsonCallable (CommandElabM β) where
  applyJson m _ := do return toJson (← m)

/-- Base case for pure results: serialize directly. Only fires on leaf values,
since `ToJson` fails to synthesize for functions and for `CommandElabM _`. -/
instance (priority := 50) {β : Type} [ToJson β] : JsonCallable β where
  applyJson b _ := return toJson b





/-- Obtain an executable function from an expression of one, with appropriate JSON conversions. -/
unsafe def materializeFunction (fn_expr : Expr) (args : List (Name × Expr)) : TermElabM (Json → CommandElabM Json) := do

  let wrapped ← mkAppM ``JsonCallable.applyJson #[fn_expr]

  let core ← evalExpr (List Json → CommandElabM Json)
    q(List Json → CommandElabM Json) wrapped (safety := .unsafe)

  return (unsafe fun (j : Json) => do
    let mut json_args := []
    for (argName, _) in args.reverse do
      let some argJson := j.getObjVal? argName.toString |>.toOption | throwError s!"Missing argument: {argName}"
      json_args := json_args ++ [argJson]
    core json_args)


def RegisteredMethodInfo.getFunction (self : RegisteredMethodInfo) : TermElabM <| Json → CommandElabM Json :=
  unsafe materializeFunction self.fn self.args

def RegisteredMethodInfo.getImportsFunction (self : RegisteredMethodInfo) : TermElabM <| Option <| Json → CommandElabM Json :=
  match self.importsFn with
  | some importsFn => do
    let fn := unsafe materializeFunction importsFn self.args
    pure <| some (← fn)
  | none => pure none


syntax (name := expose_python_stx) ("internal")? "expose_python" (term)? : attr



/-- Get information about an exposed method. Also checks that everything has correct types. -/
private def getMethodInfo (declName : Name) (attributeSyntax : Syntax) : CommandElabM RegisteredMethodInfo := do
  let env ← getEnv
  if let some ci := env.constants.find? declName then

    let doc_comment := docStringExt.find? env declName |>.getD ""

    /- Recursively get the arguments of a function and the return type; ensure they all have correct Json instances -/
    let rec go (e : Expr) : MetaM (List (Name × Expr) × Expr) := do
      match e with
      | .forallE argName argType body _ => do

        discard <| try synthInstance <| (fun x : Q(Type) => q(FromJson $x)) argType
        catch _ => throwError "Argument type {argType} is not serializable. Add a FromJson instance for this type."

        let rest ← go body
        return (rest.1 ++ [(argName, argType)], rest.2)
      | _ =>
        let no_monad := match e with
        | .app (.const ``Lean.Elab.Command.CommandElabM _) ty => ty
        | ty => ty

        discard <| try synthInstance <| (fun x : Q(Type) => q(ToJson $x)) no_monad
        catch _ => throwError "Return type {no_monad} is not serializable. Add a ToJson instance for this type."
        return ([], e)

    let (args, returnType) ← liftTermElabM <| go ci.type

    /- Build the syntax for the function application, handle both nonmonadic and monadic return types -/
    let fn_stx : TSyntax `term ←
      if !args.isEmpty then
        match returnType with
        | .app (.const ``Lean.Elab.Command.CommandElabM _) _ => `($(mkIdent declName))
        | _ => `((pure ∘ $(mkIdent declName) : CommandElabM _))
      else
        match returnType with
        | .app (.const ``Lean.Elab.Command.CommandElabM _) _ => `($(mkIdent declName))
        | _ => `(pure $(mkIdent declName))

    /- "materialize" the function (turn it from an `Expr` into executable code) -/
    let returnTypeMonadic := ((fun (x:Q(Type)) => q(CommandElabM $x)) returnType)
    let sig ← liftCoreM <| mkArrowN (args.reverse.map Prod.snd).toArray returnTypeMonadic
    let fn_expr ← liftTermElabM do instantiateMVars <| ← elabTerm fn_stx sig
    discard <| liftTermElabM <| unsafe materializeFunction fn_expr args

    /- Extract the two independent optionals from the `expose_python_stx` node
       positionally rather than via a 4-way quotation match:
         child 0 = `("internal")?`   child 1 = "expose_python"   child 2 = `(term)?`
       An absent optional parses to an empty `nullNode` (`.isNone`); a present
       one wraps its child (`.getOptional?`). -/
    let isInternal := !attributeSyntax[0].isNone

    /- materialize the optional imports function -/
    let importsSig ← liftCoreM <| mkArrowN (args.reverse.map Prod.snd).toArray q(CommandElabM <| Array Name)
    let materializedImportsFnExpr ← match attributeSyntax[2].getOptional? with
    | none => pure none
    | some importsFnStx => do
      try
        let importsFn ← liftTermElabM do
          withoutErrToSorry do
            let out ← elabTermEnsuringType importsFnStx (some importsSig)
            synthesizeSyntheticMVarsNoPostponing
            instantiateMVars out
        discard <| liftTermElabM <| unsafe materializeFunction importsFn args
        pure <| some importsFn
      catch e =>
        throwError s!"Error processing imports method for {declName}: {← e.toMessageData.format}"


    return { name := declName, fn := fn_expr, args := args, returnType := returnType, description := doc_comment, importsFn := materializedImportsFnExpr, isInternal := isInternal }

  else throwError "Declaration {declName} does not exist. Make sure you have the correct namespaces!"



/-- Exposes a method to Python. The method must be a constant with an executable value (i.e. a function or a def with no arguments). The method must have a docstring, which will be used as the description in the API docs. All argument and return types must have FromJson/ToJson instances, and the JSON keys for the arguments are taken from their names.

Provide an optional function to indicate this method imports modules and should be cached as such; the additional function should have the same arguments as the method, and return an `Array Name` of modules to be imported. Caching logic assumes that `importModules'` from TrainingData is used, as this caches the modules themselves locally to avoid redundant imports. -/
initialize exposeAttr : ParametricAttribute RegisteredMethodInfo ←
  registerParametricAttribute {
    name := `expose_python_stx
    descr := "Exposes a method to Python. The method must be a constant with an executable value (i.e. a function or a def with no arguments). The method must have a docstring, which will be used as the description in the API docs. All argument and return types must have FromJson/ToJson instances, and the JSON keys for the arguments are taken from their names.\n\nProvide an optional function to indicate this method imports modules and should be cached as such; the additional function should have the same arguments as the method, and return an `Array Name` of modules to be imported. Caching logic assumes that `importModules'` from TrainingData is used, as this caches the modules themselves locally to avoid redundant imports."
    applicationTime := .afterCompilation
    getParam := fun decl stx => do
      let prog : CommandElabM _ := getMethodInfo decl stx

      -- Seed the fabricated command scope with the ambient namespace and `open`s
      -- (carried by the attribute's `CoreM` context) so user-written attribute
      -- arguments resolve identifiers the same way they would at the use site —
      -- e.g. a bare `Name` under `open Lean`.
      let baseState := Command.mkState (← getEnv) {} {}
      let rootScope := { baseState.scopes.head! with
        currNamespace := (← getCurrNamespace), openDecls := (← getOpenDecls) }
      let state := { baseState with scopes := [rootScope], infoState.enabled := true }
      match ← prog.run { fileName := "<input>", fileMap := default, snap? := none, cancelTk? := none } |>.run state |>.toIO' with
      | .ok (result, newState) =>
        setEnv newState.env
        return result
      | .error err => throwError s!"Error processing declaration {decl}: {← err.toMessageData.format}"
  }


def getExposedMethods : CoreM (Array Name) := do
  let env ← getEnv
  let out := env.constants.fold (fun (acc : Array Name) n _ =>
    if exposeAttr.getParam? env n |>.isSome then acc.push n else acc) #[]
  return out

def getExposedMethodsInfo : CoreM (Array RegisteredMethodInfo) := do
  let env ← getEnv
  let out ← env.constants.foldM (fun (acc : Array RegisteredMethodInfo) n _ => do
    match exposeAttr.getParam? env n with
    | some info => return acc.push info
    | none => return acc
    ) #[]
  return out

def findExposedMethod? (methodName : Name) : CoreM (Option RegisteredMethodInfo) := do
  match exposeAttr.getParam? (← getEnv) methodName with
  | some info => return some info
  | none => return none

def findExposedMethod! (methodName : Name) : CoreM RegisteredMethodInfo := do
  match ← findExposedMethod? methodName with
  | some info => return info
  | none => throwError s!"Method {methodName} is not exposed to Python. Make sure it is marked with `@[expose_python]` and that you have the correct namespaces!"


end
