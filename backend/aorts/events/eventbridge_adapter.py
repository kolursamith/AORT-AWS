"""Amazon EventBridge sink for domain events (optional; enabled with AORTS_EVENT_SINK=eventbridge)."""

from __future__ import annotations

import json


class EventBridgeSink:
    name = "eventbridge"

    def __init__(self, bus_name: str, region: str, client=None, source: str = "aorts.trackb"):
        if client is None:
            import boto3

            client = boto3.client("events", region_name=region)
        self.client = client
        self.bus = bus_name
        self.source = source

    def publish(self, events: list[dict]) -> list[str]:
        """Publish up to 10 events; returns the event ids that FAILED (to be retried by the relay)."""
        entries = [
            {
                "Source": self.source,
                "DetailType": e["event_type"],
                "Detail": json.dumps(e, default=str),
                "EventBusName": self.bus,
            }
            for e in events
        ]
        resp = self.client.put_events(Entries=entries)
        failed = []
        for event, result in zip(events, resp.get("Entries", []), strict=False):
            if result.get("ErrorCode"):
                failed.append(event["event_id"])
        return failed
