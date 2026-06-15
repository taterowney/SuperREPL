from __future__ import annotations
import time
from contextlib import asynccontextmanager

from .basic import Request


class StatsTracker:
    tracked_stats = ["lean_processing_time", "import_module_time", "get_imports_time"]
    events_data = {stat: [] for stat in tracked_stats}
    tracked_events_window = 100
    
    @classmethod
    @asynccontextmanager
    async def profile(cls, label: str):
        start = time.perf_counter()
        try:
            yield
        finally:
            end = time.perf_counter()
            cls.events_data[label].append(end - start)
            if len(cls.events_data[label]) > cls.tracked_events_window:
                cls.events_data[label].pop(0)
    
    @classmethod
    @asynccontextmanager
    async def track_lean_processing_time(cls):
        async with cls.profile("lean_processing_time"):
            yield
    
    @classmethod
    def submit_lean_processing_time(cls, total_time: float, num_imports: int = 0, imports_total_time: float = 0.0) -> None:
        cls.events_data["lean_processing_time"].append(total_time - imports_total_time)
        if num_imports > 0 and imports_total_time > 0:
            cls.events_data["import_module_time"].append(imports_total_time)
            cls.events_data["get_imports_time"].append(imports_total_time / num_imports)
        if len(cls.events_data["lean_processing_time"]) > cls.tracked_events_window:
            cls.events_data["lean_processing_time"].pop(0)
        if len(cls.events_data["import_module_time"]) > cls.tracked_events_window:
            cls.events_data["import_module_time"].pop(0)
        if len(cls.events_data["get_imports_time"]) > cls.tracked_events_window:
            cls.events_data["get_imports_time"].pop(0)
    
    @classmethod
    def get_lean_processing_time(cls) -> float:
        """Estimated seconds to run one request (excluding imports). Currently a
        simple average; swap the body to change the estimator."""
        times = cls.events_data.get("lean_processing_time", [])
        return sum(times) / len(times) if times else 0.1

    @classmethod
    def get_imports_time(cls) -> float:
        """Estimated seconds to import one uncached module. Currently a simple
        average of observed per-miss import times; swap the body to change the
        estimator."""
        times = cls.events_data.get("get_imports_time", [])
        return sum(times) / len(times) if times else 1.0

