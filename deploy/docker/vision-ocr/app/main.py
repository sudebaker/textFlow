"""Vision OCR service: transcribes page images via vLLM (MiniCPM-V 4.5).

Pattern: deploy/docker/image-analyzer (spec §9 reuse). Differences:
- NO resize by default (spec §11: MAX_IMAGE_DIM=0 disables; benchmark decides).
- temperature=0 transcription-only prompt (spec §10).
- server-side semaphore (spec §20): GPU protected even against bad clients."""
import asyncio, base64, hashlib, json, logging, os, time
import redis, requests
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from prometheus_client import Counter, Gauge, Histogram, start_http_server

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Backend-agnostic: any OpenAI-compatible server (vLLM prod, Ollama dev).
# Prod default: vllm-minicpm compose service. Dev: mac-mini Ollama
# (192.168.88.12:11434, model minicpm-v4.5:latest) — see A.3 verification.
VISION_LLM_BASE_URL = os.getenv("VISION_LLM_BASE_URL", "http://vllm-minicpm:8000").rstrip("/")
VISION_MODEL = os.getenv("VISION_MODEL", "minicpm-v-4.5")
VISION_TIMEOUT = float(os.getenv("VISION_TIMEOUT", "180"))
VISION_MAX_RETRIES = int(os.getenv("VISION_MAX_RETRIES", "2"))
MAX_CONCURRENCY = int(os.getenv("VISION_MAX_CONCURRENCY", "2"))
QUEUE_WAIT_SECONDS = float(os.getenv("VISION_QUEUE_WAIT_SECONDS", "10"))
MAX_IMAGE_DIM = int(os.getenv("MAX_IMAGE_DIM", "0"))          # 0 = NO resize (spec §11)
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "604800"))
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")

# Spec §10 — transcription, not interpretation. Freeze AFTER benchmark (Fase F).
TRANSCRIBE_PROMPT = (
    "Extract ALL text visible in the image. Transcribe it exactly. "
    "Preserve the original language. Do not translate. Do not summarize. "
    "Do not infer missing text. If text is illegible, leave it empty "
    "rather than inventing content."
)

app = FastAPI(title="textFlow Vision OCR", version="1.0.0")
_redis = redis.from_url(REDIS_URL, decode_responses=True)
_sem = asyncio.Semaphore(MAX_CONCURRENCY)                     # spec §20 server side

vision_requests_total = Counter("vision_ocr_requests_total", "Requests", ["outcome"])
vision_inference_seconds = Histogram("vision_ocr_inference_seconds", "LLM call (s)")
vision_in_flight = Gauge("vision_ocr_in_flight", "In-flight transcriptions")
vision_max_concurrency = Gauge(
    "vision_ocr_max_concurrency", "Configured in-flight capacity (semaphore)")
vision_max_concurrency.set(MAX_CONCURRENCY)


def _call_vllm(image_bytes: bytes, mime: str) -> "tuple[str, dict | None]":  # OpenAI-compatible (§9)
    b64 = base64.b64encode(image_bytes).decode()
    payload = {"model": VISION_MODEL, "temperature": 0, "stream": False,
               "messages": [{"role": "user", "content": [
                   {"type": "text", "text": TRANSCRIBE_PROMPT},
                   {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}]}]}
    last = None
    for attempt in range(VISION_MAX_RETRIES):
        try:
            r = requests.post(f"{VISION_LLM_BASE_URL}/v1/chat/completions", json=payload, timeout=VISION_TIMEOUT)
            r.raise_for_status()
            body = r.json()
            content = (body["choices"][0]["message"]["content"] or "").strip()
            usage = body.get("usage")
            return content, (usage if isinstance(usage, dict) else None)
        except Exception as e:
            last = e; logger.warning("vLLM attempt %d failed: %s", attempt + 1, e)
            time.sleep(min(2 ** attempt, 30))
    vision_requests_total.labels(outcome="error").inc()
    raise HTTPException(status_code=502, detail=f"vLLM call failed: {last}")


@app.post("/transcribe")
async def transcribe(file: UploadFile = File(...), dpi: int = Form(0)) -> dict:
    data = await file.read()
    if not data: raise HTTPException(400, "Empty file")
    mime = file.content_type or "image/png"
    digest = hashlib.sha256(data).hexdigest()
    # cache: prompt-aware key (image-analyzer P1 fix pattern, spec §12)
    cache_key = f"vision:{hashlib.sha256(f'{digest}:{TRANSCRIBE_PROMPT}:{VISION_MODEL}:{MAX_IMAGE_DIM}'.encode()).hexdigest()}"
    if (cached := _redis.get(cache_key)):
        vision_requests_total.labels(outcome="cache_hit").inc()
        logger.info("cache hit %s", digest)
        payload = json.loads(cached)
        payload["cached"] = True     # serve-time semantics: was this response cached
        return payload
    try:    # spec §20: saturated -> 503 + Retry-After (client treats as page error)
        await asyncio.wait_for(_sem.acquire(), timeout=QUEUE_WAIT_SECONDS)
    except Exception:
        vision_requests_total.labels(outcome="saturated").inc()
        raise HTTPException(503, "Saturated", headers={"Retry-After": "5"})
    vision_in_flight.inc(); t0 = time.monotonic()
    try:
        with vision_inference_seconds.time():
            text, usage = _call_vllm(data, mime)
        result = {"extracted_text": text, "model": VISION_MODEL, "cached": False,
                  "dpi": dpi, "elapsed_ms": int((time.monotonic() - t0) * 1000), "usage": usage}
        try: _redis.setex(cache_key, CACHE_TTL_SECONDS, json.dumps(result))
        except Exception as e: logger.warning("cache write failed: %s", e)
        vision_requests_total.labels(outcome="ok").inc()
        return result
    finally:
        vision_in_flight.dec(); _sem.release()


@app.get("/health")
async def health() -> dict:
    return {"status": "healthy", "model": VISION_MODEL, "backend": VISION_LLM_BASE_URL,
            "max_concurrency": MAX_CONCURRENCY, "in_flight": vision_in_flight._value.get()}


start_http_server(int(os.getenv("METRICS_PORT", "9090")))
