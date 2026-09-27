"""ObjectStore adapter (TRD §3.5, database.md Part E, API-Guide §12). MinIO locally, S3 India region in prod.

Pre-signed PUT valid 10 min with exact content-type and length; pre-signed GET valid 5 min with
`Content-Disposition: attachment` (SF-16).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import boto3
from botocore.config import Config

from app.core.config import settings


@lru_cache(maxsize=2)
def _client(public: bool) -> Any:
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_public_endpoint if public else settings.s3_endpoint,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name=settings.s3_region,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def presign_put(bucket: str, key: str, content_type: str, size: int) -> str:
    return _client(True).generate_presigned_url(
        "put_object", Params={"Bucket": bucket, "Key": key, "ContentType": content_type, "ContentLength": size},
        ExpiresIn=600)


def presign_get(bucket: str, key: str) -> str:
    return _client(True).generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": key, "ResponseContentDisposition": "attachment"},
        ExpiresIn=300)


def head(bucket: str, key: str) -> dict[str, Any] | None:
    try:
        return _client(False).head_object(Bucket=bucket, Key=key)
    except Exception:  # noqa: BLE001
        return None


def read_prefix(bucket: str, key: str, n: int = 16) -> bytes:
    return _client(False).get_object(Bucket=bucket, Key=key, Range=f"bytes=0-{n - 1}")["Body"].read()


def ensure_buckets() -> None:
    c = _client(False)
    for name in ("attachments", "exports", "tiles"):
        bucket = settings.bucket(name)
        try:
            c.head_bucket(Bucket=bucket)
        except Exception:  # noqa: BLE001
            c.create_bucket(Bucket=bucket)
