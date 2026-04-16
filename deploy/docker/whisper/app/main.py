import os
import logging
from pathlib import Path
from tempfile import NamedTemporaryFile
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from faster_whisper import WhisperModel

from app.models import TranscribeResponse, HealthResponse, Segment

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

DEVICE = os.getenv("DEVICE", "cpu")
COMPUTE_TYPE = os.getenv("COMPUTE_TYPE", "int8")
MODEL_SIZE = os.getenv("MODEL_SIZE", "large-v2")
MODEL_PATH = os.getenv("MODEL_PATH", "/models")

app = FastAPI(title="textFlow Whisper", version="1.0.0")

_model = None


def get_model():
    global _model
    if _model is None:
        logger.info(f"Loading model: {MODEL_SIZE} on {DEVICE}")
        _model = WhisperModel(
            MODEL_SIZE,
            device=DEVICE,
            compute_type=COMPUTE_TYPE,
            download_root=MODEL_PATH,
            local_files_only=True,
        )
        logger.info("Model loaded successfully")
    return _model


@app.get("/health", response_model=HealthResponse)
async def health():
    return HealthResponse(
        status="healthy",
        device=DEVICE,
        model=MODEL_SIZE,
        ready=_model is not None,
    )


@app.post("/transcribe", response_model=TranscribeResponse)
async def transcribe(
    audio: UploadFile = File(..., description="Audio file"),
    language: str = Form(None, description="Language code (optional)"),
):
    try:
        model = get_model()

        with NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
            contents = await audio.read()
            tmp.write(contents)
            tmp_path = tmp.name

        try:
            segments_iter, info = model.transcribe(
                tmp_path,
                language=language,
                word_timestamps=False,
                vad_filter=True,
                vad_parameters=dict(min_silence_duration_ms=500),
            )

            segments_list = []
            full_text_parts = []

            for i, seg in enumerate(segments_iter):
                segments_list.append(Segment(
                    id=i,
                    start=float(seg.start),
                    end=float(seg.end),
                    text=seg.text.strip(),
                    avg_logprob=float(seg.avg_logprob) if hasattr(seg, "avg_logprob") else 0.0,
                ))
                full_text_parts.append(seg.text.strip())

            return TranscribeResponse(
                language=info.language or "unknown",
                duration=info.duration or 0.0,
                segments=segments_list,
                text=" ".join(full_text_parts),
            )

        finally:
            Path(tmp_path).unlink(missing_ok=True)

    except Exception as e:
        logger.error(f"Transcription failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Transcription failed: {str(e)}")