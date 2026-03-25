import io
import os
import time

from dotenv import load_dotenv
from google import genai
from google.genai import types
from PIL import Image

load_dotenv()

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])


def compress_image(path, max_size=(1024, 1024), quality=80):
    img = Image.open(path)
    original_size = img.size
    img.thumbnail(max_size, Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    compressed_bytes = buf.tell()
    buf.seek(0)
    compressed_img = Image.open(buf)
    compressed_img.load()
    print(f"[compress] {original_size} -> {compressed_img.size}, "
          f"{compressed_bytes / 1024:.1f} KB (quality={quality})")
    return compressed_img


t_start = time.time()

rizo_prompt = ("Modern minimalist travel poster style, clean lines, flat colours, soft shadows, geometric shapes, elegant vector illustration, balanced layout, poster design aesthetic, high detail, print-ready. Transform the input image into a hand-drawn poster illustration in the 4:5 ratio Style: playful naïve line art Use simple ink outlines with loose sketch strokes, uneven lines, and light hatching for texture. Convert objects into simplified illustrated icons (buildings, trees, mountains, boats, roads, etc.). Apply flat colour fills with visible hand-drawn hatching instead of gradients or smooth shading. Composition should feel like a storybook illustrated, where elements are arranged narratively rather than in a strictly realistic perspective. Use a limited flat colour palette only: deep blue warm red mustard yellow forest green Colours should appear like printed ink or marker fills, slightly imperfect and organic. Avoid gradients, photorealism, or smooth shading. Add playful details like tiny houses, trees, waves, sun rays, birds, or vehicles, depending on the scene. The background should remain clean white to emphasise the illustration, but colourful The final result should look like a hand-printed illustration from an artist's sketchbook or boutique guidebook. Guardrails - 1. Don't imagine or create new elements. Infer only from what is provided in the photo and description (if any) by the user. 2. If the user provided any specific verbiage/words, use that. 3. Don't add footnotes 4. Only a single title should be fine.")
media_context = "me, surya and archana went on a road trip to death valley. it was a really fun trip. core emotion - happiness"

prompt = rizo_prompt + " media context: " + media_context
print("final prompt:", prompt)

t_compress = time.time()
image = compress_image("test1.jpg")
print(f"[time] Image compression: {time.time() - t_compress:.2f}s")

t_generate = time.time()
response = client.models.generate_content(
    model="gemini-3.1-flash-image-preview",
    contents=[prompt, image],
    config=types.GenerateContentConfig(
        image_config=types.ImageConfig(
            aspect_ratio="9:16"
        ),
    ),
)
print(f"[time] Gemini generation: {time.time() - t_generate:.2f}s")

for part in response.parts:
    if part.text is not None:
        print(part.text)
    elif part.inline_data is not None:
        result = part.as_image()
        result.save("generated_image.png")

print(f"[time] Total: {time.time() - t_start:.2f}s")