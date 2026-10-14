"""In-process prefilter; the database unique constraint is authoritative."""

import threading


class ReplayGuard:
    def __init__(self) -> None:
        self._seen: set[tuple[str, int]] = set()
        self._lock = threading.Lock()

    def accept(self, operation_id: str, version: int) -> bool:
        key = (operation_id, version)
        with self._lock:
            if key in self._seen:
                return False
            self._seen.add(key)
            return True
