import base64
import io
import json
import os
import time

from dotenv import load_dotenv
from flask import Flask, jsonify, request, render_template
from google import genai
from google.genai import types
from PIL import Image

load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 4.5 * 1024 * 1024  # 4.5 MB (Vercel limit)

def _firebase_web_config_from_env():
    """Build Firebase Web SDK config from individual env vars (returns None if key missing)."""
    api_key = os.environ.get("FIREBASE_WEB_API_KEY")
    if not api_key:
        return None
    return {
        "apiKey": api_key,
        "authDomain": os.environ.get("FIREBASE_WEB_AUTH_DOMAIN", ""),
        "projectId": os.environ.get("FIREBASE_WEB_PROJECT_ID", ""),
        "storageBucket": os.environ.get("FIREBASE_WEB_STORAGE_BUCKET", ""),
        "messagingSenderId": os.environ.get("FIREBASE_WEB_MESSAGING_SENDER_ID", ""),
        "appId": os.environ.get("FIREBASE_WEB_APP_ID", ""),
        "measurementId": os.environ.get("FIREBASE_WEB_MEASUREMENT_ID", ""),
    }


def firebase_web_config():
    if os.environ.get("FIREBASE_WEB_ANALYTICS", "1").strip().lower() in (
        "0",
        "false",
        "no",
    ):
        return None
    raw = os.environ.get("FIREBASE_WEB_CONFIG_JSON")
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None
    return _firebase_web_config_from_env()


@app.context_processor
def _inject_firebase_web_config():
    return {"firebase_web_config": firebase_web_config()}


client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

STYLES_PATH = os.path.join(os.path.dirname(__file__), "styles.json")
with open(STYLES_PATH) as f:
    STYLES = json.load(f)
STYLE_MAP = {s["name"]: s for s in STYLES}

from storyAgent import prompt as STORY_PROMPT
from firestore_logger import fetch_run, list_runs, log_run

STORY_STYLES_REQUIRING_NARRATIVE = {"Junk Journal", "Comic Strip"}


