"""GLiNER Entity Extraction Service (Python).

This FastAPI service exposes the same endpoints that previously existed in
the Go wrapper, but everything now runs natively in Python. The service loads
the GLiNER model once and keeps it in memory for subsequent requests.
"""

import logging
import os
import platform
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from app.config.settings import Settings as AppSettings


SERVICE_NAME = "gliner-service"
SERVICE_VERSION = os.getenv("SERVICE_VERSION", "1.0.0")
DEFAULT_ENTITY_TYPES = ["PER", "ORG", "LOC", "DATE", "MONEY"]
CURRENT_VERSION = SERVICE_VERSION


class Settings:
    """Wrapper around AppSettings for compatibility."""

    def __init__(self) -> None:
        self.app_settings = AppSettings()
        self.port = int(os.getenv("PORT", "8080"))
        self.model_path = self.app_settings.gliner_model_path
        self.model_name = os.getenv("GLINER_MODEL_NAME", "urchade/gliner_large-v2.1")
        self.allow_remote_download = self.app_settings.allow_remote_download

        # Legacy threshold (fallback if per-type not specified)
        self.confidence_threshold = float(
            os.getenv("GLINER_CONFIDENCE_THRESHOLD", "0.5")
        )

        # Use per-type thresholds from app settings
        self.threshold_per = self.app_settings.entity_threshold_per
        self.threshold_org = self.app_settings.entity_threshold_org
        self.threshold_loc = self.app_settings.entity_threshold_loc
        self.threshold_date = self.app_settings.entity_threshold_date
        self.threshold_money = self.app_settings.entity_threshold_money

        self.batch_size = int(os.getenv("GLINER_BATCH_SIZE", "32"))
        self.max_length = int(os.getenv("GLINER_MAX_LENGTH", "512"))
        self.default_entity_types = self._parse_entity_types(
            os.getenv("GLINER_DEFAULT_ENTITY_TYPES", ",".join(DEFAULT_ENTITY_TYPES))
        )
        self.use_mock = os.getenv("GLINER_USE_MOCK", "false").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    def get_threshold(self, entity_type: str) -> float:
        """Get threshold for specific entity type."""
        thresholds = {
            "PER": self.threshold_per,
            "ORG": self.threshold_org,
            "LOC": self.threshold_loc,
            "DATE": self.threshold_date,
            "MONEY": self.threshold_money,
        }
        return thresholds.get(entity_type.upper(), self.confidence_threshold)

    @staticmethod
    def _parse_entity_types(raw: str) -> List[str]:
        return [entry.strip().upper() for entry in raw.split(",") if entry.strip()]


class ExtractOptions(BaseModel):
    entity_types: Optional[List[str]] = Field(default=None)
    confidence_threshold: Optional[float] = Field(default=None, ge=0, le=1)
    max_length: Optional[int] = Field(default=None, gt=0)
    batch_size: Optional[int] = Field(default=None, gt=0)


class ExtractRequest(BaseModel):
    text: str = Field(..., min_length=1)
    options: Optional[ExtractOptions] = None


class BatchExtractRequest(BaseModel):
    chunks: List[str] = Field(..., min_length=1)
    options: Optional[ExtractOptions] = None


class Entity(BaseModel):
    text: str
    label: str
    confidence: float
    start_char: int
    end_char: int


class ExtractResponse(BaseModel):
    entities: List[Entity]
    processing_time_ms: int
    success: bool
    error: Optional[str] = None


class BatchExtractResponse(BaseModel):
    results: List[ExtractResponse]
    processing_time_ms: int
    success: bool
    error: Optional[str] = None


class HealthResponse(BaseModel):
    status: str
    version: str
    timestamp: datetime
    checks: Dict[str, str]


class MockGLiNER:
    """Minimal mock used when GLiNER is unavailable or disabled."""

    def predict_entities(
        self,
        texts: List[str],
        entity_types: List[str],
        threshold: float = 0.0,
        flat_ner: bool = True,
    ):
        results: List[List[Dict[str, Any]]] = []
        for text in texts:
            if not text:
                results.append([])
                continue
            entity_type = entity_types[0] if entity_types else "MOCK"
            chunk = text[: min(10, len(text))]
            results.append(
                [
                    {
                        "text": chunk,
                        "label": entity_type,
                        "score": 0.9,
                        "start": 0,
                        "end": len(chunk),
                    }
                ]
            )
        return results


