"""Amazon SQS dispatch queue.

SQS standard queues deliver at-least-once and may reorder. Safety does not rely
on SQS: every job carries ``expected_version`` and the ledger rejects a stale or
repeated job with STALE_VERSION. Messages that keep failing move to the DLQ via
the queue's redrive policy (maxReceiveCount set in the CDK stack).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from backend.aorts.contracts.events import DispatchJob

logger = logging.getLogger("aorts.dispatch.sqs")


class SqsQueue:
    def __init__(self, queue_url: str, region: str, workers: int = 4, client=None):
        if client is None:
            import boto3

            client = boto3.client("sqs", region_name=region)
        self.client = client
        self.url = queue_url
        self.workers = workers
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def send(self, job: DispatchJob) -> None:
        self.client.send_message(
            QueueUrl=self.url,
            MessageBody=job.model_dump_json(),
            MessageAttributes={
                "transaction_id": {"DataType": "String", "StringValue": job.transaction_id}
            },
        )

    def depth(self) -> int:
        attrs = self.client.get_queue_attributes(
            QueueUrl=self.url, AttributeNames=["ApproximateNumberOfMessages"]
        )
        return int(attrs["Attributes"].get("ApproximateNumberOfMessages", 0))

    def _poll_once(self, handler: Callable[[DispatchJob], None]) -> int:
        resp = self.client.receive_message(
            QueueUrl=self.url,
            MaxNumberOfMessages=10,
            WaitTimeSeconds=10,
            MessageAttributeNames=["All"],
        )
        n = 0
        for msg in resp.get("Messages", []):
            try:
                handler(DispatchJob.model_validate_json(msg["Body"]))
            except Exception:  # noqa: BLE001 - leave it on the queue; redrive -> DLQ after maxReceiveCount
                logger.exception("SQS job failed; message will become visible again")
                continue
            self.client.delete_message(QueueUrl=self.url, ReceiptHandle=msg["ReceiptHandle"])
            n += 1
        return n

    def start(self, handler: Callable[[DispatchJob], None]) -> None:
        def _work():
            while not self._stop.is_set():
                try:
                    self._poll_once(handler)
                except Exception:  # noqa: BLE001
                    logger.exception("SQS poll failed")
                    self._stop.wait(2)

        for i in range(self.workers):
            t = threading.Thread(target=_work, name=f"sqs-worker-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def drain(self, handler: Callable[[DispatchJob], None]) -> int:
        return self._poll_once(handler)

    def stop(self) -> None:
        self._stop.set()
