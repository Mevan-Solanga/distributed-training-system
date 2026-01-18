"""S3/MinIO utilities for distributed training."""
import os
import json
import boto3
from pathlib import Path
from typing import Optional


def get_s3_client():
    """Get S3 client, with MinIO support if endpoint is set."""
    s3_endpoint = os.getenv("S3_ENDPOINT")  # e.g., http://localhost:9000
    s3_access_key = os.getenv("S3_ACCESS_KEY", "minioadmin")
    s3_secret_key = os.getenv("S3_SECRET_KEY", "minioadmin")

    if s3_endpoint:
        # MinIO mode
        return boto3.client(
            "s3",
            endpoint_url=s3_endpoint,
            aws_access_key_id=s3_access_key,
            aws_secret_access_key=s3_secret_key,
            region_name="us-east-1",
        )
    else:
        # AWS S3 mode
        return boto3.client("s3")


def download_shard(bucket: str, shard_key: str, local_path: Path) -> bool:
    """Download a shard from S3/MinIO to local disk."""
    try:
        client = get_s3_client()
        local_path.parent.mkdir(parents=True, exist_ok=True)
        client.download_file(bucket, shard_key, str(local_path))
        return True
    except Exception as e:
        print(f"[s3] failed to download {bucket}/{shard_key}: {e}")
        return False


def upload_checkpoint(bucket: str, checkpoint_path: Path, remote_key: str) -> bool:
    """Upload a checkpoint to S3/MinIO."""
    try:
        client = get_s3_client()
        client.upload_file(str(checkpoint_path), bucket, remote_key)
        return True
    except Exception as e:
        print(f"[s3] failed to upload {remote_key}: {e}")
        return False


def download_checkpoint(bucket: str, remote_key: str, local_path: Path) -> bool:
    """Download a checkpoint from S3/MinIO."""
    try:
        client = get_s3_client()
        local_path.parent.mkdir(parents=True, exist_ok=True)
        client.download_file(bucket, remote_key, str(local_path))
        return True
    except Exception as e:
        print(f"[s3] failed to download {bucket}/{remote_key}: {e}")
        return False


def list_shards(bucket: str, prefix: str = "shards/") -> list:
    """List all shard keys in S3/MinIO."""
    try:
        client = get_s3_client()
        response = client.list_objects_v2(Bucket=bucket, Prefix=prefix)
        shards = []
        for obj in response.get("Contents", []):
            key = obj["Key"]
            if key.endswith(".txt"):
                shards.append(key)
        return sorted(shards)
    except Exception as e:
        print(f"[s3] failed to list shards: {e}")
        return []


def use_s3() -> bool:
    """Check if S3 is enabled."""
    return os.getenv("USE_S3", "0").lower() in ("1", "true", "yes")
