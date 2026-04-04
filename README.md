# AI Image Style Generator

Upload your photos and a short memory summary. The app generates a narrative story from your images, suggests an illustration style, and produces stylized artwork using Google Gemini.

## Styles

| Style | Description |
|---|---|
| **Rizograph** | Hand-drawn poster with playful line art, flat ink colours, and a storybook feel |
| **Gongbi Polaroid** | Classical Chinese Gongbi painting fused with modern Polaroid framing |
| **Junk Journal** | Collage-style mixed-media scrapbook with layered textures and vintage ephemera |
| **Comic Strip** | Bold comic book panels with thick outlines, speech bubbles, and pop-art colours |

## How it works

1. **Upload** up to 10 photos and describe the memory
2. The app generates a structured **story** from your photos + summary (via Gemini)
3. Gemini suggests the most fitting **illustration style**
4. Confirm or reject the suggestion (the app will suggest an alternative)
5. On confirm, the app generates the final **artwork**
   - For Junk Journal and Comic Strip, the story narrative is fed into image generation for richer results

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file:

```
GEMINI_API_KEY=your_api_key_here
```

### Firestore logging (optional)

Runs are logged to **Google Cloud Firestore** project **`klikitat-staging`** (project number `332716122874`) so you can inspect inputs and outputs under **Run history** (`/history`). Client-side Firestore rules are locked down (`allow read, write: if false`); only the **Firebase Admin SDK** on this server can read and write.

1. In [Google Cloud Console](https://console.cloud.google.com/) → IAM & Admin → Service Accounts, create or pick a key, then download JSON.
2. Either set the path to that file, or paste the entire JSON into an env var (e.g. on Vercel):

```
GOOGLE_APPLICATION_CREDENTIALS=/absolute/path/to/service-account.json
# or (single-line JSON string — escape quotes carefully in .env):
# FIREBASE_SERVICE_ACCOUNT_JSON={"type":"service_account",...}
```

Grant the service account **Cloud Datastore User** (or **Firebase Admin** / Editor for development) on project `klikitat-staging`, and enable **Firestore** in Native mode if prompted.

If credentials are missing or invalid, the app still runs; logging is skipped and history stays empty.

### Firebase Web (client Analytics)

The HTML pages load the Firebase JS SDK with your **public** web app config and enable **Google Analytics** (`measurementId`). This does **not** grant browser access to Firestore while your rules deny client reads/writes; logging remains **server-side only** via the Admin SDK.

- Disable: set `FIREBASE_WEB_ANALYTICS=0`
- Override config (e.g. another Firebase app): set `FIREBASE_WEB_CONFIG_JSON` to a full JSON object string

Restrict the web API key by **HTTP referrer** in [Google Cloud Console](https://console.cloud.google.com/apis/credentials) for production.

### Firestore tests

With credentials set (same as logging), run:

```bash
pytest tests/test_firestore_connection.py -v
```

Tests are skipped if credentials are missing. They write to `connection_tests` (then delete) and a temporary `runs` document for `log_run` / `fetch_run` cleanup.

## Run locally

```bash
source venv/bin/activate
python app.py
```

Open http://localhost:5001 (or the port set by `PORT`)

## Deploy to Vercel

The app is configured for Vercel serverless deployment (stateless architecture, base64 image responses).

1. Push to GitHub
2. Import the repo at [vercel.com/new](https://vercel.com/new)
3. Add `GEMINI_API_KEY` and (for logging) `FIREBASE_SERVICE_ACCOUNT_JSON` with the service account JSON for project `klikitat-staging`
4. Deploy

## Project structure

```
app.py              Flask backend (API routes + history UI)
firestore_logger.py Firestore logging (Admin SDK, project klikitat-staging)
storyAgent.py       Story generation prompt
styles.json         Style definitions (name, description, prompt)
templates/
  index.html        Single-page frontend (vanilla HTML/JS/CSS)
api/
  index.py          Vercel serverless entry point
vercel.json         Vercel routing config
main.py             Standalone CLI script (original prototype)
test_gemini.py      Gemini API connection smoke test
tests/              pytest (e.g. test_firestore_connection.py — needs Firestore credentials)
```

## API endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/history` | GET | Lists recent logged runs (Firestore, server-side) |
| `/history/<run_id>` | GET | One run: inputs, story, suggestions, generated image (if stored) |
| `/api/suggest` | POST | Accepts images + summary. Returns story + style suggestion (+ `run_id` when logged) |
| `/api/reject` | POST | Accepts images + summary + excluded styles. Returns new suggestion (+ `run_id`) |
| `/api/generate` | POST | Accepts images + summary + style name. Returns base64 PNG (+ `run_id`) |
