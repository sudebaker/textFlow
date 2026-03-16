# GPU/Images/Spreadsheets Feature Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use subagent-driven-development to implement this plan task-by-task.

**Goal:** Add per-worker GPU/CPU control, JPG/PNG image support, and spreadsheet-safe processing with size/row validation and optimized pipeline routing.

**Architecture:** 
1. Fix critical bugs in docling-server (file suffix detection, endpoint mismatch, dead code)
2. Wire GPU/CPU device env vars through embeddings and entities workers
3. Add jpg/jpeg/png to orchestrator whitelist
4. Implement spreadsheet row/size validation in orchestrator, reduce pipeline for spreadsheets to entities-only

**Tech Stack:** Go (orchestrator), Python (workers), Docling, GLiNER, BAAI/bge-m3

---

## Critical Bugs (Must Fix First)

### Bug A1: docling-server file suffix hardcoded to `.pdf`
**Files:** `cmd/docling-server/docling_server.py:148, 202`

**Why critical:** Docling identifies file type from suffix. `.pdf` suffix for all files causes DOCX, XLSX, images to fail silently.

**Step 1: Read the file and understand the bug**
- Current code: `tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")`
- Problem: Always `.pdf` regardless of actual file type
- Impact: Images like `photo.jpg` get saved as temp `.pdf`, Docling sees PDF format, extracts nothing

**Step 2: Fix the suffix derivation in live code path**

File: `cmd/docling-server/docling_server.py:148`

Replace:
```python
with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
```

With:
```python
from pathlib import Path
file_suffix = Path(file.filename).suffix or ".bin"
with tempfile.NamedTemporaryFile(delete=False, suffix=file_suffix) as tmp:
```

**Step 3: Delete dead code block**

File: `cmd/docling-server/docling_server.py:192-234`

Delete all 42 lines (the entire unreachable except block that duplicates the handler logic). The exception handler at line 185-190 is sufficient.

**Step 4: Manual verification**

```bash
cd cmd/docling-server
python -m py_compile docling_server.py
```

Expected: No syntax errors

**Step 5: Commit**

```bash
git add cmd/docling-server/docling_server.py
git commit -m "fix: derive temp file suffix from actual filename in docling-server"
```

---

### Bug A2: extraction-worker calls wrong endpoint `/v1/convert/file` instead of `/convert`
**Files:** `cmd/extraction-worker/worker.py:247, 285, 350`

**Why critical:** All three extraction methods (base64, file, url) call wrong endpoint. docling-server only exposes `/convert`, not `/v1/convert/file`. Requests 404 or silently fail.

**Step 1: Understand the mismatch**

- docling-server exposes: `POST /convert` (line 136 in docling_server.py)
- extraction-worker calls: `DOCLING_URL/v1/convert/file` (3 places)
- Result: 404 errors, text extraction returns empty string

**Step 2: Fix all three endpoint calls**

File: `cmd/extraction-worker/worker.py`

**Location 1 (line 247):** In `extract_text_from_base64`
```python
# BEFORE:
response = requests.post(
    f"{DOCLING_URL}/v1/convert/file",
    files={"files": (filename, document_bytes)},
    timeout=300,
)

# AFTER:
response = requests.post(
    f"{DOCLING_URL}/convert",
    files={"files": (filename, document_bytes)},
    timeout=300,
)
```

**Location 2 (line 285):** In `extract_text_from_file`
```python
# BEFORE:
response = requests.post(
    f"{DOCLING_URL}/v1/convert/file",
    files={"files": (filename, document_bytes)},
    timeout=300,
)

# AFTER:
response = requests.post(
    f"{DOCLING_URL}/convert",
    files={"files": (filename, document_bytes)},
    timeout=300,
)
```

**Location 3 (line 350):** In `extract_text_from_url`
```python
# BEFORE:
response = requests.post(
    f"{DOCLING_URL}/v1/convert/file",
    files={"files": (filename, document_bytes)},
    timeout=300,
)

# AFTER:
response = requests.post(
    f"{DOCLING_URL}/convert",
    files={"files": (filename, document_bytes)},
    timeout=300,
)
```

**Step 3: Fix response parsing (all three locations)**

