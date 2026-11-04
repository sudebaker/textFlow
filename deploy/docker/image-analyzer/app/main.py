"""FastAPI service for image OCR and semantic description.

POST /analyze  (multipart: file=<image>, prompt=<optional>)
    -> {"extracted_text": str, "language": str|None,
        "description": null, "confidence": float|None}

POST /describe (multipart: file=<image>, prompt=<optional>)
    -> {"description": str}

OCR and description are distinct capabilities backed by separately
configured LLM endpoints: OCR_BASE_URL/OCR_MODEL for /analyze and
IMAGE_BASE_URL/IMAGE_MODEL for /describe. /analyze never describes the
image; description is only requested by the image-worker for `full` jobs.
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
def _env_value(environ, name: str, default: str) -> str:
    """Return a trimmed env value, treating blank as unset."""
    value = (environ.get(name) or "").strip()
    return value or default


def _resolve_backends(environ) -> tuple[str, str, str, str]:
    """Resolve OCR and description LLM backends (image-ocr-description).

    Description defaults to the OCR backend when its own env is absent, so a
    single-backend deployment keeps working. Compose must not inject a
    non-empty IMAGE_* default or that fallback never applies.
    """
    ocr_url = _env_value(
        environ, "OCR_BASE_URL", "http://host.docker.internal:11434"
    ).rstrip("/")
    ocr_model = _env_value(environ, "OCR_MODEL", "gemma4:e4b")
    image_url = _env_value(environ, "IMAGE_BASE_URL", ocr_url).rstrip("/")
    image_model = _env_value(environ, "IMAGE_MODEL", ocr_model)
    return ocr_url, ocr_model, image_url, image_model


OCR_BASE_URL, OCR_MODEL, IMAGE_BASE_URL, IMAGE_MODEL = _resolve_backends(os.environ)
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

DEFAULT_DESCRIPTION_PROMPT = (
    "Describe the image in one or two sentences. "
    "Focus on what is visible and relevant. "
    "Do not transcribe the text in the image."
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


def _call_llm(
    image_bytes: bytes,
    mime_type: str,
    prompt: str,
    base_url: str,
    model: str,
) -> str:
    """Call an OpenAI-compatible vision endpoint, returning the model text."""
    b64 = __import__("base64").b64encode(image_bytes).decode()
    data_url = f"data:{mime_type};base64,{b64}"

    payload = {
        "model": model,
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

    url = f"{base_url}/v1/chat/completions"
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
    cache_material = f"{digest}:{text_prompt}:{OCR_MODEL}:{MAX_IMAGE_DIM}".encode()
    cache_key = f"image:{_sha256(cache_material)}"
    cached = _redis.get(cache_key)
    if cached:
        logger.info("Cache hit for %s (prompt-aware)", digest)
        return json.loads(cached)

    processed = _resize_image(data)
    extracted = _call_llm(
        processed,
        mime_type,
        prompt=text_prompt,
        base_url=OCR_BASE_URL,
        model=OCR_MODEL,
    )

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


@app.post("/describe")
async def describe(
    file: UploadFile = File(...),
    prompt: str = Form(None),
) -> dict:
    """Return a semantic description of the image (no OCR)."""
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")

    mime_type = file.content_type or "application/octet-stream"
    digest = _sha256(data)
    description_prompt = prompt or DEFAULT_DESCRIPTION_PROMPT

    cache_material = (
        f"{digest}:{description_prompt}:{IMAGE_MODEL}:{MAX_IMAGE_DIM}".encode()
    )
    cache_key = f"image:describe:{_sha256(cache_material)}"
    cached = _redis.get(cache_key)
    if cached:
        logger.info("Description cache hit for %s", digest)
        return json.loads(cached)

    processed = _resize_image(data)
    description = _call_llm(
        processed,
        mime_type,
        prompt=description_prompt,
        base_url=IMAGE_BASE_URL,
        model=IMAGE_MODEL,
    )

    result = {"description": description}

    try:
        _redis.setex(cache_key, CACHE_TTL_SECONDS, json.dumps(result))
    except Exception as e:
        logger.warning("Cache write failed: %s", e)

    return result


@app.get("/health")
async def health() -> dict:
    return {
        "status": "healthy",
        "ocr": {"model": OCR_MODEL, "backend": OCR_BASE_URL},
        "image": {"model": IMAGE_MODEL, "backend": IMAGE_BASE_URL},
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
