"""Export the running stack's metrics summary + transactions (JSON) and optionally upload to S3.

python -m backend.aorts.experiments.export_metrics            # results/processed/export-*.json
python -m backend.aorts.experiments.export_metrics --s3       # also upload when AORTS_S3_RESULTS_BUCKET is set
"""

from __future__ import annotations

import argparse
from pathlib import Path

from backend.aorts.experiments.common import http_client


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--s3", action="store_true")
    args = p.parse_args()
    client, settings = http_client()
    r = client.request(
        "control",
        "GET",
        "/api/v1/admin/export",
        as_identity="LAB",
        headers={"X-AORTS-Admin-Token": settings.admin_token.get_secret_value()},
    )
    r.raise_for_status()
    body = r.json()
    print({"saved_to": body["saved_to"], "transactions": body["transactions"]})
    if args.s3:
        if not settings.s3_results_bucket:
            raise SystemExit("AORTS_S3_RESULTS_BUCKET is not set")
        import boto3

        path = Path(body["saved_to"])
        boto3.client("s3", region_name=settings.aws_region).upload_file(
            str(path),
            settings.s3_results_bucket,
            f"exports/{path.name}",
            ExtraArgs={"ServerSideEncryption": "aws:kms"},
        )
        print(f"uploaded s3://{settings.s3_results_bucket}/exports/{path.name}")


if __name__ == "__main__":
    main()