The docling-server returns `{"markdown": "...", "text": "...", "num_pages": N, "success": true, "filename": "..."}`, but extraction-worker expects `result.get("document", {}).get("md_content", "")`. 

For each of the three locations above, replace the parsing:

```python
# BEFORE (after response.raise_for_status()):
result = response.json()
text = result.get("document", {}).get("md_content", "") or result.get(
    "document", {}
).get("text_content", "")

# AFTER:
result = response.json()
text = result.get("markdown", "") or result.get("text", "")
```

**Step 4: Verify syntax**

```bash
cd cmd/extraction-worker
python -m py_compile worker.py
```

Expected: No syntax errors

**Step 5: Commit**

```bash
git add cmd/extraction-worker/worker.py
git commit -m "fix: correct docling endpoint from /v1/convert/file to /convert and fix response parsing"
```

---

### Bug A3: metadata extraction temp file hardcoded to `.pdf` suffix
**Files:** `cmd/extraction-worker/worker.py:410`

**Why critical:** When metadata is extracted from a non-uploaded-path message (URL or base64), a temp file is created with `.pdf` suffix, causing type misidentification for images/spreadsheets.

**Step 1: Fix the temp file creation**

File: `cmd/extraction-worker/worker.py:410`

Replace:
```python
temp_fd, temp_file_path = tempfile.mkstemp(suffix=".pdf")
```

With:
```python
# Derive file extension from message filename or mime_type
filename = message.get("filename", "document")
from pathlib import Path
file_ext = Path(filename).suffix or ".bin"
temp_fd, temp_file_path = tempfile.mkstemp(suffix=file_ext)
```

**Step 2: Verify syntax**

```bash
cd cmd/extraction-worker
python -m py_compile worker.py
```

Expected: No syntax errors

**Step 3: Commit**

```bash
git add cmd/extraction-worker/worker.py
git commit -m "fix: derive temp file suffix from filename in metadata extraction"
```

---

## Feature 1: GPU/CPU Control per Worker

### Task 1.1: Wire `EMBEDDINGS_DEVICE` env var through embeddings worker
**Files:** `cmd/embeddings-worker/worker.py`, `cmd/embeddings-worker/app/services/embeddings.py`

**Step 1: Add env var in worker.py**

File: `cmd/embeddings-worker/worker.py`

Find the section with other env vars (around line 40-60) and add:

```python
# GPU/CPU device selection
EMBEDDINGS_DEVICE = os.getenv("EMBEDDINGS_DEVICE", None)
```

**Step 2: Pass device to EmbeddingService**

File: `cmd/embeddings-worker/worker.py` around line 91

Find: `self.service = EmbeddingService(model_path=MODEL_PATH)`

Replace with:
```python
self.service = EmbeddingService(model_path=MODEL_PATH, device=EMBEDDINGS_DEVICE)
```

**Step 3: Verify EmbeddingService constructor accepts device parameter**

File: `cmd/embeddings-worker/app/services/embeddings.py` line 97-101

The constructor should already accept `device` parameter:
```python
def __init__(self, model_path: str, device: Optional[str] = None):
```

If it doesn't, add it to the constructor signature and store as `self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")`.

**Step 4: Verify service initialization uses device**

In the same file, look for `.to(self.device)` or device assignment when loading the model. Confirm it's being used.

**Step 5: Syntax check**

```bash
cd cmd/embeddings-worker
python -m py_compile worker.py app/services/embeddings.py
```

Expected: No syntax errors

**Step 6: Commit**

```bash
git add cmd/embeddings-worker/worker.py
git commit -m "feat: add EMBEDDINGS_DEVICE env var and wire to EmbeddingService"
```

---

### Task 1.2: Wire `ENTITIES_DEVICE` env var through entities worker
**Files:** `cmd/entities-worker/worker.py`

**Step 1: Add env var**

File: `cmd/entities-worker/worker.py` around line 70 (before or after other env var definitions)

Add:
```python
# GPU/CPU device selection
ENTITIES_DEVICE = os.getenv("ENTITIES_DEVICE", "cpu")
```

**Step 2: Store device in EntitiesWorker.__init__**

