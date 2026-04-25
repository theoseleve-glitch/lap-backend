"""T105 — R2BlobStore integration tests (S3-compatible API via moto).

Cloudflare R2 speaks the S3 API, so the production client is boto3
pointed at R2's endpoint. Tests stand up an in-process S3 mock with
moto so we can exercise the same code paths CI cannot reach with real
R2 creds. T106 wires this implementation into the ingest endpoint.
"""

import boto3
import pytest
from moto import mock_aws

from infra.storage import BlobStoreProtocol, R2BlobStore

R2_ENDPOINT = "https://accountid.r2.cloudflarestorage.com"
BUCKET = "lap-telemetry-test"


def _make_bucket() -> None:
    s3 = boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )
    s3.create_bucket(Bucket=BUCKET)


@mock_aws
def test_r2_implements_blob_store_protocol() -> None:
    _make_bucket()
    store: BlobStoreProtocol = R2BlobStore(
        bucket=BUCKET,
        endpoint_url=None,
        access_key_id="testing",
        secret_access_key="testing",
    )
    assert hasattr(store, "put")
    assert hasattr(store, "get")
    assert hasattr(store, "sign_url")


@mock_aws
def test_put_then_get_round_trips_bytes() -> None:
    _make_bucket()
    store = R2BlobStore(
        bucket=BUCKET,
        endpoint_url=None,
        access_key_id="testing",
        secret_access_key="testing",
    )
    store.put("session-x/samples.bin", b"payload-XYZ")
    assert store.get("session-x/samples.bin") == b"payload-XYZ"


@mock_aws
def test_get_unknown_key_returns_none() -> None:
    _make_bucket()
    store = R2BlobStore(
        bucket=BUCKET,
        endpoint_url=None,
        access_key_id="testing",
        secret_access_key="testing",
    )
    assert store.get("does-not-exist") is None


@mock_aws
def test_put_overwrites_existing_value() -> None:
    _make_bucket()
    store = R2BlobStore(
        bucket=BUCKET,
        endpoint_url=None,
        access_key_id="testing",
        secret_access_key="testing",
    )
    store.put("k", b"old")
    store.put("k", b"new")
    assert store.get("k") == b"new"


@mock_aws
def test_distinct_keys_persist_independently() -> None:
    _make_bucket()
    store = R2BlobStore(
        bucket=BUCKET,
        endpoint_url=None,
        access_key_id="testing",
        secret_access_key="testing",
    )
    store.put("a", b"A")
    store.put("b", b"B")
    assert store.get("a") == b"A"
    assert store.get("b") == b"B"


@mock_aws
def test_sign_url_returns_https_url_for_object() -> None:
    _make_bucket()
    store = R2BlobStore(
        bucket=BUCKET,
        endpoint_url=None,
        access_key_id="testing",
        secret_access_key="testing",
    )
    store.put("k", b"x")
    url = store.sign_url("k", expires_in=300)
    # moto signs via the default S3 endpoint; R2 in production uses
    # the configured endpoint_url. Either way the URL must reference
    # the bucket and key and carry a signature query parameter.
    assert isinstance(url, str)
    assert BUCKET in url
    assert "k" in url
    assert "Signature=" in url or "X-Amz-Signature=" in url


@mock_aws
def test_sign_url_respects_expires_in_argument() -> None:
    _make_bucket()
    store = R2BlobStore(
        bucket=BUCKET,
        endpoint_url=None,
        access_key_id="testing",
        secret_access_key="testing",
    )
    store.put("k", b"x")
    short = store.sign_url("k", expires_in=60)
    long_ = store.sign_url("k", expires_in=3600)
    assert "Expires=60" in short or "X-Amz-Expires=60" in short
    assert "Expires=3600" in long_ or "X-Amz-Expires=3600" in long_


@pytest.mark.parametrize("bad_expires", [0, -1, -3600])
def test_sign_url_rejects_non_positive_expires(bad_expires: int) -> None:
    with mock_aws():
        _make_bucket()
        store = R2BlobStore(
            bucket=BUCKET,
            endpoint_url=None,
            access_key_id="testing",
            secret_access_key="testing",
        )
        with pytest.raises(ValueError):
            store.sign_url("k", expires_in=bad_expires)
