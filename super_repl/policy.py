"""Routing / allocation policy for the SuperREPL server.

Everything here is *decision logic*: how to group requests into categories, which
categories to keep warm, how many processes each gets, and which process a given
request should go to. `Server` (in `main.py`) owns the processes and queues and
simply delegates these decisions, so a whole policy can be swapped out by passing
a different object implementing `RoutingPolicy`.

The default `AdaptivePolicy` self-tunes across the full spectrum from "thousands
of proofs of the same thing" (identical headers) to "thousands of different
things" (all-different headers) with no a-priori knowledge:

  * `Clusterer` groups by import-set overlap, exact-import-set fast path first.
  * `ARC` self-tunes which categories stay warm (recency vs frequency).
  * `marginal_allocation` decides replica counts (concave -> hot categories get
    more replicas, a diffuse mix spreads one-per-process).
"""

from __future__ import annotations

import heapq
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .basic import Request
    from .lean_interface import LeanInterface


# --------------------------------------------------------------------------- #
# Similarity
# --------------------------------------------------------------------------- #

def containment(imports: frozenset[str], rep: frozenset[str]) -> float:
    """Fraction of `imports` already covered by `rep`.

    Asymmetric on purpose: the routing question is "how much of this request
    does the target already have." Uniform per-olean cost makes cardinality the
    right weight (the bottleneck is the *number* of oleans, not which ones).
    """
    if not imports:
        return 1.0
    return len(imports & rep) / len(imports)


# --------------------------------------------------------------------------- #
# Clustering: leader / threshold, with an exact-import-set fast path
# --------------------------------------------------------------------------- #

class Clusterer:
    """Assign requests to categories by import-set overlap.

    Identical import sets take an O(1) exact-match path (the same-thing-many-times
    regime). Otherwise a single-pass leader algorithm attaches the request to
    the existing category whose representative import set covers it above
    `threshold`, else opens a new one. At Mathlib scale (~6k modules) import sets
    are small frozensets, so exact containment is cheap -- no MinHash needed.
    """

    def __init__(self, threshold: float = 0.8):
        self.threshold = threshold
        self._by_imports: dict[frozenset[str], int] = {}
        self._reps: dict[int, frozenset[str]] = {}
        self._next_id = 0

    def representative(self, cluster: int) -> frozenset[str]:
        return self._reps[cluster]

    def assign(self, req: "Request") -> int:
        imports = req.imports
        cached = self._by_imports.get(imports)
        if cached is not None:
            return cached

        best, best_sim = None, 0.0
        for cid, rep in self._reps.items():
            sim = containment(imports, rep)
            if sim > best_sim:
                best, best_sim = cid, sim

        if best is not None and best_sim >= self.threshold:
            cid = best
            self._reps[cid] |= imports           # grow rep toward the union
        else:
            cid = self._next_id
            self._next_id += 1
            self._reps[cid] = imports

        self._by_imports[imports] = cid
        return cid


# --------------------------------------------------------------------------- #
# Warm-set: Adaptive Replacement Cache over category ids
# --------------------------------------------------------------------------- #

