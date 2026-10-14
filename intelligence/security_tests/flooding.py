"""Local status-flood input generator; network execution is intentionally separate."""

from intelligence.security_tests._targets import require_isolated_target


def build_requests(target: str, count: int) -> list[dict[str, str]]:
    require_isolated_target(target)
    if not 1 <= count <= 10_000:
        raise ValueError("count must be between 1 and 10000")
    return [
        {"transaction_id": f"flood-{index}", "bank_id": "compromised-mock"}
        for index in range(count)
    ]
