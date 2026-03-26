# Inference E2E Setup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use @subagent-driven-development (recommended) to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Document LLM inference environment variables, connect inference-worker to vLLM's network, update E2E client to support inferences, and verify with an end-to-end test.

**Architecture:** Four parallel/sequential improvements to enable the inference feature end-to-end: (1) env var documentation for deploy consistency, (2) Docker network connectivity so inference-worker can reach vLLM, (3) E2E client struct/flag updates to match backend API, (4) manual verification test run.

**Tech Stack:** Go (client), Docker Compose (networking), Python (inference-worker, already implemented).

---

## Task A: Document LLM inference vars in `.env` and `.env.example`

**Files:**
- Modify: `.env` (root of repo)
- Modify: `.env.example` (root of repo)

### Overview

The `inference-worker` (docker-compose.yml lines 243–273) already reads `LLM_URL` and `LLM_MODEL` from environment (see `cmd/inference-worker/worker.py` lines 32–33), and docker-compose already passes them (lines 255–256). But neither `.env` nor `.env.example` documents these variables — they must be added for deployment clarity.

- [ ] **Step 1: Read `.env` to find insertion point**

Run: `tail -20 /home/hp/Proyectos/ia-text-ochestrator/.env`

This shows the current end of the file. We'll append a new section after the DEPLOYMENT CHECKLIST.

- [ ] **Step 2: Add LLM section to `.env`**

Append to `.env`:
```
# ============================================================================
# 🤖 INFERENCE WORKER (vLLM)
# ============================================================================
# These variables are used by the inference-worker to connect to a vLLM server
# and generate text inferences on document chunks.
#
# For local development: set to http://localhost:8000 if vLLM is on the host
# For Docker: set to http://vllm-qwen-2b:8000 if vLLM is in docker_default network
# For production: set to the actual vLLM server URL

LLM_URL=http://vllm-qwen-2b:8000
LLM_MODEL=qwen3-coder
INFERENCES_QUEUE=inferences
```

- [ ] **Step 3: Read `.env.example` to find insertion point**

Run: `tail -20 /home/hp/Proyectos/ia-text-ochestrator/.env.example`

Same as `.env` — append after DEPLOYMENT CHECKLIST.

- [ ] **Step 4: Add LLM section to `.env.example` (with empty LLM_URL)**

Append to `.env.example`:
```
# ============================================================================
# 🤖 INFERENCE WORKER (vLLM)
# ============================================================================
# These variables enable the inference feature using an external vLLM server.
# REQUIRED if you want to use the --inferences flag in the E2E client.
#
# LLM_URL: Base URL of the vLLM server (no /v1 suffix, just the domain)
#   - For local dev with vLLM on host: http://localhost:8000
#   - For Docker Compose: http://vllm-qwen-2b:8000 (requires docker_default network)
#   - For production: set to your deployed vLLM endpoint
#
# LLM_MODEL: Model name loaded in the vLLM server (e.g., qwen3-coder, mistral-7b)
# INFERENCES_QUEUE: RabbitMQ queue name for inference tasks (default: inferences)

LLM_URL=
LLM_MODEL=qwen3-coder
INFERENCES_QUEUE=inferences
```

- [ ] **Step 5: Commit**

```bash
cd /home/hp/Proyectos/ia-text-ochestrator
git add .env .env.example
git commit -m "chore: document LLM inference environment variables"
```

---

## Task B: Connect inference-worker to docker_default network

**Files:**
- Modify: `deploy/docker/docker-compose.yml` (lines 243–273 for inference-worker, lines 385–393 for networks section)

### Overview

The vLLM server runs in the `docker_default` network (external to this compose file). The inference-worker currently only has access to `backend` and `datastore` (both internal). We need to:

1. Declare `docker_default` as an external network
2. Add it to inference-worker's network list
3. Update the default LLM_URL to use the container name on that network

- [ ] **Step 1: Read docker-compose.yml networks section**

Run: `sed -n '385,406p' /home/hp/Proyectos/ia-text-ochestrator/deploy/docker/docker-compose.yml`

Expected output shows current networks (frontend, backend, datastore). We'll add docker_default before the volumes section.

- [ ] **Step 2: Add docker_default as external network**

In `deploy/docker/docker-compose.yml` at line 385 (the `networks:` section), add before `volumes:` at line 394:

```yaml
  docker_default:
    external: true
```

- [ ] **Step 3: Update inference-worker networks**

At line 262 (`inference-worker.networks`), change from:
```yaml
    networks:
    - backend
    - datastore
```

To:
```yaml
    networks:
    - backend
    - datastore
    - docker_default
```

- [ ] **Step 4: Update LLM_URL default to use container name**

