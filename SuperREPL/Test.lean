import SuperREPL.CheckLean
import Lean


open Lean Elab Command

-- #eval do
--   let code := "module
-- public section



-- namespace step0

-- theorem step_decl : 3 * 3 = 9 := by sorry

-- end step0"
--   logInfo m!"{toJson <| ← checkLean code}"

--   let code := "module
-- public section



-- namespace step0

-- theorem step_decl : 3 * 3 = 9 := by sorry

-- end step0"
--   logInfo m!"{toJson <| ← checkLean code}"



-- #eval do
--   let res ← checkLean "module \n import Mathlib\npublic section \n theorem putnam_1962_b1 (p : ℕ → ℝ → ℝ) (x y : ℝ) (n : ℕ) (h0 : p 0 = fun x : ℝ => 1) (hp : ∀ n > 0, p n = fun x : ℝ => ∏ i ∈ Finset.range n, (x - i)) : p n (x+y) = ∑ k ∈ Finset.range (n+1), Nat.choose n k * (p k x) * (p (n - k) y) := sorry"
--   IO.println s!"{toJson res}"


-- namespace SuperREPLTests

-- /-- Run `checkLeanCachingEnv code` and assert that every name in `expected`
-- appears in the resulting `decls`, and that the presence of errors matches
-- `wantError`. Prints a PASS/FAIL line and returns `true` on pass.

-- The cache is a process-global `IO.Ref`, so these checks are meant to be run as
-- an ordered sequence: each one exercises the cache state left by the previous. -/
-- unsafe def expect (label : String) (code : String) (expected : List String)
--     (wantError : Bool := false) : CommandElabM Bool := do
--   let res ← checkLeanCachingEnv code
--   -- Under the module system, top-level decls surface as `_private.0.<name>`, so
--   -- match on the trailing component rather than the exact string.
--   let hasDecl := fun (n : String) => res.decls.any (fun d => d == n || d.endsWith s!".{n}")
--   let missing := expected.filter (fun n => !hasDecl n)
--   let errOk := res.errors.isEmpty == !wantError
--   if missing.isEmpty && errOk then
--     IO.println s!"  PASS  {label}  (decls={res.decls}, status={res.status})"
--     return true
--   else
--     IO.println s!"  FAIL  {label}"
--     IO.println s!"        decls   = {res.decls}"
--     IO.println s!"        missing = {missing}"
--     IO.println s!"        status  = {res.status}  (wantError={wantError})"
--     IO.println s!"        errors  = {res.errors.map (·.message)}"
--     return false

-- end SuperREPLTests





-- open SuperREPLTests in
-- #eval show CommandElabM Unit from do
--   let mut r := #[]
--   -- 1. Fresh call, empty cache (no-cache branch).
--   r := r.push (← expect "fresh / empty cache"
--         "def aμ : Nat := 1\ndef a2 : Nat := 2" ["aμ", "a2"])
--   -- 2. Byte-identical re-call: full prefix hit, empty remainder (startsWith branch).
--   r := r.push (← expect "identical re-call (full cache hit)"
--         "def a1 : Nat := 1\ndef a2 : Nat := 2" ["a1", "a2"])
--   -- 3. Same shape, different names: overlap is a partial command, so we revert to
--   --    a fresh env and reprocess everything (overlap-miss / else branch).
--   r := r.push (← expect "different names, same shape"
--         "def b1 : Nat := 1\ndef b2 : Nat := 2" ["b1", "b2"])
--   -- 4. Extend the previously-cached input: startsWith hit with a non-empty remainder.
--   r := r.push (← expect "extend cached prefix"
--         "def b1 : Nat := 1\ndef b2 : Nat := 2\ndef b3 : Nat := 3" ["b1", "b2", "b3"])
--   -- 5. Share a complete leading decl, diverge in a later one (partial-prefix reuse).
--   r := r.push (← expect "shared first decl, divergent tail"
--         "def b1 : Nat := 1\ndef zzz : Nat := 9" ["b1", "zzz"])
--   -- 6. Empty input: must not crash, no decls, no errors.
--   r := r.push (← expect "empty input" "" [])
--   -- 7. Real input immediately after an empty-input cache entry (empty-prefix transition).
--   r := r.push (← expect "real input after empty"
--         "def c1 : Nat := 1\ndef c2 : Nat := 2" ["c1", "c2"])
--   -- 8. Hard error in the middle: surrounding decls still processed, error surfaced.
--   r := r.push (← expect "error in middle"
--         "def d1 : Nat := 1\ndef d2 : Nat := \"x\"\ndef d3 : Nat := 3" ["d1", "d3"] (wantError := true))
--   -- 9. Recovery: a clean input after an erroring one must report no errors.
--   r := r.push (← expect "clean input after error"
--         "def e1 : Nat := 1\ndef e2 : Nat := 2" ["e1", "e2"])

--   -- 10/11. Mid-token boundary guard: seed a clean single-decl cache, then submit
--   --        an input that *extends the last token* of the cached prefix rather than
--   --        adding a command. Without the boundary guard this spuriously errors
--   --        ("unexpected token") and reuses the stale decl.
--   r := r.push (← expect "seed single decl" "def n : Nat := 1" ["n"])
--   r := r.push (← expect "extend last token (boundary guard)" "def n : Nat := 12" ["n"])

--   let failures := (r.filter (· == false)).size
--   IO.println s!"\n{r.size - failures}/{r.size} assertions passed"

--   if failures > 0 then
--     throwError s!"{failures} caching assertions failed"
