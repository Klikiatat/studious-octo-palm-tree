"""
Server-side Firestore logging (Admin SDK). Client rules deny all access;
only this backend can read/write when credentials are configured.
"""

from __future__ import annotations

import base64
import io
import json
import os
from typing import Any, Optional

import firebase_admin
from firebase_admin import credentials, firestore
from PIL import Image

PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "klikitat-staging")
DATABASE_ID = os.environ.get("FIREBASE_DATABASE_ID", "memoro")

# Firestore field value limit is ~1 MiB; stay under for base64 strings.
_MAX_B64_CHARS = 900_000

_firestore_client: Optional[firestore.Client] = None
_firestore_init_failed = False


_DEBUG_LOG = os.path.join(os.path.dirname(__file__), ".cursor", "debug-6bcec8.log")

def _dlog(msg, data=None, hyp=""):
    # #region agent log
    import time as _t
    try:
        os.makedirs(os.path.dirname(_DEBUG_LOG), exist_ok=True)
        entry = json.dumps({"sessionId":"6bcec8","timestamp":int(_t.time()*1000),"location":"firestore_logger.py","message":msg,"data":data or {},"hypothesisId":hyp})
        with open(_DEBUG_LOG, "a") as f:
            f.write(entry + "\n")
    except Exception:
        pass
    # #endregion


def _client() -> Optional[firestore.Client]:
    global _firestore_client, _firestore_init_failed
    if _firestore_init_failed:
        _dlog("_client: returning None (init previously failed)", hyp="H4")
        return None
    if _firestore_client is not None:
        return _firestore_client
    if firebase_admin._apps:
        _firestore_client = firestore.client(database_id=DATABASE_ID)
        return _firestore_client

    sa_json_raw = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
    gac_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    _dlog("_client: env check", {
        "FIREBASE_SERVICE_ACCOUNT_JSON_set": bool(sa_json_raw),
        "FIREBASE_SERVICE_ACCOUNT_JSON_len": len(sa_json_raw) if sa_json_raw else 0,
        "FIREBASE_SERVICE_ACCOUNT_JSON_first50": (sa_json_raw or "")[:50],
        "GOOGLE_APPLICATION_CREDENTIALS": gac_path or "(not set)",
        "GAC_file_exists": bool(gac_path and os.path.isfile(gac_path)),
        "PROJECT_ID": PROJECT_ID,
        "DATABASE_ID": DATABASE_ID,
    }, hyp="H1,H3,H5")

    try:
        if sa_json_raw:
            try:
                parsed = json.loads(sa_json_raw)
                _dlog("_client: SA JSON parsed ok", {"keys": list(parsed.keys()), "project_id_in_json": parsed.get("project_id","(missing)")}, hyp="H2")
            except json.JSONDecodeError as je:
                _dlog("_client: SA JSON parse FAILED", {"error": str(je), "first100": sa_json_raw[:100]}, hyp="H2")
                raise
            cred = credentials.Certificate(parsed)
        else:
            if gac_path and os.path.isfile(gac_path):
                _dlog("_client: using file creds", {"path": gac_path}, hyp="H3")
                cred = credentials.Certificate(gac_path)
            else:
                _dlog("_client: falling back to ApplicationDefault", hyp="H4")
                cred = credentials.ApplicationDefault()
        firebase_admin.initialize_app(cred, {"projectId": PROJECT_ID})
        _firestore_client = firestore.client(database_id=DATABASE_ID)
        _dlog("_client: SUCCESS", {"project": PROJECT_ID, "db": DATABASE_ID}, hyp="ALL")
        print(f"[firestore] Initialized for project {PROJECT_ID}, database {DATABASE_ID}")
        return _firestore_client
    except Exception as e:
        _firestore_init_failed = True
        _dlog("_client: INIT FAILED", {"error": str(e), "error_type": type(e).__name__}, hyp="H2,H4")
        print(f"[firestore] Disabled (init failed): {e}")
        return None


def _pil_to_jpeg_b64(img: Image.Image, max_side: int = 1280, quality: int = 82) -> str:
    im = img.copy()
    if im.mode != "RGB":
        im = im.convert("RGB")
    w, h = im.size
    if max(w, h) > max_side:
        im.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=quality, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _strip_data_url(b64: str) -> str:
    s = b64.strip()
    if s.startswith("data:") and "," in s:
        return s.split(",", 1)[-1]
    return s


