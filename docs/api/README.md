# API Documentation

This directory contains generated HTML and text documentation for the **IA Text Orchestrator** project.

## Structure

### 📑 Entry Point
- **`index.html`** - Professional landing page with links to all documentation

### 🔵 Go API Documentation (Text Format)
Generated using `go doc` with all exported symbols documented:

- `go_models.txt` - Core data structures (Job, Status, Task, Entity, etc.)
- `go_middleware.txt` - Circuit breaker, retry logic, error handling
- `go_pipeline.txt` - Orchestration engine, fan-out, polling
- `go_events.txt` - Event bus, pub/sub, message routing
- `go_redis.txt` - Redis client, connection pooling, key operations (482 lines)
- `go_cache.txt` - Content caching, cache-aside pattern
- `go_broker.txt` - RabbitMQ integration, message queuing
- `go_config.txt` - Configuration management, environment variables
- `go_logging.txt` - Structured logging with zerolog
- `go_metrics.txt` - Prometheus metrics collection

### 🐍 Python API Documentation (HTML Format)
Generated with detailed class/method/function documentation:

- `python_extraction-worker_docs.html` - Document extraction via Docling
- `python_completion-worker_docs.html` - LLM aggregation & webhooks
- `python_metadata-worker_docs.html` - Lightweight metadata extraction
- `python_inference-worker_docs.html` - LLM integration & fallback logic
- `python_embeddings-worker_docs.html` - BAAI/bge-m3 embeddings
- `python_entities-worker_docs.html` - GLiNER NER (Named Entity Recognition)

### 📊 Additional Files
- `python_docs.json` - Extracted Python docstrings in JSON format (for tools/automation)

## Viewing Documentation

### Option 1: Web Browser
```bash
# Open the documentation in your default browser
open docs/api/index.html

# Or navigate to the file directly in your browser
file:///path/to/textflow/docs/api/index.html
```

### Option 2: Command Line
```bash
# View Go documentation
cat docs/api/go_models.txt
cat docs/api/go_redis.txt

# View Python documentation JSON
cat docs/api/python_docs.json | jq .
```

### Option 3: HTTP Server
```bash
cd docs/api
python3 -m http.server 8000

# Then open: http://localhost:8000/index.html
```

## Documentation Details

### Go Documentation
- **Format**: Plain text with section headers
- **Content**: Package-level documentation, exported types, constants, functions, methods
- **Coverage**: 100% of exported APIs from all internal packages
- **Generated with**: `go doc -all <package>`

### Python Documentation  
- **Format**: HTML with professional styling
- **Content**: Module docstrings, class definitions, method signatures, function documentation
- **Coverage**: 100% of documented classes and functions in all workers
- **Generated with**: Custom Python AST parser extracting Google-style docstrings

## How to Update Documentation

Regenerate all documentation after code changes:

```bash
# Regenerate all docs
python3 scripts/generate_docs.py

# This will:
# 1. Extract all Go package documentation
# 2. Parse Python docstrings from all workers
# 3. Generate HTML for Python workers
# 4. Create/update the main index.html
```

## Integration with CI/CD

Suggested pre-commit hook or CI step:

```bash
# Ensure docs are up to date before commit
python3 scripts/generate_docs.py
git add docs/api/
```

## Architecture Documentation

For high-level system design and data flow diagrams, see:
- `README_ARCHITECTURE.md` - System overview
- `internal/README_ARCHITECTURE.md` - Go infrastructure
- `cmd/README_ARCHITECTURE.md` - Workers & services
- `pkg/README_ARCHITECTURE.md` - Shared libraries

---

**Generated**: 2026-03-27  
**Tool**: `go doc` + Custom Python documentation generator  
**Total Files**: 21 | Total Size**: ~268 KB