At line 255 (`LLM_URL` env var), change from:
```yaml
    - LLM_URL=${LLM_URL:-http://vllm_server:8000}
```

To:
```yaml
    - LLM_URL=${LLM_URL:-http://vllm-qwen-2b:8000}
```

(This matches the actual container name running on the `docker_default` network.)

- [ ] **Step 5: Verify changes**

Run: `docker compose -f deploy/docker/docker-compose.yml config | grep -A 20 'inference-worker:'`

Expected: `inference-worker` service should list three networks (backend, datastore, docker_default) and LLM_URL should default to `http://vllm-qwen-2b:8000`.

- [ ] **Step 6: Commit**

```bash
cd /home/hp/Proyectos/ia-text-ochestrator
git add deploy/docker/docker-compose.yml
git commit -m "feat: connect inference-worker to docker_default network for vLLM access"
```

---

## Task C: Update E2E client (`tools/client/main.go`) to support inferences

**Files:**
- Modify: `tools/client/main.go` (lines 26–75 for structs, lines 80–122 for CLI args, lines 178–190 for usage, line 215 for request building)

### Overview

The client's local structs don't match the backend API schema. We need to:

1. Add `Features []string` to `CreateJobRequest`
2. Add `Steps map[string]string` to `GetJobResponse`
3. Add `MicroInferences []ChunkInferences` to `JobResults`
4. Define `MicroInference` and `ChunkInferences` structs
5. Add `--inferences` boolean CLI flag
6. Pass `features: ["inferences"]` if flag is set
7. Display inference summary in output

- [ ] **Step 1: Add MicroInference and ChunkInferences structs**

After `Entity` struct (line 74), add:

```go
type MicroInference struct {
	Text       string   `json:"text"`
	Confidence float32  `json:"confidence"`
	Entities   []string `json:"entities,omitempty"`
}

type ChunkInferences struct {
	ChunkID    interface{}      `json:"chunk_id"`
	Inferences []MicroInference `json:"inferences"`
}
```

- [ ] **Step 2: Add Features field to CreateJobRequest**

In `CreateJobRequest` struct (lines 26–30), add after `Filename`:

```go
	Features []string `json:"features,omitempty"`
```

- [ ] **Step 3: Add Steps field to GetJobResponse**

In `GetJobResponse` struct (lines 38–44), add after `Error`:

```go
	Steps map[string]string `json:"steps,omitempty"`
```

- [ ] **Step 4: Add MicroInferences field to JobResults**

In `JobResults` struct (lines 46–57), add at the end before closing brace:

```go
	MicroInferences []ChunkInferences `json:"micro_inferences,omitempty"`
```

- [ ] **Step 5: Add --inferences flag to CLI parser**

In `main()` function (lines 80–86), add after `showHelp` declaration:

```go
		inferencesEnabled bool
```

Then in the switch statement (lines 90–122), add a new case after the `-u` handling (before `default`):

```go
		case "-f", "--inferences":
			inferencesEnabled = true
```

- [ ] **Step 6: Update CreateJobRequest to include features**

In `uploadDocument()` function (lines 215–218), update the request body building from:

```go
	reqBody := CreateJobRequest{
		DocumentBase64: documentBase64,
		Filename:       filename,
	}
```

To pass inferencesEnabled as a parameter and conditionally add features. First, update the function signature (line 192):

```go
func uploadDocument(ctx context.Context, apiURL string, inputFile string, inferencesEnabled bool) (string, error) {
```

Then update the request building (around line 215):

```go
	reqBody := CreateJobRequest{
		DocumentBase64: documentBase64,
		Filename:       filename,
	}
	if inferencesEnabled {
		reqBody.Features = []string{"inferences"}
	}
```

- [ ] **Step 7: Update main() to pass inferencesEnabled**

Update the call to `uploadDocument()` (line 151) from:

```go
	jobID, err := uploadDocument(ctx, apiURL, inputFile)
```

To:

```go
	jobID, err := uploadDocument(ctx, apiURL, inputFile, inferencesEnabled)
```

- [ ] **Step 8: Add inferences display to downloadResults()**

In `downloadResults()` function (lines 364–415), after writing the file (line 397), add a summary display if inferences exist:

```go
	// Display inference summary if present
	if result.Results != nil && len(result.Results.MicroInferences) > 0 {
		fmt.Printf("\nInferences generated for %d chunks:\n", len(result.Results.MicroInferences))
		for _, ci := range result.Results.MicroInferences {
			fmt.Printf("  Chunk %v: %d inferences\n", ci.ChunkID, len(ci.Inferences))
		}
	}
```

- [ ] **Step 9: Update printUsage()**

Update the usage output (lines 178–190) to document the new `--inferences` flag:

Change the Options section from:

