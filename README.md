# SuperREPL

SuperREPL is intended to provide fast, batched Lean 4 utilites for checking large numbers of machine-generated proofs. 

## The goals
- Is as fast and parallel as possible
- Functions as a client/server setup so that it is compatible with distributed computing
- Functions fully online, and the server can sort out how best to route requests to various Lean processes based on heuristics
- Manages its memory usage and that of its Lean processes 


## The gameplan

The server will maintain a router, a heuristics manager, and a set of Lean interfaces. Given requests, the router will decide (based on heuristics) whether to open new Lean processes, close existing ones, and how to route the requests to be checked in Lean; things that must be considered by heuristics etc. are the amount of available RAM and CPUs, whether a Lean process has specifically compiled a part of the code before and saved it as a "prefix environment" (this makes checking much faster), whether the Lean process has imported similar modules previously (this makes checking somewhat faster), the startup time of a Lean process, whether a Lean process has already imported a bunch of modules that will no longer be used (this wastes memory), and how the volume and content of the requests relates to all this. Since requests may not all be received at once, the heuristics should maintain a rolling average or similar to ensure potentially faster Lean processes remain open for as long as is reasonable to wait. 


With uniform requests:

Cost = # requests * # constants per request * time per constant / # processes   +   # total imported modules added * time per imported module  +  total time remaining on processes
(ignoring finalizeImport for now since probably every module's gonna have to do that for convenience)

How to efficiently calculate the best scenario for permuting around processes' imported modules? How to handle freeing modules (will we want to keep them around)?

Idea: every process has a "time cost" for moving it into the current request group (current remaining processing time + import stuff), order them by this minus weighted average of "costs" for all other request groups?
Do we always allocate processes to groups relative to the groups' size, or is this ever inefficient?

Caching goals:
- If we get a long stream of the same request, all processes are delegated to that import set
- If we have only one process, it switches between the request groups enough to keep up with all incoming requests, but not too much
- Evict imports no longer used by any current groups



TODO:
- [X] Lean bridge + "uses imports" flag
- [ ] DSLean for type translation?
- [X] Imports processing
- [ ] Modularize Python side
- [ ] Benchmark + improve caching policy
- [ ] Send import diff instead of every time
