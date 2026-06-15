module
public import TrainingData.Environment.CacheImports
public import Lean

public section
open Lean Meta Elab Command

/-- Keep track of the time taken to import modules at the most recent call -/
initialize importsTimeRef : IO.Ref (Option Nat) ← IO.mkRef none

/-- Keep track of the number of cache misses at the most recent call to importModulesCached -/
initialize importCacheMissesRef : IO.Ref (Option Nat) ← IO.mkRef none


/--
Creates environment object from given imports. Also works with SuperREPL's caching system to ensure concurrent Lean processes are allocated the best tasks given what modules they've loaded.

If `leakEnv` is true, we mark the environment as persistent, which means it will not be freed. We
set this when the object would survive until the end of the process anyway. In exchange, RC updates
are avoided, which is especially important when they would be atomic because the environment is
shared across threads (potentially, storing it in an `IO.Ref` is sufficient for marking it as such).

If `loadExts` is true, we initialize the environment extensions using the imported data. Doing so
may use the interpreter and thus is only safe to do after calling `enableInitializersExecution`; see
also caveats there. If not set, every extension will have its initial value as its state. While the
environment's constant map can be accessed without `loadExts`, many functions that take
`Environment` or are in a monad carrying it such as `CoreM` may not function properly without it.

If `level` is `exported`, the module to be elaborated is assumed to be participating in the module
system and imports will be restricted accordingly. If it is `server`, the data for
`getModuleEntries (includeServer := true)` is loaded as well. If it is `private`, all data is loaded
as if no `module` annotations were present in the imports.
-/
def importModulesCached (imports : Array Import) (opts : Options) (trustLevel : UInt32 := 0)
  (plugins : Array System.FilePath := #[]) (leakEnv loadExts : Bool := false) (level : OLeanLevel := OLeanLevel.private)
  (arts : NameMap ImportArtifacts := ∅) : IO Environment := do
  let start ← IO.monoMsNow
  let (env, misses) ← importModulesGetMisses imports opts trustLevel plugins leakEnv loadExts level arts
  let stop ← IO.monoMsNow
  importsTimeRef.set (some (stop - start))
  importCacheMissesRef.set (some misses)
  return env


/-- Pop the most recent import statistics, returning the time taken in milliseconds (first return value) and the number of total cache misses (second value). -/
def popLastImportStats : IO (Option (Nat × Nat)) := do
  let time ← importsTimeRef.get
  let misses ← importCacheMissesRef.get
  importsTimeRef.set none
  importCacheMissesRef.set none
  return match time, misses with
         | some t, some m => some (t, m)
         | _, _ => none

end
