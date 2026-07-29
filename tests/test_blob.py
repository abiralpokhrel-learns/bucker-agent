"""Blob store tests (step 9)."""

from __future__ import annotations

import pytest

from bucker.core.blob import BlobStore


def test_round_trip(blob_root):
    store = BlobStore(blob_root)
    ref = store.put(b"hello durable world")
    assert store.get(ref) == b"hello durable world"
    assert ref.startswith("sha256:")


def test_same_content_same_ref_dedup(blob_root):
    store = BlobStore(blob_root)
    a = store.put("identical payload")
    b = store.put("identical payload")
    assert a == b


def test_different_content_different_ref(blob_root):
    store = BlobStore(blob_root)
    assert store.put("one") != store.put("two")


def test_json_round_trip_and_key_order_stability(blob_root):
    store = BlobStore(blob_root)
    ref1 = store.put_json({"b": 2, "a": 1})
    ref2 = store.put_json({"a": 1, "b": 2})
    assert ref1 == ref2, "key order must not change the content ref"
    assert store.get_json(ref1) == {"a": 1, "b": 2}


def test_missing_blob_raises(blob_root):
    store = BlobStore(blob_root)
    with pytest.raises(KeyError):
        store.get("sha256:" + "0" * 64)


def test_verify_detects_tampering(blob_root):
    """A tampered archive must be detectable — replay depends on it."""
    store = BlobStore(blob_root)
    ref = store.put("original model response")
    assert store.verify(ref) is True

    path = store._path_for(BlobStore._strip(ref))
    path.write_bytes(b"tampered model response")

    assert store.verify(ref) is False


def test_verify_false_for_missing(blob_root):
    assert BlobStore(blob_root).verify("sha256:" + "f" * 64) is False