class GLiNERAdapter:
    def __init__(self, settings: Settings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger
        self.model: Any = None
        self.model_status: str = "not_loaded"

    def _resolve_entity_types(self, options: Optional[ExtractOptions]) -> List[str]:
        if options and options.entity_types:
            return [
                item.strip().upper() for item in options.entity_types if item.strip()
            ]
        return self.settings.default_entity_types

    def _resolve_threshold(
        self,
        options: Optional[ExtractOptions],
        entity_types: Optional[List[str]] = None,
    ) -> Dict[str, float]:
        """
        Get thresholds for entity extraction.
        Returns a mapping of entity type to threshold.
        """
        if options and options.confidence_threshold is not None:
            # If explicit threshold provided in options, use for all types
            override = float(options.confidence_threshold)
            types = entity_types or self.settings.default_entity_types
            return {t: override for t in types}

        # Use per-type thresholds
        types = entity_types or self.settings.default_entity_types
        return {t: self.settings.get_threshold(t) for t in types}

    def ensure_model(self) -> None:
        if self.model is not None:
            return

        if self.settings.use_mock:
            self.model = MockGLiNER()
            self.model_status = "mock"
            self.logger.warning(
                "Starting GLiNER service in mock mode; set GLINER_USE_MOCK=false to use the real model"
            )
            return

        try:
            from gliner import GLiNER
        except Exception as exc:  # pragma: no cover - defensive import guard
            self.model_status = "unavailable"
            raise RuntimeError(
                "GLiNER package is not installed; install requirements first"
            ) from exc

        model_path = Path(self.settings.model_path)
        config_file = model_path / "config.json"

        # Try to load from local path first
        if config_file.exists():
            self.logger.info(
                "Loading GLiNER model from local path",
                extra={"model_path": str(model_path)},
            )
            try:
                self.model = GLiNER.from_pretrained(str(model_path))
                self.model_status = "ready"
                self.logger.info(
                    "GLiNER model loaded from local cache",
                    extra={"model_path": str(model_path)},
                )
                return
            except Exception as e:
                self.logger.warning(f"Failed to load from local path: {e}")

        # Fallback to remote download if allowed
        if self.settings.allow_remote_download:
            self.logger.info(
                "Loading GLiNER model from HuggingFace",
                extra={"model_name": self.settings.model_name},
            )
            try:
                self.model = GLiNER.from_pretrained(self.settings.model_name)
                self.model_status = "ready"
                self.logger.info(
                    "GLiNER model loaded from HuggingFace",
                    extra={"model_name": self.settings.model_name},
                )
                return
            except Exception as e:
                self.model_status = "unavailable"
                self.logger.error(f"Failed to load model from HuggingFace: {e}")
                raise
        else:
            self.model_status = "missing_model"
            raise FileNotFoundError(
                f"Model not found at {model_path} and remote download is disabled"
            )

    def extract(self, text: str, options: Optional[ExtractOptions]) -> ExtractResponse:
        if not text.strip():
            raise ValueError("text must not be empty")

        self.ensure_model()

        entity_types = self._resolve_entity_types(options)
        threshold_map = self._resolve_threshold(options, entity_types)

        # GLiNER expects a single threshold, use minimum for safety
        threshold = min(threshold_map.values())

        start_time = time.perf_counter()

        predictions = self.model.predict_entities(
            [text],
            entity_types,
            threshold=threshold,
            flat_ner=True,
        )

        duration_ms = int((time.perf_counter() - start_time) * 1000)

        entities: List[Entity] = []
        first_batch = predictions[0] if predictions else []
        for prediction in first_batch:
            label = prediction.get("label", "")
            score = float(prediction.get("score", threshold))
            entity_threshold = threshold_map.get(label, threshold)

            # Only include if confidence meets the threshold for this entity type
            if score >= entity_threshold:
                entities.append(
                    Entity(
                        text=prediction.get("text", ""),
                        label=label,
                        confidence=score,
                        start_char=int(prediction.get("start", 0)),
                        end_char=int(prediction.get("end", 0)),
                    )
                )

        return ExtractResponse(
            entities=entities,
            processing_time_ms=duration_ms,
            success=True,
            error=None,
        )

    def extract_batch(
        self, chunks: List[str], options: Optional[ExtractOptions]
    ) -> BatchExtractResponse:
        if not chunks:
            raise ValueError("chunks must contain at least one text entry")

        self.ensure_model()

        entity_types = self._resolve_entity_types(options)
        threshold_map = self._resolve_threshold(options, entity_types)

        # GLiNER expects a single threshold, use minimum for safety
        threshold = min(threshold_map.values())

        start_time = time.perf_counter()

        predictions = self.model.predict_entities(
            chunks,
            entity_types,
            threshold=threshold,
            flat_ner=True,
        )

        duration_ms = int((time.perf_counter() - start_time) * 1000)

        results: List[ExtractResponse] = []
        safe_predictions = predictions or [[] for _ in chunks]

        for idx, prediction_list in enumerate(safe_predictions):
            entities: List[Entity] = []
            for prediction in prediction_list:
                label = prediction.get("label", "")
                score = float(prediction.get("score", threshold))
                entity_threshold = threshold_map.get(label, threshold)

                # Only include if confidence meets the threshold for this entity type
                if score >= entity_threshold:
                    entities.append(
                        Entity(
                            text=prediction.get("text", ""),
                            label=label,
                            confidence=score,
                            start_char=int(prediction.get("start", 0)),
                            end_char=int(prediction.get("end", 0)),
                        )
                    )
            results.append(
                ExtractResponse(
                    entities=entities,
                    processing_time_ms=duration_ms,
                    success=True,
                    error=None,
                )
            )

        return BatchExtractResponse(
            results=results,
            processing_time_ms=duration_ms,
            success=True,
            error=None,
        )

    def health(self) -> Dict[str, str]:
        checks: Dict[str, str] = {}

        checks["model_path"] = (
            "ok" if Path(self.settings.model_path).exists() else "missing"
        )
        checks["model"] = self.model_status
        checks["mode"] = "mock" if self.settings.use_mock else "gliner"
        checks["python"] = platform.python_version()

        return checks


settings = Settings()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(SERVICE_NAME)

adapter = GLiNERAdapter(settings, logger)

app = FastAPI(
    title="GLiNER Entity Extraction Service",
    description="API for Named Entity Recognition using GLiNER",
    version=CURRENT_VERSION,
    docs_url="/swagger",
    redoc_url=None,
    openapi_url="/swagger/doc.json",
    contact={
        "name": "GLiNER Service",
        "url": "https://github.com/amphora/journalist-agent",
    },
    license_info={"name": "MIT"},
    servers=[{"url": "http://localhost:8080", "description": "Local"}],
)


REQUEST_COUNT = Counter(
    "gliner_requests_total", "Total GLiNER requests", ["endpoint", "status"]
)
REQUEST_LATENCY = Histogram(
    "gliner_request_latency_ms", "Request latency in milliseconds", ["endpoint"]
)


@app.middleware("http")
async def add_metrics(request: Request, call_next):
    start_time = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
    except Exception:
        raise
    finally:
        elapsed_ms = (time.perf_counter() - start_time) * 1000
        REQUEST_COUNT.labels(endpoint=request.url.path, status=str(status_code)).inc()
        REQUEST_LATENCY.labels(endpoint=request.url.path).observe(elapsed_ms)
    return response


@app.post(
    "/api/v1/extract",
    response_model=ExtractResponse,
    summary="Extract entities from text",
    tags=["extract"],
)
async def extract_entities(payload: ExtractRequest):
    try:
        result = adapter.extract(payload.text, payload.options)
        return JSONResponse(status_code=200, content=result.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError as exc:
        logger.exception("Model path missing")
        raise HTTPException(status_code=500, detail=str(exc))
    except Exception as exc:  # pragma: no cover - unexpected errors
        logger.exception("Extraction failed")
        raise HTTPException(status_code=500, detail=str(exc))


@app.post(
    "/api/v1/extract/batch",
    response_model=BatchExtractResponse,
    summary="Extract entities from multiple texts",
    tags=["extract"],
)
async def extract_entities_batch(payload: BatchExtractRequest):
    try:
        result = adapter.extract_batch(payload.chunks, payload.options)
        return JSONResponse(status_code=200, content=result.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError as exc:
        logger.exception("Model path missing")
        raise HTTPException(status_code=500, detail=str(exc))
    except Exception as exc:  # pragma: no cover - unexpected errors
        logger.exception("Batch extraction failed")
        raise HTTPException(status_code=500, detail=str(exc))


@app.get(
    "/api/v1/health",
    response_model=HealthResponse,
    summary="Health status",
    tags=["health"],
)
async def health():
    checks = adapter.health()
    status = "healthy" if checks.get("model") in {"ready", "mock"} else "degraded"

    return HealthResponse(
        status=status,
        version=SERVICE_VERSION,
        timestamp=datetime.now(timezone.utc),
        checks=checks,
    )


@app.get(
    "/metrics",
    summary="Prometheus metrics",
    tags=["metrics"],
)
async def metrics():
    data = generate_latest()
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)


if __name__ == "__main__":  # pragma: no cover
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=settings.port,
        reload=False,
        log_level="info",
    )
