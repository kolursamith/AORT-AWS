"""Generate duplicate, stale, and reordered event sequences for local tests."""


def duplicate(events: list[dict]) -> list[dict]:
    return [item for event in events for item in (dict(event), dict(event))]


def reverse(events: list[dict]) -> list[dict]:
    return list(reversed([dict(event) for event in events]))
