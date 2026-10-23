"""DynamoDB implementation of :class:`VersionedStore` (conditional writes).

Table schema (created by the CDK stack): partition key ``pk`` (S); attributes
``version`` (N) and ``data`` (S, JSON). Credentials come from the ECS task role;
never from hard-coded keys.
"""

from __future__ import annotations

import json

from backend.aorts.adapters.twin_state import VersionConflict


class DynamoVersionedStore:
    def __init__(self, table_name: str, region: str, client=None):
        if client is None:
            import boto3

            client = boto3.client("dynamodb", region_name=region)
        self.client = client
        self.table = table_name

    def get(self, key: str) -> tuple[int, dict] | None:
        resp = self.client.get_item(
            TableName=self.table, Key={"pk": {"S": key}}, ConsistentRead=True
        )
        item = resp.get("Item")
        if not item:
            return None
        return int(item["version"]["N"]), json.loads(item["data"]["S"])

    def put(self, key: str, data: dict, expected_version: int | None) -> int:
        from botocore.exceptions import ClientError

        new_version = (expected_version or 0) + 1
        kwargs = {
            "TableName": self.table,
            "Item": {
                "pk": {"S": key},
                "version": {"N": str(new_version)},
                "data": {"S": json.dumps(data, default=str)},
            },
        }
        if expected_version is None:
            kwargs["ConditionExpression"] = "attribute_not_exists(pk)"
        else:
            kwargs["ConditionExpression"] = "version = :expected"
            kwargs["ExpressionAttributeValues"] = {":expected": {"N": str(expected_version)}}
        try:
            self.client.put_item(**kwargs)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                current = self.get(key)
                raise VersionConflict(
                    key, expected_version, current[0] if current else None
                ) from exc
            raise
        return new_version

    def items(self, prefix: str = "") -> dict[str, tuple[int, dict]]:
        out: dict[str, tuple[int, dict]] = {}
        paginator = self.client.get_paginator("scan")
        for page in paginator.paginate(TableName=self.table):
            for item in page.get("Items", []):
                pk = item["pk"]["S"]
                if pk.startswith(prefix):
                    out[pk] = (int(item["version"]["N"]), json.loads(item["data"]["S"]))
        return out