@app.errorhandler(413)
def request_entity_too_large(e):
    return jsonify(error="Upload too large. Please use fewer or smaller images (limit ~4.5 MB)."), 413


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def compress_image_from_file(file_storage, max_size=(1024, 1024), quality=80):
    """Compress an uploaded file (werkzeug FileStorage) and return a PIL Image."""
    img = Image.open(file_storage.stream)
    if img.mode == "RGBA":
        img = img.convert("RGB")
    original_size = img.size
    img.thumbnail(max_size, Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    compressed_kb = buf.tell() / 1024
    buf.seek(0)
    compressed_img = Image.open(buf)
    compressed_img.load()
    print(f"[compress] {original_size} -> {compressed_img.size}, {compressed_kb:.1f} KB")
    return compressed_img


def _fmt_firestore_time(ts):
    if ts is None:
        return ""
    try:
        return ts.strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return str(ts)


def _story_title(story):
    """Support legacy flat story dicts and new { memory_story, memory_summary } shape."""
    if not isinstance(story, dict):
        return "(no title)"
    inner = story.get("memory_story")
    if isinstance(inner, dict) and inner.get("title"):
        return inner["title"]
    return story.get("title") or "(no title)"


def _compress_uploaded_images(files):
    """Compress a list of uploaded files and return PIL images + timing."""
    t = time.time()
    images = [compress_image_from_file(f) for f in files]
    print(f"[time] Compression ({len(files)} images): {time.time() - t:.2f}s")
    return images


def _truncate_text(value, max_len=120000):
    if value is None:
        return None
    text = str(value)
    if len(text) <= max_len:
        return text
    return text[:max_len] + f"\n...[truncated {len(text) - max_len} chars]"


def build_image_story_context(story, style_name):
    """Extract image-relevant fields from storyAgent JSON for the image model."""
    if not isinstance(story, dict):
        return "(story unavailable)"

    ms = story.get("memory_story")
    if not isinstance(ms, dict):
        raw = story.get("raw")
        if raw:
            return f"(parse incomplete; use summary grounding)\n{str(raw)[:2500]}"
        return "(no memory_story in story output)"

    lines = []

    ms_summary = story.get("memory_summary")
    if ms_summary:
        lines.append(f"- Memory summary: {ms_summary}")

    if ms.get("title"):
        lines.append(f"- Title: {ms['title']}")

    em = ms.get("emotion")
    if em:
        ei = ms.get("emotion_intensity") or "medium"
        lines.append(f"- Emotion: {em} (intensity: {ei})")

    if ms.get("core_message"):
        lines.append(f"- Core message: {ms['core_message']}")

    nav = ms.get("narrative") or {}
    if nav.get("moment"):
        lines.append(f"- Moment / scene: {nav['moment']}")
    if nav.get("meaning"):
        lines.append(f"- Meaning: {nav['meaning']}")
    if nav.get("reflection"):
        lines.append(f"- Reflection: {nav['reflection']}")

    ve = ms.get("visual_elements") or {}
    ko = ve.get("key_objects") or []
    if ko:
        ko = [str(x) for x in ko if x]
        if ko:
            lines.append(f"- Key objects: {', '.join(ko)}")
    ec = ve.get("environment_cues") or []
    if ec:
        ec = [str(x) for x in ec if x]
        if ec:
            lines.append(f"- Environment: {', '.join(ec)}")
    if ve.get("color_mood"):
        lines.append(f"- Color mood: {ve['color_mood']}")

    te = ms.get("text_elements") or {}
    if te.get("primary_caption"):
        lines.append(f"- Primary caption (on-image text): {te['primary_caption']}")
    if te.get("title_text"):
        lines.append(f"- Title text: {te['title_text']}")
    if te.get("secondary_caption"):
        lines.append(f"- Secondary caption: {te['secondary_caption']}")
    if te.get("handwritten_note"):
        lines.append(f"- Handwritten note: {te['handwritten_note']}")

    chars = ms.get("characters") or []
    if chars:
        char_bits = []
        for c in chars:
            if not isinstance(c, dict):
                continue
            lab = (c.get("label") or "").strip()
            desc = (c.get("description") or "").strip()
            prim = " (primary)" if c.get("is_primary") else ""
            head = f"{lab}{prim}".strip()
            if head and desc:
                char_bits.append(f"{head}: {desc}")
            elif desc:
                char_bits.append(desc)
            elif head:
                char_bits.append(head)
        if char_bits:
            lines.append("- Characters:\n  " + "\n  ".join(char_bits))

    pm = ms.get("photo_mapping") or []
    if pm:
        pm_bits = []
        for p in pm:
            if not isinstance(p, dict):
                continue
            idx = p.get("photo_index")
            role = p.get("role")
            vf = p.get("visual_focus") or ""
            ch = p.get("composition_hint") or ""
            parts = [f"photo {idx}", f"role={role}"]
            if vf:
                parts.append(f"focus={vf}")
            if ch:
                parts.append(f"composition={ch}")
            pm_bits.append(", ".join(parts))
        if pm_bits:
            lines.append("- Photo mapping:\n  " + "\n  ".join(pm_bits))

    ef = ms.get("experience_flow") or {}
    if ef.get("highlight_moment"):
        lines.append(f"- Highlight moment: {ef['highlight_moment']}")
    if ef.get("pacing"):
        lines.append(f"- Pacing: {ef['pacing']}")

    sa = ms.get("style_adaptations") or {}
    if style_name and sa.get(style_name):
        lines.append(f"- Style-specific direction for «{style_name}»: {sa[style_name]}")

    if not lines:
        raw = story.get("raw")
        if raw:
            return str(raw)[:2500]
        return "(empty story fields)"

    return "\n".join(lines)


def generate_story(images, summary, style_hint=None):
    """Call Gemini to produce a structured story from photos + summary."""
    style_clause = ""
    if style_hint:
        key = {"Junk Journal": "junk_journal", "Comic Strip": "comic_strip"}.get(style_hint)
        if key:
            style_clause = (
                f'\n\nStyle hint: the user selected an illustration style aligned with "{key}". '
                f"Make that entry in memory_story.style_adaptations especially detailed and actionable."
            )

    user_turn = json.dumps(
        {"messages": [{"role": "user", "content": summary}]},
        ensure_ascii=False,
    )
    story_prompt = (
        STORY_PROMPT
        + style_clause
        + "\n\nUser turn (conversation JSON):\n"
        + user_turn
        + "\n\nPhotos are attached in submission order (photo_index 0 = first image, then 1, …)."
        + "\n\nRespond with ONLY valid JSON (no markdown fences)."
    )

    t = time.time()
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[story_prompt] + images,
    )
    print(f"[time] Story generation: {time.time() - t:.2f}s")

    raw = response.text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"title": "", "raw": raw}