class ARC:
    """Self-tuning warm-set (Megiddo & Modha, USENIX FAST '03).

    Continuously balances *recency* (categories seen once recently -> the
    all-different-headers regime) against *frequency* (categories seen
    repeatedly -> the hot, same-import-set regime) with no a-priori tuning. Ghost
    lists `b1`/`b2` record recently evicted ids and steer the target `p`:
    re-hits in `b1` grow the recency arm, re-hits in `b2` grow the frequency
    arm. `warm_set()` is the set of categories that currently deserve capacity.

    Lists are ordered LRU (front) .. MRU (back).
    """

    def __init__(self, capacity: int):
        self.c = capacity
        self.p = 0
        self.t1: list[int] = []   # recent, seen once
        self.t2: list[int] = []   # frequent, seen more than once
        self.b1: list[int] = []   # ghosts evicted from t1
        self.b2: list[int] = []   # ghosts evicted from t2

    def warm_set(self) -> set[int]:
        return set(self.t1) | set(self.t2)

    def access(self, x: int) -> None:
        if x in self.t1:
            self.t1.remove(x); self.t2.append(x); return
        if x in self.t2:
            self.t2.remove(x); self.t2.append(x); return

        if x in self.b1:                                   # recency under-provisioned
            self.p = min(self.c, self.p + max(1, len(self.b2) // max(1, len(self.b1))))
            self._replace(x)
            self.b1.remove(x); self.t2.append(x); return
        if x in self.b2:                                   # frequency under-provisioned
            self.p = max(0, self.p - max(1, len(self.b1) // max(1, len(self.b2))))
            self._replace(x)
            self.b2.remove(x); self.t2.append(x); return

        # brand-new category: make room in the ghost/resident lists
        if len(self.t1) + len(self.b1) == self.c:
            if len(self.t1) < self.c:
                self.b1.pop(0); self._replace(x)
            else:
                self.t1.pop(0)
        else:
            total = len(self.t1) + len(self.t2) + len(self.b1) + len(self.b2)
            if total >= self.c:
                if total == 2 * self.c:
                    self.b2.pop(0)
                self._replace(x)
        self.t1.append(x)

    def _replace(self, x: int) -> None:
        if self.t1 and (len(self.t1) > self.p or (x in self.b2 and len(self.t1) == self.p)):
            self.b1.append(self.t1.pop(0))
        elif self.t2:
            self.b2.append(self.t2.pop(0))


# --------------------------------------------------------------------------- #
# Allocation: marginal-value greedy (optimal for concave value)
# --------------------------------------------------------------------------- #

def marginal_allocation(rates: dict[int, float], slots: int) -> dict[int, int]:
    """Hand out `slots` processes across categories by repeatedly giving the next
    slot to the category with the highest *marginal* value.

    The n-th process for a category is worth ``rate / n`` (diminishing returns),
    so the cumulative value is concave and this greedy is optimal. A hot category
    keeps a high marginal and accrues replicas; a diffuse mix flattens out and
    the slots spread one-per-category -- the regime balance falls out for free.
    """
    counts = {c: 0 for c in rates}
    heap = [(-rate, c) for c, rate in rates.items() if rate > 0]
    heapq.heapify(heap)
    for _ in range(slots):
        if not heap:
            break
        _, c = heapq.heappop(heap)
        counts[c] += 1
        heapq.heappush(heap, (-(rates[c] / (counts[c] + 1)), c))
    return counts


# --------------------------------------------------------------------------- #
# Policy interface + default implementation
# --------------------------------------------------------------------------- #

class RoutingPolicy(Protocol):
    """What `Server` needs from a policy. Implement this to swap behavior."""

    def classify(self, req: "Request") -> int:
        """Return the category id for `req` (and update any internal state)."""
        ...

    def allocate(self, processes: list["LeanInterface"],
                 demand: dict[int, float]) -> dict[int, list[int]]:
        """Given per-category arrival rates, decide which processes serve which
        category (pinning them) and return the category -> [process id] map."""
        ...

    def route(self, req: "Request", processes: list["LeanInterface"],
              assignment: dict[int, list[int]]) -> "LeanInterface":
        """Pick the process that should run `req`."""
        ...


class AdaptivePolicy:
    """Import-set clustering + ARC warm-set + marginal allocation + affinity routing.

    Routing and re-pinning rank processes by `eta` -- the time to *finish* a
    request, i.e. drain the existing backlog plus import whatever is missing --
    so a process buried under queued work is not chosen just because its cache
    happens to overlap well.
    """

    def __init__(self, num_processes: int, cluster_threshold: float = 0.8):
        self.clusterer = Clusterer(cluster_threshold)
        self.arc = ARC(capacity=num_processes)

    def classify(self, req: "Request") -> int:
        cid = self.clusterer.assign(req)
        self.arc.access(cid)
        return cid

    def allocate(self, processes: list["LeanInterface"],
                 demand: dict[int, float]) -> dict[int, list[int]]:
        warm = self.arc.warm_set()
        need = marginal_allocation({c: r for c, r in demand.items() if c in warm},
                                   len(processes))
        assignment: dict[int, list[int]] = {c: [] for c in need}
        free: list["LeanInterface"] = []

        # keep processes on a still-wanted category (hysteresis / minimal churn)
        for proc in processes:
            if proc.cluster in need and need[proc.cluster] > 0:
                need[proc.cluster] -= 1
                assignment[proc.cluster].append(proc.id)
            else:
                free.append(proc)

        # fill remaining demand soonest-available-first (drain backlog + import)
        for cluster, remaining in need.items():
            rep = self.clusterer.representative(cluster)
            for _ in range(remaining):
                if not free:
                    break
                proc = min(free, key=lambda p: p.eta(rep))
                free.remove(proc)
                proc.cluster = cluster
                assignment[cluster].append(proc.id)

        for proc in free:           # leftovers become generalists for cold traffic
            proc.cluster = None
        return assignment

    def route(self, req: "Request", processes: list["LeanInterface"],
              assignment: dict[int, list[int]]) -> "LeanInterface":
        replicas = assignment.get(req.cluster)
        # warm category -> among its replicas (bounded-load spillover); cold
        # category -> any process, whichever could finish soonest.
        candidates = [processes[i] for i in replicas] if replicas else processes
        return min(candidates, key=lambda p: p.eta(req.imports))
