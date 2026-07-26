"""S3-compatible object storage for uploaded reconciliation files.

Uses the plain `s3` boto3 client with an optional custom endpoint so the
same code path works against AWS S3 in production and a self-hosted
S3-compatible store (MinIO) locally.
"""
from __future__ import annotations

import os

import boto3
from botocore.client import Config as BotoConfig


def bucket_name() -> str:
    return os.environ["RECONOPS_S3_BUCKET"]


def _client():
    endpoint = os.environ.get("RECONOPS_S3_ENDPOINT_URL") or None
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=os.environ.get("RECONOPS_S3_REGION", "us-east-1"),
        aws_access_key_id=os.environ.get("RECONOPS_S3_ACCESS_KEY_ID") or None,
        aws_secret_access_key=os.environ.get("RECONOPS_S3_SECRET_ACCESS_KEY") or None,
        config=BotoConfig(s3={"addressing_style": "path"}) if endpoint else None,
    )


def upload_key_for(account_id: str, job_id: str, side: str, filename: str) -> str:
    safe_name = filename.replace("/", "_").replace("\\", "_")
    return f"accounts/{account_id}/jobs/{job_id}/{side}_{safe_name}"


def put_object(key: str, data: bytes) -> None:
    _client().put_object(
        Bucket=bucket_name(), Key=key, Body=data, ServerSideEncryption="AES256",
    )


def delete_object(key: str) -> None:
    _client().delete_object(Bucket=bucket_name(), Key=key)
