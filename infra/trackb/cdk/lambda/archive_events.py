"""EventBridge -> S3 archive for AORTS domain events (one object per event, idempotent key)."""

import json
import os

import boto3

s3 = boto3.client("s3")


def handler(event, _context):
    detail = event.get("detail", {})
    event_id = detail.get("event_id") or event.get("id")
    day = (detail.get("timestamp") or event.get("time", ""))[:10] or "unknown"
    # Same event id -> same key: redelivered events overwrite identical content (idempotent).
    s3.put_object(
        Bucket=os.environ["BUCKET"],
        Key=f"events/{day}/{event_id}.json",
        Body=json.dumps(detail).encode(),
        ServerSideEncryption="AES256",
    )
    return {"archived": event_id}
