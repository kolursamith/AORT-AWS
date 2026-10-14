from concurrent.futures import ThreadPoolExecutor

from backend.security.rate_limits import PerBankRateLimiter


def test_concurrent_requests_cannot_exceed_quota() -> None:
    limiter = PerBankRateLimiter(quota=10)
    with ThreadPoolExecutor(max_workers=20) as executor:
        results = list(executor.map(lambda _: limiter.check("bank-1", now=0).accepted, range(100)))
    assert sum(results) == 10
