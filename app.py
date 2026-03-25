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

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

STYLES_PATH = os.path.join(os.path.dirname(__file__), "styles.json")
with open(STYLES_PATH) as f:
    STYLES = json.load(f)
STYLE_MAP = {s["name"]: s for s in STYLES}

from storyAgent import prompt as STORY_PROMPT

STORY_STYLES_REQUIRING_NARRATIVE = {"Junk Journal", "Comic Strip"}


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


def _compress_uploaded_images(files):
    """Compress a list of uploaded files and return PIL images + timing."""
    t = time.time()
    images = [compress_image_from_file(f) for f in files]
    print(f"[time] Compression ({len(files)} images): {time.time() - t:.2f}s")
    return images


def generate_story(images, summary, style_hint=None):
    """Call Gemini to produce a structured story from photos + summary."""
    style_clause = ""
    if style_hint:
        key = {"Junk Journal": "junk_journal", "Comic Strip": "comic_strip"}.get(style_hint)
        if key:
            style_clause = f"\n\nThe selected style is {key}. Use the corresponding output format."

    story_prompt = (
        STORY_PROMPT
        + style_clause
        + f"\n\nUser context: {summary}"
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

@app.route("/")
def index():
    return render_template("index.html")


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
    print(f"[story] Generated base story: {story.get('title', '(no title)')}")

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

    return jsonify(
        suggestion=suggestion,
        style_description=style_info["description"],
        story=story,
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

    return jsonify(
        suggestion=suggestion,
        style_description=style_info["description"],
        remaining=len(remaining) - 1,
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

    story_context = ""
    styled_story = None
    if style_name in STORY_STYLES_REQUIRING_NARRATIVE:
        styled_story = generate_story(compressed_images, summary, style_hint=style_name)
        story_context = (
            "\n\nStructured story (use this for narrative and text elements):\n"
            + json.dumps(styled_story, indent=2)
        )
        print(f"[story] Regenerated for {style_name}: {styled_story.get('title', '')}")

    prompt = style["prompt"] + " media context: " + summary + story_context
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
            result_img = part.as_image()
            buf = io.BytesIO()
            result_img.save(buf, format="PNG")
            image_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

    if not image_b64:
        return jsonify(error="Generation failed — no image returned.", detail=response_text), 500

    print(f"[time] Total generate: {time.time() - t_start:.2f}s")

    return jsonify(
        image_base64=image_b64,
        generation_time=round(time.time() - t_gen, 2),
        total_time=round(time.time() - t_start, 2),
        story=styled_story,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