def build_suggest_prompt(summary, style_descriptions, excluded=None):
    excluded = excluded or []
    available = [s for s in style_descriptions if s["name"] not in excluded]
    styles_text = "\n".join(
        f'- **{s["name"]}**: {s["description"]}' for s in available
    )
    return (
        "You are an art-direction assistant. Given the user's photos and summary, "
        "pick the single most appropriate illustration style from the list below.\n\n"
        f"## Available styles\n{styles_text}\n\n"
        f"## User summary\n{summary}\n\n"
        "Respond with ONLY valid JSON (no markdown fences) in this exact format:\n"
        '{"style_name": "<exact style name>", "reasoning": "<1-2 sentence explanation>"}'
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/api/debug-firestore")
def debug_firestore():
    """Diagnostic: shows Firestore init state (no secrets exposed)."""
    sa_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
    gac = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    from firestore_logger import PROJECT_ID, DATABASE_ID, _client, _firestore_init_failed
    client = _client()
    return jsonify(
        FIREBASE_SERVICE_ACCOUNT_JSON_set=bool(sa_json),
        FIREBASE_SERVICE_ACCOUNT_JSON_length=len(sa_json) if sa_json else 0,
        GOOGLE_APPLICATION_CREDENTIALS=gac or "(not set)",
        GAC_file_exists=bool(gac and os.path.isfile(gac)),
        FIREBASE_PROJECT_ID=PROJECT_ID,
        FIREBASE_DATABASE_ID=DATABASE_ID,
        firestore_client_ok=client is not None,
        init_failed_flag=_firestore_init_failed,
    )


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/history")
def history():
    runs = list_runs(limit=100)
    for r in runs:
        r["created_at_fmt"] = _fmt_firestore_time(r.get("created_at"))
    return render_template("history.html", runs=runs)


@app.route("/history/<run_id>")
def history_detail(run_id):
    row = fetch_run(run_id)
    if not row:
        return render_template("history_detail.html", error="Run not found.", run=None), 404
    main, inputs, out_b64 = row
    main["created_at_fmt"] = _fmt_firestore_time(main.get("created_at"))
    return render_template(
        "history_detail.html",
        error=None,
        run=main,
        input_images=inputs,
        output_image_base64=out_b64,
    )


@app.route("/api/suggest", methods=["POST"])
def suggest():
    """Stateless: receives images + summary, returns story + style suggestion."""
    t_start = time.time()

    summary = request.form.get("summary", "")
    files = request.files.getlist("images")
    if not files or not files[0].filename:
        return jsonify(error="Please upload at least one image."), 400
    if len(files) > 10:
        return jsonify(error="Maximum 10 images allowed."), 400

    compressed_images = _compress_uploaded_images(files)

    story = generate_story(compressed_images, summary)
    print(f"[story] Generated base story: {_story_title(story)}")

    prompt = build_suggest_prompt(summary, STYLES)
    contents = [prompt] + compressed_images

    t_suggest = time.time()
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=contents,
    )
    print(f"[time] Style suggestion: {time.time() - t_suggest:.2f}s")

    try:
        raw = response.text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        suggestion = json.loads(raw)
    except (json.JSONDecodeError, AttributeError):
        suggestion = {"style_name": STYLES[0]["name"], "reasoning": "Default fallback."}

    style_info = STYLE_MAP.get(suggestion["style_name"], STYLES[0])

    print(f"[time] Total suggest: {time.time() - t_start:.2f}s")

    run_id = log_run(
        "suggest",
        summary=summary,
        filenames=[f.filename for f in files],
        images=compressed_images,
        story=story,
        suggestion=suggestion,
        style_description=style_info["description"],
        total_time=round(time.time() - t_start, 2),
    )

    return jsonify(
        suggestion=suggestion,
        style_description=style_info["description"],
        story=story,
        run_id=run_id,
    )


