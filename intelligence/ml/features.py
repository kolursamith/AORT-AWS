"""Pure feature extraction shared by future training and inference code."""

from contracts.v1.models import RouteTelemetry


def route_features(item: RouteTelemetry) -> dict[str, float]:
    offered = item.payment_rps + item.check_rps
    return {
        "offered_rps": offered,
        "utilization_proxy": offered / max(item.observed_throughput_rps, 1e-9),
        "p95_latency_ms": item.p95_latency_ms,
        "timeout_rate": item.timeout_rate,
        "queue_depth": float(item.queue_depth),
    }
