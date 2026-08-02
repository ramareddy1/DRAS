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


def get_object(key: str) -> bytes:
    """Read one object's bytes. Raises botocore ClientError if absent."""
    response = _client().get_object(Bucket=bucket_name(), Key=key)
    return response["Body"].read()


def delete_object(key: str) -> None:
    _client().delete_object(Bucket=bucket_name(), Key=key)


def delete_prefix(account_id: str) -> None:
    """Delete every object under accounts/{account_id}/ — used for a full
    account purge, since one account's uploads span every job it has ever
    run. Raises Exception if S3 reports any per-object errors during a
    batch delete_objects call, rather than silently treating a partial
    delete as success."""
    client = _client()
    prefix = f"accounts/{account_id}/"
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket_name(), Prefix=prefix):
        keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        if keys:
            response = client.delete_objects(Bucket=bucket_name(), Delete={"Objects": keys})
            if "Errors" in response and response["Errors"]:
                raise Exception(f"Partial delete failure for account {account_id}: {response['Errors']}")