def _write_output_image(run_ref: firestore.DocumentReference, raw_b64: str) -> None:
    """Write generated image to output_image/main; chunk if necessary."""
    col = run_ref.collection("output_image")
    ref = col.document("main")
    if len(raw_b64) <= _MAX_B64_CHARS:
        ref.set({"image_base64": raw_b64, "stored": True, "encoding": "single"})
        return

    chunk_size = 800_000
    parts = [raw_b64[i : i + chunk_size] for i in range(0, len(raw_b64), chunk_size)]
    if len(parts) > 10:
        ref.set(
            {
                "stored": False,
                "reason": "output_too_large_for_firestore",
                "approx_chars": len(raw_b64),
            }
        )
        return

    payload: dict[str, Any] = {"stored": True, "encoding": "chunked", "chunks": len(parts)}
    for i, c in enumerate(parts):
        payload[f"image_base64_{i}"] = c
    ref.set(payload)


def log_run(
    run_type: str,
    *,
    summary: str = "",
    filenames: Optional[list[str]] = None,
    images: Optional[list[Image.Image]] = None,
    **fields: Any,
) -> Optional[str]:
    """
    Persist one logical API run. Returns Firestore document id, or None if logging skipped/failed.
    """
    db = _client()
    if not db:
        return None

    filenames = filenames or []
    images = images or []

    run_ref = db.collection("runs").document()
    run_id = run_ref.id

    data: dict[str, Any] = {
        "type": run_type,
        "created_at": firestore.SERVER_TIMESTAMP,
        "summary": summary or "",
        "filenames": filenames,
        "input_image_count": len(images),
    }

    scalar_keys = (
        "story",
        "suggestion",
        "style_description",
        "style_name",
        "image_prompt",
        "model_output_text",
        "excluded",
        "generation_time",
        "total_time",
        "detail",
        "error",
        "remaining",
    )
    for key in scalar_keys:
        if key in fields and fields[key] is not None:
            data[key] = fields[key]

    output_b64 = fields.get("output_image_base64")
    data["has_output_image"] = bool(output_b64)

    batch = db.batch()
    batch.set(run_ref, data)

    for i, img in enumerate(images):
        ref = run_ref.collection("input_images").document(str(i))
        fn = filenames[i] if i < len(filenames) else ""
        try:
            jpeg_b64 = _pil_to_jpeg_b64(img)
            if len(jpeg_b64) > _MAX_B64_CHARS:
                batch.set(
                    ref,
                    {"filename": fn, "stored": False, "reason": "jpeg_too_large"},
                )
            else:
                batch.set(
                    ref,
                    {"filename": fn, "jpeg_base64": jpeg_b64, "stored": True},
                )
        except Exception as e:
            batch.set(ref, {"filename": fn, "stored": False, "reason": str(e)})

    try:
        batch.commit()
    except Exception as e:
        print(f"[firestore] batch.commit failed: {e}")
        return None

    if output_b64:
        try:
            _write_output_image(run_ref, _strip_data_url(output_b64))
        except Exception as e:
            print(f"[firestore] output_image write failed: {e}")
            run_ref.collection("output_image").document("main").set(
                {"stored": False, "reason": str(e)}
            )

    return run_id


def _reassemble_output(doc_dict: dict[str, Any]) -> Optional[str]:
    if not doc_dict.get("stored"):
        return None
    if doc_dict.get("image_base64"):
        return doc_dict["image_base64"]
    n = int(doc_dict.get("chunks") or 0)
    if n and doc_dict.get("image_base64_0") is not None:
        return "".join(doc_dict.get(f"image_base64_{i}") or "" for i in range(n))
    return None


def fetch_run(run_id: str) -> Optional[tuple[dict[str, Any], list[dict], Optional[str]]]:
    """Returns (main_doc_dict, input_image_dicts, output_base64_or_none)."""
    db = _client()
    if not db:
        return None
    ref = db.collection("runs").document(run_id)
    doc = ref.get()
    if not doc.exists:
        return None
    main = doc.to_dict() or {}
    main["id"] = doc.id

    inputs: list[dict[str, Any]] = []
    n = int(main.get("input_image_count") or 0)
    for i in range(n):
        sub = ref.collection("input_images").document(str(i)).get()
        if sub.exists:
            inputs.append(sub.to_dict() or {})

    out_b64: Optional[str] = None
    out_doc = ref.collection("output_image").document("main").get()
    if out_doc.exists:
        out_b64 = _reassemble_output(out_doc.to_dict() or {})

    return main, inputs, out_b64


def list_runs(limit: int = 100) -> list[dict[str, Any]]:
    db = _client()
    if not db:
        return []
    q = (
        db.collection("runs")
        .order_by("created_at", direction=firestore.Query.DESCENDING)
        .limit(limit)
    )
    out = []
    for doc in q.stream():
        row = doc.to_dict() or {}
        row["id"] = doc.id
        out.append(row)
    return out
