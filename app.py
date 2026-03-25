import io
import json
import os
import time
import uuid

from dotenv import load_dotenv
from flask import Flask, jsonify, request, render_template, send_from_directory
from google import genai
from google.genai import types
from PIL import Image

load_dotenv()

app = Flask(__name__)
app.secret_key = os.urandom(24)

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

UPLOAD_DIR = os.path.join("static", "uploads")
OUTPUT_DIR = os.path.join("static", "outputs")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

with open("styles.json") as f:
    STYLES = json.load(f)
STYLE_MAP = {s["name"]: s for s in STYLES}

from storyAgent import prompt as STORY_PROMPT

STORY_STYLES_REQUIRING_NARRATIVE = {"Junk Journal", "Comic Strip"}

sessions: dict = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def compress_image(path, max_size=(1024, 1024), quality=80):
    img = Image.open(path)
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
    t_start = time.time()

    summary = request.form.get("summary", "")
    files = request.files.getlist("images")
    if not files or not files[0].filename:
        return jsonify(error="Please upload at least one image."), 400
    if len(files) > 10:
        return jsonify(error="Maximum 10 images allowed."), 400

    session_id = str(uuid.uuid4())
    image_paths = []
    compressed_images = []

    t_compress = time.time()
    for f in files:
        fname = f"{session_id}_{uuid.uuid4().hex[:8]}_{f.filename}"
        path = os.path.join(UPLOAD_DIR, fname)
        f.save(path)
        image_paths.append(path)
        compressed_images.append(compress_image(path))
    print(f"[time] Compression ({len(files)} images): {time.time() - t_compress:.2f}s")

    sessions[session_id] = {
        "summary": summary,
        "image_paths": image_paths,
        "compressed_images": compressed_images,
        "excluded_styles": [],
        "story": None,
    }

    story = generate_story(compressed_images, summary)
    sessions[session_id]["story"] = story
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
    sessions[session_id]["excluded_styles"].append(suggestion["style_name"])

    print(f"[time] Total suggest: {time.time() - t_start:.2f}s")

    return jsonify(
        session_id=session_id,
        suggestion=suggestion,
        style_description=style_info["description"],
        story=story,
    )


@app.route("/api/reject", methods=["POST"])
def reject():
    t_start = time.time()
    data = request.get_json()
    session_id = data.get("session_id")
    sess = sessions.get(session_id)
    if not sess:
        return jsonify(error="Session not found."), 404

    excluded = sess["excluded_styles"]
    remaining = [s for s in STYLES if s["name"] not in excluded]
    if not remaining:
        return jsonify(error="No more styles available.", exhausted=True), 200

    prompt = build_suggest_prompt(sess["summary"], STYLES, excluded=excluded)
    contents = [prompt] + sess["compressed_images"]

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
    sess["excluded_styles"].append(suggestion["style_name"])

    print(f"[time] Total reject+suggest: {time.time() - t_start:.2f}s")

    return jsonify(
        session_id=session_id,
        suggestion=suggestion,
        style_description=style_info["description"],
        remaining=len(remaining) - 1,
    )


@app.route("/api/generate", methods=["POST"])
def generate():
    # Additional Info: 
    # v0 - current implementation
    # v1 - labeled faces trim and upload (using face detection API)
    # v2- full context engine integration  
    
    t_start = time.time()
    data = request.get_json()
    session_id = data.get("session_id")
    style_name = data.get("style_name")

    sess = sessions.get(session_id)
    if not sess:
        return jsonify(error="Session not found."), 404

    style = STYLE_MAP.get(style_name)
    if not style:
        return jsonify(error=f"Unknown style: {style_name}"), 400

    story_context = ""
    if style_name in STORY_STYLES_REQUIRING_NARRATIVE:
        styled_story = generate_story(
            sess["compressed_images"], sess["summary"], style_hint=style_name
        )
        sess["story"] = styled_story
        story_context = (
            "\n\nStructured story (use this for narrative and text elements):\n"
            + json.dumps(styled_story, indent=2)
        )
        print(f"[story] Regenerated for {style_name}: {styled_story.get('title', '')}")

    prompt = style["prompt"] + " media context: " + sess["summary"] + story_context
    contents = [prompt] + sess["compressed_images"]

    t_gen = time.time()
    response = client.models.generate_content(
        model="gemini-3.1-flash-image-preview",
        contents=contents,
        config=types.GenerateContentConfig(
            image_config=types.ImageConfig(aspect_ratio="9:16"),
        ),
    )
    print(f"[time] Image generation: {time.time() - t_gen:.2f}s")

    output_path = None
    response_text = None
    for part in response.parts:
        if part.text is not None:
            response_text = part.text
        elif part.inline_data is not None:
            result_img = part.as_image()
            out_name = f"{session_id}_output.png"
            output_path = os.path.join(OUTPUT_DIR, out_name)
            result_img.save(output_path)

    if not output_path:
        return jsonify(error="Generation failed — no image returned.", detail=response_text), 500

    print(f"[time] Total generate: {time.time() - t_start:.2f}s")

    return jsonify(
        image_url=f"/{output_path}",
        generation_time=round(time.time() - t_gen, 2),
        total_time=round(time.time() - t_start, 2),
        story=sess.get("story"),
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
