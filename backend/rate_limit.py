import bootstrap_path  # noqa: F401

import time
from collections import defaultdict
from typing import DefaultDict, List

LIMIT = 30
WINDOW = 60

_buckets: DefaultDict[str, List[float]] = defaultdict(list)


def is_rate_limited(client_ip: str) -> bool:
    now = time.time()
    _buckets[client_ip] = [t for t in _buckets[client_ip] if now - t < WINDOW]
    if len(_buckets[client_ip]) >= LIMIT:
        return True
    _buckets[client_ip].append(now)
    return False
