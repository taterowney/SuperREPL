module

public import TrainingData.Environment.CacheImports
public meta import SuperREPL.BridgeInitializer
import Lean

public section

open Lean Meta Elab Command


@[internal expose_python]
def freeCachedModules (modulesToFree : Array Name) : CommandElabM Unit := freeModules modulesToFree


end