File: `cmd/entities-worker/worker.py` line 80

Replace:
```python
self.device = "cpu"
```

With:
```python
self.device = ENTITIES_DEVICE
```

**Step 3: Apply device to GLiNER after model load**

File: `cmd/entities-worker/worker.py` around line 150-151 (after `GLiNER.from_pretrained()`)

After line 151 (`logger.info("   ✓ GLiNER model loaded successfully")`), add:

```python
if self.device != "cpu":
    logger.info(f"   Moving GLiNER to device: {self.device}")
    self.model = self.model.to(self.device)
```

**Step 4: Verify `.to()` is called during inference**

Search the file for lines like `self.model.predict()` or similar inference calls. Ensure they use `self.device` if not already implicit.

**Step 5: Syntax check**

```bash
cd cmd/entities-worker
python -m py_compile worker.py
```

Expected: No syntax errors

**Step 6: Commit**

```bash
git add cmd/entities-worker/worker.py
git commit -m "feat: add ENTITIES_DEVICE env var and move GLiNER to specified device"
```

---

### Task 1.3: Document new env vars
**Files:** `.env.example`, `deploy/docker/docker-compose.yml`

**Step 1: Update .env.example**

File: `.env.example`

Find the section with worker config (around line 40-50) and add:

```bash
# GPU/CPU Device Selection (optional, auto-detects if not set)
# Options: "cpu", "cuda", "cuda:0", "cuda:1", etc.
EMBEDDINGS_DEVICE=
ENTITIES_DEVICE=cpu
```

**Step 2: Update docker-compose.yml**

File: `deploy/docker/docker-compose.yml`

Find the `embeddings-worker` service and add to its `environment:` section:
```yaml
- EMBEDDINGS_DEVICE=${EMBEDDINGS_DEVICE:-}
```

Find the `entities-worker` service and add to its `environment:` section:
```yaml
- ENTITIES_DEVICE=${ENTITIES_DEVICE:-cpu}
```

**Step 3: Verify YAML syntax**

```bash
docker-compose -f deploy/docker/docker-compose.yml config > /dev/null
```

Expected: No errors

**Step 4: Commit**

```bash
git add .env.example deploy/docker/docker-compose.yml
git commit -m "docs: add EMBEDDINGS_DEVICE and ENTITIES_DEVICE to env config"
```

---

## Feature 2: JPG/PNG Image Support

### Task 2.1: Add image extensions to orchestrator whitelist
**Files:** `cmd/orchestrator/main.go`

**Step 1: Update allowedExtensions map**

File: `cmd/orchestrator/main.go` around line 616-627

Find the `allowedExtensions := map[string]bool{` block and add:

```go
allowedExtensions := map[string]bool{
    ".pdf":  true,
    ".txt":  true,
    ".doc":  true,
    ".docx": true,
    ".ppt":  true,
    ".pptx": true,
    ".xls":  true,
    ".xlsx": true,
    ".csv":  true,
    ".json": true,
    ".jpg":  true,
    ".jpeg": true,
    ".png":  true,
}
```

**Step 2: Update error message**

File: `cmd/orchestrator/main.go` around line 633

Replace:
```go
Detail: "file type not allowed. Supported types: pdf, txt, doc, docx, ppt, pptx, xls, xlsx, csv, json",
```

With:
```go
Detail: "file type not allowed. Supported types: pdf, txt, doc, docx, ppt, pptx, xls, xlsx, csv, json, jpg, jpeg, png",
```

**Step 3: Build and syntax check**

```bash
cd cmd/orchestrator
go build -o orchestrator .
```

Expected: Binary builds successfully

**Step 4: Commit**

```bash
git add cmd/orchestrator/main.go
git commit -m "feat: add jpg, jpeg, png to allowed file extensions"
```

---

## Feature 3: Spreadsheet Validation & Reduced Pipeline

### Task 3.1: Add spreadsheet size/row validation in orchestrator
**Files:** `cmd/orchestrator/main.go`

**Step 1: Add config vars for spreadsheet limits**

File: `cmd/orchestrator/main.go` around line 60-80 (near other config initialization)

Add after other env var reads:

```go
// Spreadsheet size limits
maxSpreadsheetRowsStr := os.Getenv("MAX_SPREADSHEET_ROWS")
if maxSpreadsheetRowsStr == "" {
    maxSpreadsheetRowsStr = "2000"
}
maxSpreadsheetRows, _ := strconv.Atoi(maxSpreadsheetRowsStr)

maxSpreadsheetSizeMBStr := os.Getenv("MAX_SPREADSHEET_SIZE_MB")
if maxSpreadsheetSizeMBStr == "" {
    maxSpreadsheetSizeMBStr = "5"
}
maxSpreadsheetSizeMB, _ := strconv.Atoi(maxSpreadsheetSizeMBStr)
maxSpreadsheetBytes := int64(maxSpreadsheetSizeMB * 1024 * 1024)
```

**Step 2: Add spreadsheet validation in uploadHandler**

File: `cmd/orchestrator/main.go` around line 635 (after the extension check, before jobID generation)

Add:

```go
// Check spreadsheet size and row limits
if ext == ".csv" || ext == ".xls" || ext == ".xlsx" {
    // Check file size
    if header.Size > maxSpreadsheetBytes {
        c.JSON(http.StatusBadRequest, models.ErrorResponse{
            Error:  "file_too_large",
            Detail: fmt.Sprintf("spreadsheet exceeds size limit of %d MB", maxSpreadsheetSizeMB),
        })
        return
    }

    // For CSV, count rows
    if ext == ".csv" {
        fileContent, err := ioutil.ReadAll(file)
        if err != nil {
            c.JSON(http.StatusBadRequest, models.ErrorResponse{
                Error:  "read_error",
                Detail: "could not read file",
            })
            return
        }

        reader := csv.NewReader(bytes.NewReader(fileContent))
        recordCount := 0
        for {
            _, err := reader.Read()
            if err == io.EOF {
                break
            }
            if err != nil {
                c.JSON(http.StatusBadRequest, models.ErrorResponse{
                    Error:  "csv_parse_error",
                    Detail: "could not parse CSV file",
                })
                return
            }
            recordCount++
        }

        if recordCount > maxSpreadsheetRows {
            c.JSON(http.StatusBadRequest, models.ErrorResponse{
                Error:  "too_many_rows",
                Detail: fmt.Sprintf("CSV exceeds row limit of %d rows (%d rows found)", maxSpreadsheetRows, recordCount),
            })
            return
        }

        // Rewind file for later use
        file.Seek(0, 0)
    }
}
```

**Step 3: Add imports at top of file**

File: `cmd/orchestrator/main.go` around line 1-20

Ensure these are imported (add if missing):
```go
import (
    "bytes"
    "encoding/csv"
    "io"
    "io/ioutil"
    "strconv"
    // ... other imports
)
```

**Step 4: Set document type in JobMessage**

After validation, when storing the JobMessage, set a field to indicate spreadsheet type. 

File: `cmd/orchestrator/main.go` around line 660-700 (where JobMessage is created)

Before publishing to RabbitMQ, add to the JobMessage:

```go
// Mark document type for pipeline routing
if ext == ".csv" || ext == ".xls" || ext == ".xlsx" {
    jobMsg.MIMEType = "application/spreadsheet"  // Use existing MIMEType field to mark it
}
```

**Step 5: Build and syntax check**

```bash
cd cmd/orchestrator
go build -o orchestrator .
```

Expected: Binary builds successfully

**Step 6: Commit**

```bash
git add cmd/orchestrator/main.go
git commit -m "feat: add spreadsheet row/size validation with configurable limits"
```

---

### Task 3.2: Route spreadsheet messages to entities-only queue
**Files:** `cmd/extraction-worker/worker.py`

**Step 1: Detect spreadsheet type**

File: `cmd/extraction-worker/worker.py` around line 464

Before the queue publish loop, add detection logic:

```python
# Determine if this is a spreadsheet (reduce pipeline: entities only)
is_spreadsheet = False
if message.get("mime_type") == "application/spreadsheet":
    is_spreadsheet = True
elif message.get("document_path"):
    path_lower = message["document_path"].lower()
    if path_lower.endswith((".csv", ".xls", ".xlsx")):
        is_spreadsheet = True

# Route to appropriate queues
if is_spreadsheet:
    target_queues = ["entities"]
    logger.info(f"Detected spreadsheet, routing to entities-only pipeline")
else:
    target_queues = ["embeddings", "entities", "metadata"]
```

