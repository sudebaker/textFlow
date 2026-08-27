"""image-analyzer: multimodal LLM service for textFlow image analysis.

Exposes POST /analyze (multipart) matching the contract expected by the
textFlow image-worker (pkg/image_client). It extracts the TEXT visible in
the image (OCR via a vision LLM) — never a description. The image is
resized before the LLM call (spec 5.1) and results are cached by the
image SHA256 (spec 5.3). The LLM backend is OpenAI-compatible
(/v1/chat/completions), so it works with vLLM in production and with
Ollama as the temporary dev engine.
"""
