"""
Firestore integration tests (Admin SDK).

Requires credentials for project `klikitat-staging`:
  - `GOOGLE_APPLICATION_CREDENTIALS` → path to service account JSON, or
  - `FIREBASE_SERVICE_ACCOUNT_JSON` → raw JSON string

Run: pytest tests/test_firestore_connection.py -v
"""

from __future__ import annotations

import uuid

import pytest

from firestore_logger import PROJECT_ID, DATABASE_ID, _client, fetch_run, list_runs, log_run
from PIL import Image


@pytest.fixture(scope="module")
def db():
    client = _client()
    assert client is not None, (
        "Firestore client failed to initialize. "
        "Set GOOGLE_APPLICATION_CREDENTIALS or FIREBASE_SERVICE_ACCOUNT_JSON."
    )
    return client


def test_env_vars_loaded():
    assert PROJECT_ID, "FIREBASE_PROJECT_ID should be set"
    assert DATABASE_ID, "FIREBASE_DATABASE_ID should be set"


def test_client_initializes(db):
    """Admin SDK connects to Firestore."""
    assert db is not None


def test_client_singleton_returns_same_instance(db):
    assert _client() is db


def test_write_read_delete(db):
    """Write a document, read it back, then delete it and confirm it's gone."""
    doc_id = f"smoke_{uuid.uuid4().hex}"
    col = db.collection("connection_tests")
    ref = col.document(doc_id)
    payload = {"kind": "pytest_smoke", "value": 42, "tag": "write_read_delete"}

    ref.set(payload)
    try:
        snap = ref.get()
        assert snap.exists, "document should exist after set()"
        data = snap.to_dict()
        assert data["kind"] == "pytest_smoke"
        assert data["value"] == 42
        assert data["tag"] == "write_read_delete"
    finally:
        ref.delete()

    gone = ref.get()
    assert not gone.exists, "document should not exist after delete()"


def test_log_run_fetch_run_delete(db):
    """Full round-trip through log_run → fetch_run → verify → delete."""
    img = Image.new("RGB", (32, 24), color=(200, 100, 50))

    run_id = log_run(
        "test",
        summary="pytest log_run roundtrip",
        filenames=["tiny.jpg"],
        images=[img],
        suggestion={"style_name": "Test", "reasoning": "smoke"},
        total_time=0.01,
    )
    assert run_id is not None, "log_run returned None (Firestore write failed)"

    try:
        row = fetch_run(run_id)
        assert row is not None, "fetch_run returned None for a just-written run"
        main, inputs, out_b64 = row

        assert main["id"] == run_id
        assert main["summary"] == "pytest log_run roundtrip"
        assert main["type"] == "test"
        assert main["input_image_count"] == 1
        assert main["has_output_image"] is False

        assert len(inputs) == 1
        assert inputs[0]["stored"] is True
        assert inputs[0]["jpeg_base64"]

        assert out_b64 is None
    finally:
        _delete_run_recursive(db, run_id)

    # Confirm the run is gone
    assert fetch_run(run_id) is None, "run document should not exist after cleanup"


def test_list_runs_returns_list(db):
    runs = list_runs(limit=5)
    assert isinstance(runs, list)


def test_test_entry_create_and_delete(db):
    """Create a dedicated test entry, verify it appears in list_runs, then delete it."""
    img = Image.new("RGB", (16, 16), color=(0, 255, 0))
    tag = f"test_entry_{uuid.uuid4().hex[:8]}"

    run_id = log_run(
        "test_entry",
        summary=tag,
        filenames=["green.png"],
        images=[img],
        style_name="TestStyle",
        total_time=0.0,
    )
    assert run_id is not None

    try:
        # Fetch and validate
        row = fetch_run(run_id)
        assert row is not None
        main, inputs, _ = row
        assert main["type"] == "test_entry"
        assert main["summary"] == tag
        assert main["style_name"] == "TestStyle"
        assert len(inputs) == 1
        assert inputs[0]["stored"] is True

        # Verify it shows up in list_runs
        recent = list_runs(limit=20)
        ids = [r["id"] for r in recent]
        assert run_id in ids, "newly created test entry should appear in list_runs"
    finally:
        _delete_run_recursive(db, run_id)

    # Confirm deletion
    assert fetch_run(run_id) is None, "test entry should be deleted"
    recent_after = list_runs(limit=20)
    ids_after = [r["id"] for r in recent_after]
    assert run_id not in ids_after, "deleted entry should not appear in list_runs"


# ---------------------------------------------------------------------------

def _delete_run_recursive(db, run_id: str):
    """Delete a run document and its subcollections."""
    ref = db.collection("runs").document(run_id)
    batch = db.batch()
    n = 0
    for sub_name in ("input_images", "output_image"):
        for doc in ref.collection(sub_name).stream():
            batch.delete(doc.reference)
            n += 1
            if n >= 450:
                batch.commit()
                batch = db.batch()
                n = 0
    batch.delete(ref)
    batch.commit()