**Step 2: Update queue publish loop**

File: `cmd/extraction-worker/worker.py` around line 464

Replace:
```python
for queue in ["embeddings", "entities", "metadata"]:
```

With:
```python
for queue in target_queues:
```

**Step 3: Syntax check**

```bash
cd cmd/extraction-worker
python -m py_compile worker.py
```

Expected: No syntax errors

**Step 4: Commit**

```bash
git add cmd/extraction-worker/worker.py
git commit -m "feat: route spreadsheets to entities-only pipeline (skip embeddings and metadata)"
```

---

### Task 3.3: Document new spreadsheet env vars
**Files:** `.env.example`, `deploy/docker/docker-compose.yml`

**Step 1: Update .env.example**

File: `.env.example`

Find the section with orchestrator config and add:

```bash
# Spreadsheet Processing Limits
MAX_SPREADSHEET_ROWS=2000
MAX_SPREADSHEET_SIZE_MB=5
```

**Step 2: Update docker-compose.yml**

File: `deploy/docker/docker-compose.yml`

Find the `orchestrator` service and add to its `environment:` section:

```yaml
- MAX_SPREADSHEET_ROWS=${MAX_SPREADSHEET_ROWS:-2000}
- MAX_SPREADSHEET_SIZE_MB=${MAX_SPREADSHEET_SIZE_MB:-5}
```

**Step 3: Verify YAML syntax**

```bash
docker-compose -f deploy/docker/docker-compose.yml config > /dev/null
```

Expected: No errors

**Step 4: Commit**

```bash
git add .env.example deploy/docker/docker-compose.yml
git commit -m "docs: add MAX_SPREADSHEET_ROWS and MAX_SPREADSHEET_SIZE_MB to config"
```

---

## Verification & Build

### Final Build & Test

After all commits, run full build and test suite:

```bash
make build
make test
make test-python
```

Expected: All pass

---

## Commit Summary

When complete, you should have these **13 commits**:

1. ✅ fix: derive temp file suffix from actual filename in docling-server
2. ✅ fix: correct docling endpoint from /v1/convert/file to /convert and fix response parsing
3. ✅ fix: derive temp file suffix from filename in metadata extraction
4. ✅ feat: add EMBEDDINGS_DEVICE env var and wire to EmbeddingService
5. ✅ feat: add ENTITIES_DEVICE env var and move GLiNER to specified device
6. ✅ docs: add EMBEDDINGS_DEVICE and ENTITIES_DEVICE to env config
7. ✅ feat: add jpg, jpeg, png to allowed file extensions
8. ✅ feat: add spreadsheet row/size validation with configurable limits
9. ✅ feat: route spreadsheets to entities-only pipeline (skip embeddings and metadata)
10. ✅ docs: add MAX_SPREADSHEET_ROWS and MAX_SPREADSHEET_SIZE_MB to config

---

## Testing Strategy

### Manual Testing Checklist

After implementation, verify:

1. **Images work end-to-end:**
   - Upload a `.jpg` / `.png` file
   - Check orchestrator accepts it (no 400 error)
   - Check extraction-worker calls `/convert` (not `/v1/convert/file`)
   - Check docling-server saves with correct suffix
   - Check embeddings are generated

2. **Spreadsheets are validated:**
   - Upload a CSV with 3000+ rows → expect 400 error
   - Upload a 10MB XLSX file → expect 400 error
   - Upload valid CSV (500 rows, 1MB) → expect 200, routed to entities only

3. **GPU/CPU switching:**
   - Start embeddings-worker with `EMBEDDINGS_DEVICE=cpu` → check logs
   - Restart with `EMBEDDINGS_DEVICE=cuda` (if GPU available) → check logs show GPU
   - Same for entities-worker

4. **Backwards compatibility:**
   - PDF uploads still work
   - Existing text extraction unchanged
   - Non-spreadsheet files get full pipeline (embeddings + entities + metadata)

---