@app.route("/api/reject", methods=["POST"])
def reject():
    """Stateless: receives images + summary + excluded styles, returns new suggestion."""
    t_start = time.time()

    summary = request.form.get("summary", "")
    excluded = json.loads(request.form.get("excluded", "[]"))
    files = request.files.getlist("images")
    if not files or not files[0].filename:
        return jsonify(error="Please upload at least one image."), 400

    remaining = [s for s in STYLES if s["name"] not in excluded]
    if not remaining:
        return jsonify(error="No more styles available.", exhausted=True), 200

    compressed_images = _compress_uploaded_images(files)

    prompt = build_suggest_prompt(summary, STYLES, excluded=excluded)
    contents = [prompt] + compressed_images

    t_suggest = time.time()
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=contents,
    )
    print(f"[time] Re-suggestion: {time.time() - t_suggest:.2f}s")

    try:
        raw = response.text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        suggestion = json.loads(raw)
    except (json.JSONDecodeError, AttributeError):
        suggestion = {"style_name": remaining[0]["name"], "reasoning": "Fallback."}

    if suggestion["style_name"] not in STYLE_MAP:
        suggestion["style_name"] = remaining[0]["name"]

    style_info = STYLE_MAP.get(suggestion["style_name"], remaining[0])

    print(f"[time] Total reject+suggest: {time.time() - t_start:.2f}s")

    run_id = log_run(
        "reject",
        summary=summary,
        filenames=[f.filename for f in files],
        images=compressed_images,
        suggestion=suggestion,
        style_description=style_info["description"],
        excluded=excluded,
        remaining=len(remaining) - 1,
        total_time=round(time.time() - t_start, 2),
    )

    return jsonify(
        suggestion=suggestion,
        style_description=style_info["description"],
        remaining=len(remaining) - 1,
        run_id=run_id,
    )


@app.route("/api/generate", methods=["POST"])
def generate():
    """Stateless: receives images + summary + style_name, returns base64 image."""
    t_start = time.time()

    summary = request.form.get("summary", "")
    style_name = request.form.get("style_name", "")
    files = request.files.getlist("images")
    if not files or not files[0].filename:
        return jsonify(error="Please upload at least one image."), 400

    style = STYLE_MAP.get(style_name)
    if not style:
        return jsonify(error=f"Unknown style: {style_name}"), 400

    compressed_images = _compress_uploaded_images(files)

    style_hint = style_name if style_name in STORY_STYLES_REQUIRING_NARRATIVE else None
    styled_story = generate_story(compressed_images, summary, style_hint=style_hint)
    story_context = build_image_story_context(styled_story, style_name)
    print(f"[story] For image generation ({style_name}): {_story_title(styled_story)}")

    prompt = (
        style["prompt"]
        + "\n\n## Story agent output (priority for mood, text, and composition)\n"
        "Apply these fields when present: emotion and emotion_intensity for palette and energy; "
        "color_mood, key_objects, and environment for visuals; primary_caption, title_text, "
        "secondary_caption, and handwritten_note for any typography; characters and photo_mapping "
        "for who and what to emphasize; highlight_moment and pacing for narrative emphasis; "
        f"the style-specific line for «{style_name}» for how this style should treat the scene.\n\n"
        + story_context
        + "\n\n## Original user summary (grounding)\n"
        + summary
    )
    contents = [prompt] + compressed_images

    t_gen = time.time()
    response = client.models.generate_content(
        model="gemini-3.1-flash-image-preview",
        contents=contents,
        config=types.GenerateContentConfig(
            image_config=types.ImageConfig(aspect_ratio="9:16"),
        ),
    )
    print(f"[time] Image generation: {time.time() - t_gen:.2f}s")

    image_b64 = None
    response_text = None
    for part in response.parts:
        if part.text is not None:
            response_text = part.text
        elif part.inline_data is not None:
            # google.genai.types.Image.save() only accepts a file path, not PIL kwargs;
            # use raw bytes from the image wrapper or blob.
            gen_image = part.as_image()
            raw = getattr(gen_image, "image_bytes", None) if gen_image is not None else None
            if not raw and part.inline_data.mime_type and part.inline_data.mime_type.startswith(
                "image/"
            ):
                raw = part.inline_data.data
            if raw:
                image_b64 = base64.b64encode(raw).decode("utf-8")

    if not image_b64:
        return jsonify(error="Generation failed — no image returned.", detail=response_text), 500

    print(f"[time] Total generate: {time.time() - t_start:.2f}s")

    run_id = log_run(
        "generate",
        summary=summary,
        filenames=[f.filename for f in files],
        images=compressed_images,
        story=styled_story,
        style_name=style_name,
        style_description=style.get("description"),
        image_prompt=_truncate_text(prompt),
        model_output_text=_truncate_text(response_text),
        generation_time=round(time.time() - t_gen, 2),
        total_time=round(time.time() - t_start, 2),
        output_image_base64=image_b64,
    )

    return jsonify(
        image_base64=image_b64,
        generation_time=round(time.time() - t_gen, 2),
        total_time=round(time.time() - t_start, 2),
        story=styled_story,
        run_id=run_id,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=True)
