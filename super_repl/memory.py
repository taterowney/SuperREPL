"""Optional RAM-budget enforcement for a pool of Lean bridges.

Each :class:`~lean_interface.LeanInterface` runs a ``lake``/``lean`` process tree
whose resident memory grows over its lifetime as it caches imported modules. This
module keeps the *aggregate* footprint of a pool under a caller-supplied budget:

* **Startup fit** — right after the pool spawns, :meth:`MemoryManager.check_startup`
  verifies the freshly-imported processes fit with room to spare
  (``startup_fraction`` of the budget, default 75%). The remaining headroom is
  what the caches are allowed to grow into; if the baseline already overruns it,
  the budget cannot be honored and a :class:`MemoryBudgetError` is raised.

* **Growth control** — a background monitor samples every bridge's tree RSS on an
  interval. When the aggregate crosses ``high_water_fraction`` of the budget
  (default 90%) it restarts the single hungriest bridge, which preserves that
  bridge's queued requests (see :meth:`LeanInterface.restart`) while dropping its
  memory back to the freshly-imported baseline.

The manager reads memory only; it never blocks request handling except for the
restart it explicitly triggers. It is deliberately decoupled from
:class:`~main.Server` so the same policy can guard any list of bridges.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .lean_interface import LeanInterface

logger = logging.getLogger(__name__)


class MemoryBudgetError(RuntimeError):
    """Raised when a freshly-spawned pool already exceeds its startup allowance,
    so the requested memory budget cannot be honored (too many processes, or a
    budget too small for the modules being imported)."""


def _human(n: int | None) -> str:
    """Format a byte count for logs (``None`` -> ``"?"``)."""
    if n is None:
        return "?"
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}TiB"


class MemoryManager:
    """Keep a pool of Lean bridges under an aggregate RAM budget.

    Parameters
    ----------
    processes:
        The bridges to watch. Held by reference, so restarts/replacements inside
        each :class:`~lean_interface.LeanInterface` are picked up automatically.
    budget_bytes:
        Total resident memory (bytes) the pool is allowed to occupy.
    startup_fraction:
        Fraction of the budget the *freshly-spawned* pool must fit within
        (default 0.75), leaving the rest as headroom for cache growth.
    high_water_fraction:
        Fraction of the budget at which the monitor restarts the hungriest
        bridge (default 0.90). Must be ``>= startup_fraction`` and ``<= 1``.
    interval:
        Seconds between monitor samples (default 5).
    min_restart_interval:
        Minimum seconds between budget-triggered restarts, so a single spike
        cannot thrash the pool while a fresh process is still warming up
        (default 20).
    """

    def __init__(
        self,
        processes: "list[LeanInterface]",
        budget_bytes: int,
        *,
        startup_fraction: float = 0.75,
        high_water_fraction: float = 0.90,
        interval: float = 5.0,
        min_restart_interval: float = 20.0,
    ) -> None:
        if budget_bytes <= 0:
            raise ValueError("budget_bytes must be positive")
        if not 0 < startup_fraction <= high_water_fraction <= 1.0:
            raise ValueError(
                "require 0 < startup_fraction <= high_water_fraction <= 1"
            )
        self.processes = processes
        self.budget_bytes = int(budget_bytes)
        self.startup_fraction = startup_fraction
        self.high_water_fraction = high_water_fraction
        self.interval = interval
        self.min_restart_interval = min_restart_interval

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_restart = 0.0

    # ── Derived thresholds ───────────────────────────────────────
    @property
    def startup_bytes(self) -> int:
        """Byte ceiling the freshly-spawned pool must fit under."""
        return int(self.budget_bytes * self.startup_fraction)

    @property
    def high_water_bytes(self) -> int:
        """Byte level at which the hungriest bridge is restarted."""
        return int(self.budget_bytes * self.high_water_fraction)

    # ── Reading ──────────────────────────────────────────────────
    def _read(self) -> tuple[list[int | None], int]:
        """Per-process tree RSS (``None`` when unreadable, e.g. mid-restart) and
        the total over the readable ones."""
        rss = [p.memory_rss() for p in self.processes]
        total = sum(r for r in rss if r is not None)
        return rss, total

    def snapshot(self) -> dict[str, Any]:
        """A JSON-friendly view of current memory vs. budget, for health checks."""
        rss, total = self._read()
        return {
            "budget_bytes": self.budget_bytes,
            "total_rss_bytes": total,
            "startup_bytes": self.startup_bytes,
            "high_water_bytes": self.high_water_bytes,
            "fraction": total / self.budget_bytes if self.budget_bytes else None,
            "processes": [
                {"id": p.id, "rss_bytes": r} for p, r in zip(self.processes, rss)
            ],
        }

    # ── Startup gate ─────────────────────────────────────────────
    def check_startup(self) -> None:
        """Verify the freshly-spawned pool fits within the startup allowance.

        Raises :class:`MemoryBudgetError` if the baseline footprint already
        exceeds ``startup_fraction`` of the budget — there would be no headroom
        for the caches to grow, so the budget is unworkable as configured."""
        rss, total = self._read()
        if any(r is None for r in rss):
            logger.warning(
                "Memory budget: could not read RSS for %d/%d bridge(s) at startup "
                "(psutil missing or process unreadable); startup fit unverified.",
                sum(r is None for r in rss), len(rss),
            )
        if total > self.startup_bytes:
            raise MemoryBudgetError(
                f"Lean pool startup footprint {_human(total)} exceeds "
                f"{self.startup_fraction:.0%} of the {_human(self.budget_bytes)} "
                f"budget ({_human(self.startup_bytes)}). Reduce the number of "
                f"processes or raise the budget."
            )
        logger.info(
            "Memory budget: startup footprint %s / %s budget (%.0f%%); "
            "will restart the hungriest bridge past %s.",
            _human(total), _human(self.budget_bytes),
            100 * total / self.budget_bytes, _human(self.high_water_bytes),
        )

    # ── Monitor lifecycle ────────────────────────────────────────
    def start(self) -> None:
        """Start the background monitor thread (idempotent)."""
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="lean-mem-monitor", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the monitor thread (idempotent). Does not restart any bridge."""
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=5)

    def _run(self) -> None:
        # `Event.wait(interval)` returns True only when stop() is signalled, so
        # this both paces the loop and lets stop() interrupt the idle wait.
        while not self._stop.wait(self.interval):
            try:
                self._tick()
            except Exception:  # a monitor must never die on a transient error
                logger.exception("Memory monitor tick failed")

    def _tick(self) -> None:
        """One monitoring pass: if the pool is over the high-water mark and we
        are not in the post-restart cooldown, restart the hungriest bridge."""
        rss, total = self._read()
        if total < self.high_water_bytes:
            return
        if time.monotonic() - self._last_restart < self.min_restart_interval:
            return  # still cooling down from the last restart

        hungriest: "LeanInterface | None" = None
        hungriest_rss = -1
        for proc, value in zip(self.processes, rss):
            if value is not None and value > hungriest_rss:
                hungriest, hungriest_rss = proc, value
        if hungriest is None:
            return  # nothing readable to act on

        logger.warning(
            "Memory budget: pool at %s crossed high-water %s of %s; restarting "
            "hungriest bridge id=%s (%s).",
            _human(total), _human(self.high_water_bytes),
            _human(self.budget_bytes), hungriest.id, _human(hungriest_rss),
        )
        # Mark the restart *before* it runs: restart() blocks until the fresh
        # process is up, and we want the cooldown measured from when relief began.
        self._last_restart = time.monotonic()
        try:
            hungriest.restart()
        except Exception:
            logger.exception(
                "Memory budget: restart of bridge id=%s failed", hungriest.id
            )
