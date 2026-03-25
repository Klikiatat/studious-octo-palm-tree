"""Quick smoke test for the Gemini API connection."""

import os
import time

from dotenv import load_dotenv
from google import genai

load_dotenv()

api_key = os.environ.get("GEMINI_API_KEY")
if not api_key:
    print("FAIL: GEMINI_API_KEY not set in .env")
    exit(1)

client = genai.Client(api_key=api_key)

print("Testing Gemini text generation (gemini-2.5-flash)...")
t = time.time()
response = client.models.generate_content(
    model="gemini-2.5-flash",
    contents="Say 'hello' in one word.",
)
elapsed = time.time() - t
print(f"  Response : {response.text.strip()}")
print(f"  Latency  : {elapsed:.2f}s")
print("OK: Gemini connection is working.")