```go
	fmt.Println("Options:")
	fmt.Println("  -i, --input <file>     Path to document file or URL (required)")
	fmt.Println("  -o, --output <file>    Path to save results JSON (required)")
	fmt.Println("  -u, --url <url>        API base URL (default: http://localhost:8080)")
	fmt.Println("  -h, --help             Show this help message")
```

To:

```go
	fmt.Println("Options:")
	fmt.Println("  -i, --input <file>     Path to document file or URL (required)")
	fmt.Println("  -o, --output <file>    Path to save results JSON (required)")
	fmt.Println("  -u, --url <url>        API base URL (default: http://localhost:8080)")
	fmt.Println("  -f, --inferences       Enable inference generation (requires vLLM)")
	fmt.Println("  -h, --help             Show this help message")
```

- [ ] **Step 10: Verify no compilation errors**

Run: `cd /home/hp/Proyectos/ia-text-ochestrator/tools/client && go build -o client . && echo "Build OK"`

Expected: `Build OK` with no errors.

- [ ] **Step 11: Commit**

```bash
cd /home/hp/Proyectos/ia-text-ochestrator
git add tools/client/main.go
git commit -m "feat: add inference support to E2E client"
```

---

## Task D: E2E verification test

**Files:** None (manual verification)

### Overview

We'll rebuild the inference-worker image with the new network config, start it, compile the client with the new features, and run a real end-to-end inference test using a small public PDF.

- [ ] **Step 1: Rebuild inference-worker image**

Run: `cd /home/hp/Proyectos/ia-text-ochestrator && docker compose -f deploy/docker/docker-compose.yml build inference-worker`

Expected: Docker builds successfully with no errors. Image should include the latest code if any Python changes were made.

- [ ] **Step 2: Start inference-worker**

Run: `cd /home/hp/Proyectos/ia-text-ochestrator && docker compose -f deploy/docker/docker-compose.yml up -d inference-worker`

Expected: Service starts without errors. Verify: `docker compose ps | grep inference-worker` should show `Up` status.

- [ ] **Step 3: Verify inference-worker can reach vLLM**

Run: `docker compose logs inference-worker | tail -20`

Check for connection errors. If no errors and you see "Listening" or "Started", it's ready.

- [ ] **Step 4: Compile the updated E2E client**

Run: `cd /home/hp/Proyectos/ia-text-ochestrator/tools/client && go build -o client .`

Expected: Binary `client` created with no errors.

- [ ] **Step 5: Download a small test PDF**

Run: 
```bash
cd /tmp
curl -L -o test.pdf "https://www.w3.org/WAI/WCAG21/Techniques/pdf/img/pdffill.pdf"
```

Or use any other small public PDF. Expected: File downloaded, ~10 KB.

- [ ] **Step 6: Run E2E test with --inferences flag**

Run: 
```bash
cd /home/hp/Proyectos/ia-text-ochestrator/tools/client
./client -i /tmp/test.pdf -o /tmp/inference_results.json --inferences
```

Expected output:
```
Preparing document upload...
Uploading document: pdffill.pdf
Job created: <job-id>
Monitoring job progress...
Status: pending ⠋ Status: extracting ⠙ ... Status: completed
Downloading results...
Inferences generated for N chunks:
  Chunk chunk_0: M inferences
  ...
Process completed in: <duration>

Results saved to: /tmp/inference_results.json
```

- [ ] **Step 7: Verify results JSON contains micro_inferences**

Run: `jq '.micro_inferences | length' /tmp/inference_results.json`

Expected: Number > 0 (indicates inferences were generated and included in results).

- [ ] **Step 8: Inspect one inference entry (optional)**

Run: `jq '.micro_inferences[0]' /tmp/inference_results.json`

Expected structure:
```json
{
  "chunk_id": "chunk_0",
  "inferences": [
    {
      "text": "...",
      "confidence": 0.95,
      "entities": [...]
    }
  ]
}
```

---

## Summary

| Task | Changes | Commits |
|------|---------|---------|
| A | `.env` + `.env.example` | 1 commit: chore vars |
| B | `docker-compose.yml` network config | 1 commit: feat network |
| C | `tools/client/main.go` structs + flags | 1 commit: feat client |
| D | Manual verification (no commits) | — |

**Total: 3 commits** (all can be done atomically task-by-task)

**Prerequisites met:**
- ✅ vLLM running in `docker_default` (verified at plan time)
- ✅ Backend already supports `Features` in CreateJobRequest
- ✅ Backend already exposes `Steps` and `MicroInferences` in GetJobResponse
- ✅ inference-worker already reads `LLM_URL` and `LLM_MODEL`

**After completion:**
- Developers can deploy with clear env var defaults
- inference-worker has network access to vLLM
- E2E client can request and display inferences
- Feature verified end-to-end with a real test
