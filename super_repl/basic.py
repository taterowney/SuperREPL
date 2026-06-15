from __future__ import annotations

from dataclasses import dataclass, field
from time import time
from uuid import uuid4


@dataclass
class Request:
    method: str
    params: dict
    # Full set of transitive imports the request needs (used for clustering and
    # routing). Empty means "no specific imports".
    imports: frozenset[str] = field(default_factory=frozenset)
    time_received: float = field(default_factory=time)
    cluster: int | None = None                    # assigned by the policy
    uuid: str = field(default_factory=uuid4)
