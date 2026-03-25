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

## Run locally

```bash
source venv/bin/activate
python app.py
```

Open http://localhost:5000

## Deploy to Vercel

The app is configured for Vercel serverless deployment (stateless architecture, base64 image responses).

1. Push to GitHub
2. Import the repo at [vercel.com/new](https://vercel.com/new)
3. Add `GEMINI_API_KEY` in Environment Variables
4. Deploy

## Project structure

```
app.py              Flask backend (stateless API routes)
storyAgent.py       Story generation prompt
styles.json         Style definitions (name, description, prompt)
templates/
  index.html        Single-page frontend (vanilla HTML/JS/CSS)
api/
  index.py          Vercel serverless entry point
vercel.json         Vercel routing config
main.py             Standalone CLI script (original prototype)
test_gemini.py      Gemini API connection smoke test
```

## API endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/api/suggest` | POST | Accepts images + summary. Returns story + style suggestion |
| `/api/reject` | POST | Accepts images + summary + excluded styles. Returns new suggestion |
| `/api/generate` | POST | Accepts images + summary + style name. Returns base64 PNG |
