# textFlow — Roadmap

This roadmap describes the public direction of textFlow. It is intentionally
short: it lists capabilities that exist today and the limitations we consider
worth addressing next. Dates, internal milestones and implementation plans are
not published here.

## Current capabilities

- **Document extraction** for PDF, DOCX, PPTX and plain text through Docling.
- **Multimodal ingestion**: spreadsheets, images (OCR + optional semantic
  description) and audio (Whisper transcription).
- **Two processing profiles**: `rag` (entry + chunking + embeddings +
  inferences) and `full` (adds entities and metadata; images also get a
  description). Profiles can be selected per file and per batch document.
- **Optional stages** via the `inferences` and `inference_embeddings`
  features.
- **Air-gapped operation** after the initial model download: no internet
  access is required at build or runtime.
- **Backend-agnostic ML**: embeddings, inference and vision back ends are
  supplied by the operator through OpenAI-compatible endpoints.
- **Operations surface**: health checks, Prometheus metrics, Grafana
  dashboards and an artifact store with reachability-based garbage collection.

## Known limitations

These are deliberate current boundaries, not hidden defects:

- **Speaker diarization is not implemented.** The audio API accepts a
  `diarize` field but ignores it.
- **Reference GPU deployment is optional.** The base Compose stack is
  CPU-only; GPU acceleration requires the provided overlay.
- **Adaptive inference flow control is opt-in**
  (`INFERENCE_ADAPTIVE_ENABLED=false`). Its default behaviour is parameterised
  but not yet tuned against real serving benchmarks.
- **The visual-OCR quality gate is off by default**
  (`VISION_OCR_ENABLED=false`). Calibration against production hardware is
  still pending.
- **Authentication is optional** (`API_KEY`). Deployments exposed beyond a
  trusted network must provide their own access control or enable the API key
  middleware.
- **No built-in multi-tenant isolation.** One deployment is expected to serve
  one trust domain.

## Near-term direction

- Validate adaptive concurrency and visual extraction against real GPU
  serving, then decide whether their defaults change.
- Complete the multimodal pipeline where it is still partial: diarization and
  richer media metadata.
- Keep model acquisition and bundle generation reproducible as upstream
  projects move.
- Improve operability: clearer failure modes, queue back-pressure and
  observability for long-running jobs.

## Out of scope

- Hosting a public SaaS or managing user accounts.
- Shipping model weights in the repository or in container images.
- Provider-specific integrations that break the OpenAI-compatible contract.

## Contributing

Concrete proposals are welcome through GitHub issues. See
[`CONTRIBUTING.md`](../CONTRIBUTING.md) for the development workflow.
