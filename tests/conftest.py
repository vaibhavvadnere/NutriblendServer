"""
Test setup: a local S3 server (moto) stands in for Cloudflare R2 and an
in-memory MongoDB (mongomock-motor) stands in for Atlas, so these tests never
touch real cloud resources.

    pip install -r requirements.txt -r requirements-dev.txt
    python -m pytest tests -q
"""

import os
import socket

import pytest


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


_PORT = _free_port()
# Must be set before `app` is imported (settings are read at import time).
os.environ.update(
    {
        "ENV": "development",
        "STORAGE_PROVIDER": "r2",
        "R2_ENDPOINT_URL": f"http://127.0.0.1:{_PORT}",
        "R2_ACCOUNT_ID": "",
        "R2_ACCESS_KEY_ID": "test-key",
        "R2_SECRET_ACCESS_KEY": "test-secret",
        "R2_BUCKET": "nutriblend-test",
        "UPLOAD_CHUNK_SIZE_MB": "5",
        "VIDEO_OPTIMIZE_ENABLED": "false",
        "MEDIA_URL_SECRET": "test-media-secret",
        "MONGO_URI": "mongodb://127.0.0.1:1",
        "TRUST_PROXY_HEADERS": "false",
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }
)


@pytest.fixture(scope="session")
def s3_server():
    from moto.server import ThreadedMotoServer

    server = ThreadedMotoServer(ip_address="127.0.0.1", port=_PORT)
    server.start()
    import boto3

    boto3.client(
        "s3", endpoint_url=os.environ["R2_ENDPOINT_URL"], region_name="us-east-1",
        aws_access_key_id="test-key", aws_secret_access_key="test-secret",
    ).create_bucket(Bucket="nutriblend-test")
    yield os.environ["R2_ENDPOINT_URL"]
    server.stop()


@pytest.fixture()
def mongo(monkeypatch):
    """Swap every Mongo collection the media code uses for an in-memory one."""
    from mongomock_motor import AsyncMongoMockClient

    from app.core import database
    from app.repositories import video_repo

    db = AsyncMongoMockClient()["nutriblend_test"]
    monkeypatch.setattr(database, "db", db)
    monkeypatch.setattr(database, "videos_collection", db["videos"])
    monkeypatch.setattr(database, "storage_uploads_collection", db["storage_uploads"])
    monkeypatch.setattr(video_repo, "videos_collection", db["videos"])
    return db


@pytest.fixture()
def storage(s3_server, mongo):
    from app.providers import storage as storage_pkg

    storage_pkg._cached = None
    s = storage_pkg.get_storage()
    assert s.name == "r2"
    return s
