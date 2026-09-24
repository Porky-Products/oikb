"""Process-local duplicate failure counts for one server/KB destination."""

FailureKey = tuple[str, str, str]  # destination path, filename, source checksum
DUPLICATE_FAILURE_LIMIT = 3


class DuplicateFailureTracker:
    """Owned by the daemon; updated by the sync coordinator, never workers.

    The daemon serializes runs for each KB. Only currently pending versions
    are retained, so changed, removed, or successfully synced files reset.
    """

    def __init__(self) -> None:
        self._counts: dict[FailureKey, int] = {}

    def retain_pending(self, keys: set[FailureKey]) -> None:
        self._counts = {key: count for key, count in self._counts.items() if key in keys}

    def blocked_keys(self) -> set[FailureKey]:
        return {key for key, count in self._counts.items() if count >= DUPLICATE_FAILURE_LIMIT}

    def record_duplicate(self, key: FailureKey) -> bool:
        count = min(self._counts.get(key, 0) + 1, DUPLICATE_FAILURE_LIMIT)
        self._counts[key] = count
        return count == DUPLICATE_FAILURE_LIMIT

    def reset(self, key: FailureKey) -> None:
        self._counts.pop(key, None)

    def clear(self) -> None:
        self._counts.clear()
