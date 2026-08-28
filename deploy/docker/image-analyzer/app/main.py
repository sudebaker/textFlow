"""FastAPI service that extracts text from images via a vision LLM.

POST /analyze  (multipart: file=<image>, prompt=<optional>)
    -> {"extracted_text": str, "language": str|None,
        "description": null, "confidence": float|None}

The service intentionally returns no "description": textFlow's image-worker
appends description to extracted_text, and the requirement is to capture the
TEXT in the image (OCR), not to describe it. description is always null.
"""

import hashlib
import io
import json
import logging
import os

import redis
import requests
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from PIL import Image

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="textFlow Image Analyzer", version="1.0.0")

# --- Configuration (all env-driven, OpenAI-compatible defaults) ---
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://host.docker.internal:11434").rstrip("/")
LLM_MODEL = os.getenv("LLM_MODEL", "gemma4:e4b")
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "180"))
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "3"))
MAX_IMAGE_DIM = int(os.getenv("MAX_IMAGE_DIM", "1024"))  # spec 5.1 resize
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "86400"))  # spec 5.3
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")

_redis = redis.from_url(REDIS_URL, decode_responses=True)

# Default prompt: extract ONLY the visible text. No descriptions, no commentary.
DEFAULT_PROMPT = (
    "Extract ALL text visible in the image. "
    "Return only the transcribed text, nothing else. "
    "If there is no text, return an empty string. "
    "Do not describe the image and do not add commentary."
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _resize_image(data: bytes) -> bytes:
    """Resize image so its largest dimension <= MAX_IMAGE_DIM (spec 5.1)."""
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        if max(img.size) <= MAX_IMAGE_DIM:
            return data
        img.thumbnail((MAX_IMAGE_DIM, MAX_IMAGE_DIM))
        buf = io.BytesIO()
        fmt = img.format or "JPEG"
        if fmt.upper() not in ("JPEG", "PNG", "WEBP"):
            fmt = "JPEG"
        img.save(buf, format=fmt)
        return buf.getvalue()
    except Exception as e:
        # Never fail analysis on resize failure; pass the original through.
        logger.warning("Resize failed, using original image: %s", e)
        return data


def _detect_language(text: str) -> str | None:
    if not text or not text.strip():
        return None
    try:
        from langdetect import detect
        return detect(text)
    except Exception:
        return None


def _call_llm(image_bytes: bytes, mime_type: str, prompt: str) -> str:
    """Call an OpenAI-compatible vision endpoint, returning the model text."""
    b64 = __import__("base64").b64encode(image_bytes).decode()
    data_url = f"data:{mime_type};base64,{b64}"

    payload = {
        "model": LLM_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
        "stream": False,
    }

    url = f"{LLM_BASE_URL}/v1/chat/completions"
    last_err: Exception | None = None
    for attempt in range(LLM_MAX_RETRIES):
        try:
            resp = requests.post(url, json=payload, timeout=LLM_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            return (data["choices"][0]["message"]["content"] or "").strip()
        except Exception as e:
            last_err = e
            logger.warning("LLM attempt %d failed: %s", attempt + 1, e)
            if attempt < LLM_MAX_RETRIES - 1:
                import time
                time.sleep(min(2 ** attempt, 30))
    raise HTTPException(status_code=502, detail=f"LLM call failed: {last_err}")


@app.post("/analyze")
async def analyze(
    file: UploadFile = File(...),
    prompt: str = Form(None),
) -> dict:
    """Extract text visible in the uploaded image."""
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")

    mime_type = file.content_type or "application/octet-stream"
    digest = _sha256(data)
    text_prompt = prompt or DEFAULT_PROMPT

    # spec 5.3: cache key includes image + effective prompt + model + preprocessing
    # so two prompts on the same image never collide (P1 bug fix).
    cache_material = f"{digest}:{text_prompt}:{LLM_MODEL}:{MAX_IMAGE_DIM}".encode()
    cache_key = f"image:{_sha256(cache_material)}"
    cached = _redis.get(cache_key)
    if cached:
        logger.info("Cache hit for %s (prompt-aware)", digest)
        return json.loads(cached)

    processed = _resize_image(data)
    extracted = _call_llm(processed, mime_type, prompt=text_prompt)

    result = {
        "extracted_text": extracted,
        "language": _detect_language(extracted),
        "description": None,  # intentional: we do not describe, we transcribe
        "confidence": 1.0 if extracted else 0.0,
    }

    try:
        _redis.setex(cache_key, CACHE_TTL_SECONDS, json.dumps(result))
    except Exception as e:
        logger.warning("Cache write failed: %s", e)

    return result


@app.get("/health")
async def health() -> dict:
    return {"status": "healthy", "model": LLM_MODEL, "backend": LLM_BASE_URL}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
