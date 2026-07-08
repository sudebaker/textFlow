package main

import (
	"bufio"
	"bytes"
	"compress/gzip"
	"context"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"mime/multipart"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"
)

const (
	pollingInterval = 3 * time.Second
	defaultTimeout  = 1 * time.Hour
	defaultAPIURL   = "http://localhost:8080"
	defaultMaxInflight = 5
	defaultRetryBackoff = 2 * time.Second
	maxRetries = 5
)

var audioExtensions = map[string]bool{
	".mp3": true, ".wav": true, ".m4a": true, ".ogg": true,
}

var imageExtensions = map[string]bool{
	".jpg": true, ".jpeg": true, ".png": true,
}

type CreateJobRequest struct {
	DocumentBase64 string   `json:"document_base64,omitempty"`
	DocumentURL    string   `json:"document_url,omitempty"`
	Filename       string   `json:"filename,omitempty"`
	Features       []string `json:"features,omitempty"`
	WebhookURL     string   `json:"webhook_url,omitempty"`
	WebhookSecret  string   `json:"webhook_secret,omitempty"`
}

type CreateJobResponse struct {
	JobID     string `json:"job_id"`
	Status    string `json:"status"`
	StatusURL string `json:"status_url"`
}

type GetJobResponse struct {
	JobID       string            `json:"job_id"`
	Status      string            `json:"status"`
	Results     *JobResults       `json:"results,omitempty"`
	Error       string            `json:"error,omitempty"`
	CreatedAt   time.Time         `json:"created_at"`
	Steps       map[string]string `json:"steps,omitempty"`
	CurrentStep string            `json:"current_step,omitempty"`
}

// JobProgress tracks real-time job progress information
type JobProgress struct {
	Status          string            `json:"status"`
	CurrentStep     string            `json:"current_step"`
	Steps           map[string]string `json:"steps"`
	ChunksProcessed int
	TotalChunks     int
	Entities        int
	Inferences      int
	Error           string
}

type JobResults struct {
	JobID            string                   `json:"job_id"`
	Status           string                   `json:"status"`
	CreatedAt        string                   `json:"created_at"`
	CompletedAt      string                   `json:"completed_at"`
	Text             string                   `json:"text"`
	Chunks           []Chunk                  `json:"chunks,omitempty"`
	Entities         map[string]EntityMinimal `json:"entities,omitempty"`
	DocumentMetadata map[string]interface{}   `json:"document_metadata,omitempty"`
	TextMetadata     map[string]interface{}   `json:"text_metadata,omitempty"`
}

type Chunk struct {
	ChunkID             string          `json:"chunk_id"`
	Text                string          `json:"text"`
	StartOffset         int             `json:"start_offset"`
	EndOffset           int             `json:"end_offset"`
	TokenCount          int             `json:"token_count,omitempty"`
	Embeddings          []float32       `json:"embeddings,omitempty"`
	EmbeddingCompressed string          `json:"embedding_compressed,omitempty"`
	EntityIDs           []string        `json:"entity_ids,omitempty"`
	Inferences          []InferenceItem `json:"inferences,omitempty"`
}

type EntityMinimal struct {
	Label       string  `json:"label"`
	Text        string  `json:"text"`
	Confidence  float32 `json:"confidence"`
	StartOffset int     `json:"start_offset,omitempty"`
	EndOffset   int     `json:"end_offset,omitempty"`
}

type InferenceItem struct {
	Text       string    `json:"text"`
	Confidence float32   `json:"confidence"`
	EntityRefs []string  `json:"entity_refs,omitempty"`
	EntityID   string    `json:"entity_id,omitempty"`
	Embedding  []float32 `json:"embedding,omitempty"`
}

type MicroInference struct {
	Text       string   `json:"text"`
	Confidence float32  `json:"confidence"`
	EntityRefs []string `json:"entity_refs,omitempty"`
}

type ChunkInferences struct {
	ChunkID    interface{}      `json:"chunk_id"`
	Inferences []MicroInference `json:"inferences"`
}

// Batch types
type BatchDocument struct {
	Text     string                 `json:"text"`
	Filename string                 `json:"filename,omitempty"`
	Metadata map[string]interface{} `json:"metadata,omitempty"`
}

type BatchRequest struct {
	Documents      []BatchDocument `json:"documents"`
	MaxConcurrency int             `json:"max_concurrency,omitempty"`
	WebhookURL     string          `json:"webhook_url,omitempty"`
	WebhookSecret  string          `json:"webhook_secret,omitempty"`
}

type BatchJobRef struct {
	ID       string `json:"id"`
	Filename string `json:"filename,omitempty"`
	Status   string `json:"status"`
}

type BatchResponse struct {
	BatchID   string        `json:"batch_id"`
	Total     int           `json:"total"`
	Jobs      []BatchJobRef `json:"jobs"`
	StatusURL string        `json:"status_url"`
	CreatedAt time.Time     `json:"created_at"`
}

type BatchStatusResponse struct {
	BatchID   string        `json:"batch_id"`
	Status    string        `json:"status"`
	Total     int           `json:"total"`
	Completed int           `json:"completed"`
	Failed    int           `json:"failed"`
	Pending   int           `json:"pending"`
	Jobs      []BatchJobRef `json:"jobs"`
	CreatedAt time.Time     `json:"created_at"`
}

// SSE Event types
type JobEvent struct {
	JobID     string    `json:"job_id"`
	Status    string    `json:"status"`
	Progress  float64   `json:"progress,omitempty"`
	Timestamp time.Time `json:"timestamp"`
	Error     string    `json:"error,omitempty"`
}

var (
	spinner = []string{"⠋", "⠙", "⠹", "⠸", "⠼", "⠒", "⠂", "⠂", "⠒", "⠲", "⠴", "⠤", "⠄", "⠄", "⠤", "⠴", "⠶", "⠦", "⠰", "⠠", "⠰", "⠦", "⠶", "⠴", "⠤", "⠄", "⠄", "⠤", "⠴", "⠶", "⠦", "⠰"}
)

// printStatus prints a status update and flushes stdout
func printStatus(format string, args ...interface{}) {
	fmt.Printf(format, args...)
	// Force flush to stdout - some terminals buffer \r updates
	os.Stdout.Sync()
}

func main() {
	var (
		inputFile         string
		outputFile        string
		apiURL            string
		showHelp          bool
		inferencesEnabled bool
		webhookURL        string
		webhookSecret     string
		useSSE            bool
		useBatch          bool
		batchFile         string
		timeoutStr        string
		resumeJobID       string
		diarizeEnabled    bool
		maxInflight       int
		sequential        bool
		retryBackoffStr   string
	)

	args := os.Args[1:]
	for i := 0; i < len(args); i++ {
		switch args[i] {
		case "-h", "--help":
			showHelp = true
		case "-i", "--input":
			if i+1 >= len(args) {
				fmt.Println("Error: -i/--input requires a value")
				printUsage()
				os.Exit(1)
			}
			inputFile = args[i+1]
			i++
		case "-o", "--output":
			if i+1 >= len(args) {
				fmt.Println("Error: -o/--output requires a value")
				printUsage()
				os.Exit(1)
			}
			outputFile = args[i+1]
			i++
		case "-u", "--url":
			if i+1 >= len(args) {
				fmt.Println("Error: -u/--url requires a value")
				printUsage()
				os.Exit(1)
			}
			apiURL = args[i+1]
			i++
		case "-f", "--inferences":
			inferencesEnabled = true
		case "-w", "--webhook":
			if i+1 >= len(args) {
				fmt.Println("Error: -w/--webhook requires a value")
				printUsage()
				os.Exit(1)
			}
			webhookURL = args[i+1]
			i++
		case "--webhook-secret":
			if i+1 >= len(args) {
				fmt.Println("Error: --webhook-secret requires a value")
				printUsage()
				os.Exit(1)
			}
			webhookSecret = args[i+1]
			i++
		case "--sse":
			useSSE = true
		case "-b", "--batch":
			useBatch = true
			if i+1 < len(args) && !strings.HasPrefix(args[i+1], "-") {
				batchFile = args[i+1]
				i++
			}
		case "--timeout":
			if i+1 >= len(args) {
				fmt.Println("Error: --timeout requires a value")
				printUsage()
				os.Exit(1)
			}
			timeoutStr = args[i+1]
			i++
		case "--job-id":
			if i+1 >= len(args) {
				fmt.Println("Error: --job-id requires a value")
				printUsage()
				os.Exit(1)
			}
			resumeJobID = args[i+1]
			i++
		case "--diarize":
			diarizeEnabled = true
		case "--max-inflight":
			if i+1 >= len(args) {
				fmt.Println("Error: --max-inflight requires a value")
				printUsage()
				os.Exit(1)
			}
			fmt.Sscanf(args[i+1], "%d", &maxInflight)
			i++
		case "--sequential":
			sequential = true
		case "--retry-backoff":
			if i+1 >= len(args) {
				fmt.Println("Error: --retry-backoff requires a value")
				printUsage()
				os.Exit(1)
			}
			retryBackoffStr = args[i+1]
			i++
		default:
			fmt.Printf("Unknown argument: %s\n", args[i])
			printUsage()
			os.Exit(1)
		}
	}

	if showHelp {
		printUsage()
		os.Exit(0)
	}

	if apiURL == "" {
		apiURL = defaultAPIURL
	}

	// Apply sequential override
	if sequential {
		maxInflight = 1
	}
	if maxInflight <= 0 {
		maxInflight = defaultMaxInflight
	}

	retryBackoff := defaultRetryBackoff
	if retryBackoffStr != "" {
		parsed, err := time.ParseDuration(retryBackoffStr)
		if err != nil {
			fmt.Printf("Error: Invalid retry-backoff value '%s': %v\n", retryBackoffStr, err)
			printUsage()
			os.Exit(1)
		}
		retryBackoff = parsed
	}

	// Validate required arguments based on mode
	if useBatch {
		if batchFile == "" && inputFile == "" {
			fmt.Println("Error: Batch mode requires either -b/--batch <file> or -i/--input <file>")
			printUsage()
			os.Exit(1)
		}
		if batchFile == "" {
			batchFile = inputFile
		}
		if outputFile == "" {
			fmt.Println("Error: Batch mode requires -o/--output")
			printUsage()
			os.Exit(1)
		}
	} else if resumeJobID != "" {
		if outputFile == "" {
			fmt.Println("Error: --job-id requires -o/--output")
			printUsage()
			os.Exit(1)
		}
	} else {
		if inputFile == "" || outputFile == "" {
			printUsage()
			os.Exit(1)
		}
	}

	timeout := defaultTimeout
	if timeoutStr != "" {
		parsed, err := time.ParseDuration(timeoutStr)
		if err != nil {
			fmt.Printf("Error: Invalid timeout value '%s': %v\n", timeoutStr, err)
			printUsage()
			os.Exit(1)
		}
		timeout = parsed
	}

	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()

	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, syscall.SIGINT, syscall.SIGTERM)

	go func() {
		<-sigChan
		fmt.Println("\nProcess interrupted by user")
		cancel()
		os.Exit(130)
	}()

	start := time.Now()

	if useBatch {
		err := runBatchMode(ctx, apiURL, batchFile, outputFile, webhookURL, webhookSecret, maxInflight, retryBackoff)
		if err != nil {
			fmt.Printf("Error in batch mode: %v\n", err)
			os.Exit(1)
		}
	} else if resumeJobID != "" {
		fmt.Printf("Resuming job: %s\n", resumeJobID)

		// Check current status first
		status, _, err := getJobStatus(ctx, apiURL, resumeJobID)
		if err != nil {
			fmt.Printf("Error checking job status: %v\n", err)
			os.Exit(1)
		}

		// If still processing, monitor until done
		if status == "processing" || status == "pending" {
			fmt.Printf("Job is still %s, waiting for completion...\n", status)
			if useSSE {
				status, err = monitorJobSSE(ctx, apiURL, resumeJobID)
			} else {
				status, err = monitorJob(ctx, apiURL, resumeJobID)
			}
			if err != nil {
				fmt.Printf("Error monitoring job: %v\n", err)
				os.Exit(1)
			}
		}

		if status != "completed" {
			fmt.Printf("Job failed with status: %s\n", status)
			os.Exit(1)
		}

		err = downloadResults(ctx, apiURL, resumeJobID, outputFile)
		if err != nil {
			fmt.Printf("Error downloading results: %v\n", err)
			os.Exit(1)
		}

		fmt.Printf("\nResults saved to: %s\n", outputFile)
	} else {
		jobID, err := uploadDocument(ctx, apiURL, inputFile, inferencesEnabled, webhookURL, webhookSecret, diarizeEnabled, retryBackoff)
		if err != nil {
			fmt.Printf("Error uploading document: %v\n", err)
			os.Exit(1)
		}

		var status string
		if useSSE {
			status, err = monitorJobSSE(ctx, apiURL, jobID)
		} else {
			status, err = monitorJob(ctx, apiURL, jobID)
		}
		if err != nil {
			fmt.Printf("Error monitoring job: %v\n", err)
			os.Exit(1)
		}

		if status != "completed" {
			fmt.Printf("Job failed with status: %s\n", status)
			os.Exit(1)
		}

		err = downloadResults(ctx, apiURL, jobID, outputFile)
		if err != nil {
			fmt.Printf("Error downloading results: %v\n", err)
			os.Exit(1)
		}

		fmt.Printf("\nResults saved to: %s\n", outputFile)
	}

	fmt.Printf("Total time: %s\n", time.Since(start).Round(time.Millisecond))
	os.Exit(0)
}

func printUsage() {
	fmt.Println("Usage: client [options]")
	fmt.Println("")
	fmt.Println("Options:")
	fmt.Println("  -i, --input <file>          Path to document, audio, or image file (required for single mode)")
	fmt.Println("  -o, --output <file>         Path to save results JSON (required)")
	fmt.Println("  -u, --url <url>             API base URL (default: http://localhost:8080)")
	fmt.Println("  -f, --inferences            Enable inference generation with vector embeddings (requires vLLM)")
	fmt.Println("  -w, --webhook <url>         Webhook URL for job completion notification")
	fmt.Println("  --webhook-secret <secret>   Secret for webhook signature verification")
	fmt.Println("  --diarize                  Enable speaker diarization for audio files (Whisper)")
	fmt.Println("  --sse                      Use SSE streaming instead of polling")
	fmt.Println("  --timeout <duration>       Timeout for entire operation (default: 10m)")
	fmt.Println("  -b, --batch [file]         Batch processing mode (reads JSON file with documents)")
	fmt.Println("  --job-id <id>              Resume or download results for an existing job ID")
	fmt.Println("  --max-inflight <n>         Max concurrent jobs in flight (default: 5)")
	fmt.Println("  --sequential               Alias for --max-inflight 1")
	fmt.Println("  --retry-backoff <duration> Base backoff for retries (default: 2s)")
	fmt.Println("  -h, --help                 Show this help message")
	fmt.Println("")
	fmt.Println("Single Job Mode:")
	fmt.Println("  client -i /path/to/file.pdf -o /path/to/output.json")
	fmt.Println("  client -i https://example.com/file.pdf -o output.json -w https://myapp.com/webhook")
	fmt.Println("  client -i /path/to/file.pdf -o output.json --sse")
	fmt.Println("  client -i /path/to/large.pdf -o output.json --timeout 1h")
	fmt.Println("")
	fmt.Println("Inference Mode:")
	fmt.Println("  client -i /path/to/file.pdf -o output.json -f")
	fmt.Println("")
	fmt.Println("Audio Files (MP3, WAV, M4A, OGG):")
	fmt.Println("  client -i /path/to/audio.mp3 -o output.json")
	fmt.Println("  client -i /path/to/audio.wav -o output.json --diarize")
	fmt.Println("")
	fmt.Println("Image Files (JPG, JPEG, PNG):")
	fmt.Println("  client -i /path/to/image.jpg -o output.json")
	fmt.Println("  client -i /path/to/photo.png -o output.json")
	fmt.Println("")
	fmt.Println("Resume Mode:")
	fmt.Println("  client --job-id <id> -o output.json")
	fmt.Println("")
	fmt.Println("Batch Mode:")
	fmt.Println("  client -b documents.json -o results.json")
	fmt.Println("  client -b documents.json -o results.json -w https://myapp.com/webhook")
	fmt.Println("  client -b documents.json -o results.json --max-inflight 3")
	fmt.Println("  client -b documents.json -o results.json --sequential")
}

func uploadDocument(ctx context.Context, apiURL string, inputFile string, inferencesEnabled bool, webhookURL, webhookSecret string, diarizeEnabled bool, retryBackoff time.Duration) (string, error) {
	if strings.HasPrefix(inputFile, "http://") || strings.HasPrefix(inputFile, "https://") {
		ext := strings.ToLower(filepath.Ext(inputFile))
		if audioExtensions[ext] || imageExtensions[ext] {
			return "", fmt.Errorf("audio/image URLs are not supported: use a local file path")
		}
	}

	if !strings.HasPrefix(inputFile, "http://") && !strings.HasPrefix(inputFile, "https://") {
		ext := strings.ToLower(filepath.Ext(inputFile))
		if audioExtensions[ext] || imageExtensions[ext] {
			return uploadFileMultipart(ctx, apiURL, inputFile, ext, diarizeEnabled, webhookURL, inferencesEnabled)
		}
	}

	fmt.Println("Preparing document upload...")

	var (
		documentBase64 string
		filename       string
		err            error
	)

	if strings.HasPrefix(inputFile, "http://") || strings.HasPrefix(inputFile, "https://") {
		documentBase64, filename, err = downloadAndEncode(ctx, inputFile)
		if err != nil {
			return "", fmt.Errorf("failed to download file: %w", err)
		}
	} else {
		documentBase64, filename, err = encodeFile(inputFile)
		if err != nil {
			return "", fmt.Errorf("failed to encode file: %w", err)
		}
	}

	fmt.Printf("Uploading document: %s\n", filename)

	reqBody := CreateJobRequest{
		DocumentBase64: documentBase64,
		Filename:       filename,
		WebhookURL:     webhookURL,
		WebhookSecret:  webhookSecret,
	}
	if inferencesEnabled {
		reqBody.Features = []string{"inferences"}
	}

	jsonData, err := json.Marshal(reqBody)
	if err != nil {
		return "", fmt.Errorf("failed to marshal request: %w", err)
	}

	// Retry loop with backoff for 503/429
	var nextWait time.Duration
	for retry := 0; retry <= maxRetries; retry++ {
		if retry > 0 {
			wait := nextWait
			if wait == 0 {
				wait = time.Duration(retry) * retryBackoff
			}
			nextWait = 0
			select {
			case <-ctx.Done():
				return "", ctx.Err()
			case <-time.After(wait):
			}
			fmt.Printf("Retrying upload (attempt %d/%d)...\n", retry+1, maxRetries+1)
		}

		req, err := http.NewRequestWithContext(ctx, "POST", apiURL+"/v1/documents/process", bytes.NewBuffer(jsonData))
		if err != nil {
			return "", fmt.Errorf("failed to create request: %w", err)
		}

		req.Header.Set("Content-Type", "application/json")

		client := &http.Client{Timeout: 30 * time.Second}
		resp, err := client.Do(req)
		if err != nil {
			return "", fmt.Errorf("failed to connect to API: %w", err)
		}

		if resp.StatusCode == http.StatusAccepted {
			var result CreateJobResponse
			if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
				resp.Body.Close()
				return "", fmt.Errorf("failed to parse response: %w", err)
			}
			resp.Body.Close()
			fmt.Printf("Job created: %s\n", result.JobID)
			return result.JobID, nil
		}

		// Handle 503/429 with Retry-After
		if resp.StatusCode == http.StatusServiceUnavailable || resp.StatusCode == http.StatusTooManyRequests {
			retryAfter := resp.Header.Get("Retry-After")
			resp.Body.Close()

			if retryAfter != "" {
				if seconds, err := time.ParseDuration(retryAfter + "s"); err == nil {
					nextWait = seconds
				}
			}
			fmt.Printf("Server busy (HTTP %d), Retry-After: %s, retry %d/%d\n",
				resp.StatusCode, retryAfter, retry+1, maxRetries)
			continue
		}

		// Other errors
		body, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		return "", fmt.Errorf("API returned status %d: %s", resp.StatusCode, string(body))
	}

	return "", fmt.Errorf("max retries exceeded")
}

func encodeFile(filePath string) (string, string, error) {
	data, err := os.ReadFile(filePath)
	if err != nil {
		return "", "", err
	}

	encoded := base64.StdEncoding.EncodeToString(data)
	filename := filepath.Base(filePath)

	return encoded, filename, nil
}

func downloadAndEncode(ctx context.Context, fileURL string) (string, string, error) {
	req, err := http.NewRequestWithContext(ctx, "GET", fileURL, nil)
	if err != nil {
		return "", "", err
	}

	client := &http.Client{Timeout: 60 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return "", "", err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return "", "", fmt.Errorf("download failed with status %d", resp.StatusCode)
	}

	data, err := io.ReadAll(resp.Body)
	if err != nil {
		return "", "", err
	}

	encoded := base64.StdEncoding.EncodeToString(data)

	parsedURL, err := url.Parse(fileURL)
	if err != nil {
		return "", "", err
	}
	filename := filepath.Base(parsedURL.Path)
	if filename == "" || filename == "/" {
		filename = "downloaded_file"
	}

	return encoded, filename, nil
}

func uploadFileMultipart(ctx context.Context, apiURL string, filePath string, ext string, diarizeEnabled bool, webhookURL string, inferencesEnabled bool) (string, error) {
	fmt.Printf("Uploading %s file via multipart: %s\n", strings.TrimPrefix(ext, "."), filePath)

	body := &bytes.Buffer{}
	writer := multipart.NewWriter(body)

	part, err := writer.CreateFormFile("file", filepath.Base(filePath))
	if err != nil {
		return "", fmt.Errorf("failed to create form file: %w", err)
	}

	fileData, err := os.Open(filePath)
	if err != nil {
		return "", fmt.Errorf("failed to open file: %w", err)
	}
	defer fileData.Close()

	if _, err := io.Copy(part, fileData); err != nil {
		return "", fmt.Errorf("failed to copy file data: %w", err)
	}

	if audioExtensions[ext] && diarizeEnabled {
		if err := writer.WriteField("diarize", "true"); err != nil {
			return "", fmt.Errorf("failed to write diarize field: %w", err)
		}
	}

	if webhookURL != "" {
		if err := writer.WriteField("notify_webhook", webhookURL); err != nil {
			return "", fmt.Errorf("failed to write webhook field: %w", err)
		}
	}

	if inferencesEnabled {
		if err := writer.WriteField("features", "inferences"); err != nil {
			return "", fmt.Errorf("failed to write features field: %w", err)
		}
	}

	if err := writer.Close(); err != nil {
		return "", fmt.Errorf("failed to close multipart writer: %w", err)
	}

	req, err := http.NewRequestWithContext(ctx, "POST", apiURL+"/v1/documents/upload", body)
	if err != nil {
		return "", fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Content-Type", writer.FormDataContentType())

	client := &http.Client{Timeout: 60 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return "", fmt.Errorf("failed to connect to API: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusAccepted {
		respBody, _ := io.ReadAll(resp.Body)
		return "", fmt.Errorf("API returned status %d: %s", resp.StatusCode, string(respBody))
	}

	var result CreateJobResponse
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return "", fmt.Errorf("failed to parse response: %w", err)
	}

	fmt.Printf("Job created: %s\n", result.JobID)

	return result.JobID, nil
}

func statusLabel(status, step string) string {
	switch status {
	case "transcribing":
		return "Transcribing audio (Whisper)..."
	case "analyzing_image":
		return "Analyzing image (LLM)..."
	case "processing":
		if step != "" {
			return fmt.Sprintf("Processing: %s", step)
		}
		return "Processing..."
	case "completed":
		return "✓ Completed"
	case "failed":
		return "✗ Failed"
	default:
		return status
	}
}

// formatProgressLabel creates a detailed progress label with all available metrics
func formatProgressLabel(progress *JobProgress) string {
	var parts []string

	// Status with symbol
	switch progress.Status {
	case "completed":
		parts = append(parts, "✓ Completed")
	case "failed":
		parts = append(parts, "✗ Failed")
	case "pending":
		parts = append(parts, "⏳ Pending")
	case "processing":
		parts = append(parts, "⚙ Processing")
	case "transcribing":
		parts = append(parts, "🎤 Transcribing")
	case "analyzing_image":
		parts = append(parts, "🖼 Analyzing")
	}

	// Current step
	if progress.CurrentStep != "" {
		parts = append(parts, fmt.Sprintf("(%s)", progress.CurrentStep))
	}

	// Progress metrics
	var metrics []string

	if progress.TotalChunks > 0 {
		pct := 0.0
		if progress.TotalChunks > 0 {
			pct = float64(progress.ChunksProcessed) / float64(progress.TotalChunks) * 100
		}
		metrics = append(metrics, fmt.Sprintf("chunks: %d/%d (%.0f%%)",
			progress.ChunksProcessed, progress.TotalChunks, pct))
	}

	if progress.Entities > 0 {
		metrics = append(metrics, fmt.Sprintf("entities: %d", progress.Entities))
	}

	if progress.Inferences > 0 {
		metrics = append(metrics, fmt.Sprintf("inferences: %d", progress.Inferences))
	}

	if len(metrics) > 0 {
		parts = append(parts, fmt.Sprintf("[%s]", strings.Join(metrics, " | ")))
	}

	return strings.Join(parts, " ")
}

func isTerminalStatus(status string) bool {
	return status == "completed" || status == "failed"
}

func isActiveStatus(status string) bool {
	return status == "pending" || status == "processing" || status == "transcribing" || status == "analyzing_image"
}

func monitorJob(ctx context.Context, apiURL string, jobID string) (string, error) {
	fmt.Println("Monitoring job progress...")

	pollingTicker := time.NewTicker(pollingInterval)
	defer pollingTicker.Stop()

	spinnerTicker := time.NewTicker(80 * time.Millisecond)
	defer spinnerTicker.Stop()

	var mu sync.RWMutex
	spinnerIndex := 0
	var lastProgress *JobProgress
	var lastLabel string

	for {
		select {
		case <-ctx.Done():
			if ctx.Err() == context.DeadlineExceeded {
				printStatus("\r⏱  Tiempo de espera excedido. El job continúa procesándose en el servidor.\n")
				printStatus("   Usa --job-id %s -o results.json para descargar cuando termine.\n", jobID)
				return "timeout", nil
			}
			printStatus("\r❌ Operación cancelada por usuario\n")
			return "", ctx.Err()
		case <-spinnerTicker.C:
			mu.RLock()
			progress := lastProgress
			label := lastLabel
			mu.RUnlock()
			if progress != nil {
				spinnerIndex = (spinnerIndex + 1) % len(spinner)
				printStatus("\r%s %s", spinner[spinnerIndex], label)
			}
		case <-pollingTicker.C:
			progress, err := getJobProgress(ctx, apiURL, jobID)
			if err != nil {
				if ctx.Err() != nil {
					continue
				}
				progress = &JobProgress{
					Status: "error",
					Error:  err.Error(),
				}
			}
			mu.Lock()
			lastProgress = progress
			lastLabel = formatProgressLabel(lastProgress)
			mu.Unlock()

			if isTerminalStatus(progress.Status) {
				if progress.Status == "completed" {
					printStatus("\r✓ %s\n", lastLabel)
				} else if progress.Status == "failed" {
					printStatus("\r✗ Job falló: %s\n", progress.Error)
				} else {
					printStatus("\r✗ %s\n", lastLabel)
				}
				return progress.Status, nil
			}
		}
	}
}

func runBatchMode(ctx context.Context, apiURL, batchFile, outputFile, webhookURL, webhookSecret string, maxInflight int, retryBackoff time.Duration) error {
	fmt.Println("Reading batch file...")

	data, err := os.ReadFile(batchFile)
	if err != nil {
		return fmt.Errorf("failed to read batch file: %w", err)
	}

	var batchReq BatchRequest
	if err := json.Unmarshal(data, &batchReq); err != nil {
		return fmt.Errorf("failed to parse batch file: %w", err)
	}

	if len(batchReq.Documents) == 0 {
		return fmt.Errorf("batch file must contain at least one document")
	}

	if webhookURL != "" {
		batchReq.WebhookURL = webhookURL
		batchReq.WebhookSecret = webhookSecret
	}

	fmt.Printf("Processing %d documents (max-inflight: %d)...\n", len(batchReq.Documents), maxInflight)

	totalJobs := 0
	failedJobs := 0
	startTime := time.Now()

	for i := 0; i < len(batchReq.Documents); i += maxInflight {
		end := i + maxInflight
		if end > len(batchReq.Documents) {
			end = len(batchReq.Documents)
		}
		chunk := batchReq.Documents[i:end]

		chunkReq := BatchRequest{
			Documents:      chunk,
			MaxConcurrency: batchReq.MaxConcurrency,
			WebhookURL:     batchReq.WebhookURL,
			WebhookSecret:  batchReq.WebhookSecret,
		}

		chunkNum := (i / maxInflight) + 1
		totalChunks := (len(batchReq.Documents) + maxInflight - 1) / maxInflight
		fmt.Printf("\n--- Chunk %d/%d (%d documents) ---\n", chunkNum, totalChunks, len(chunk))

		chunkJSON, err := json.Marshal(chunkReq)
		if err != nil {
			return fmt.Errorf("failed to marshal chunk: %w", err)
		}

		var nextWait time.Duration
		success := false
		for retry := 0; retry <= maxRetries; retry++ {
			if retry > 0 {
				wait := nextWait
				if wait == 0 {
					wait = time.Duration(retry) * retryBackoff
				}
				nextWait = 0
				select {
				case <-ctx.Done():
					return ctx.Err()
				case <-time.After(wait):
				}
				fmt.Printf("  Retrying chunk (attempt %d/%d)...\n", retry+1, maxRetries+1)
			}

			req, err := http.NewRequestWithContext(ctx, "POST", apiURL+"/v1/documents/batch", bytes.NewBuffer(chunkJSON))
			if err != nil {
				return fmt.Errorf("failed to create request: %w", err)
			}
			req.Header.Set("Content-Type", "application/json")

			client := &http.Client{Timeout: 60 * time.Second}
			resp, err := client.Do(req)
			if err != nil {
				return fmt.Errorf("failed to connect to API: %w", err)
			}

			if resp.StatusCode == http.StatusAccepted {
				var result BatchResponse
				if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
					resp.Body.Close()
					return fmt.Errorf("failed to parse response: %w", err)
				}
				resp.Body.Close()

				fmt.Printf("  Batch created: %s (%d jobs)\n", result.BatchID, result.Total)
				totalJobs += result.Total

				_, err := monitorBatch(ctx, apiURL, result.BatchID)
				if err != nil {
					return fmt.Errorf("error monitoring batch: %w", err)
				}

				finalStatus, err := getBatchStatus(ctx, apiURL, result.BatchID)
				if err != nil {
					return fmt.Errorf("failed to get batch status: %w", err)
				}

				fmt.Printf("  Chunk completed: %d/%d done, %d failed\n",
					finalStatus.Completed, finalStatus.Total, finalStatus.Failed)
				failedJobs += finalStatus.Failed
				success = true
				break
			}

			// Handle 503/429
			if resp.StatusCode == http.StatusServiceUnavailable || resp.StatusCode == http.StatusTooManyRequests {
				retryAfter := resp.Header.Get("Retry-After")
				resp.Body.Close()
				if retryAfter != "" {
					if seconds, err := time.ParseDuration(retryAfter + "s"); err == nil {
						nextWait = seconds
					}
				}
				fmt.Printf("  Server busy (HTTP %d), Retry-After: %s, retry %d/%d\n",
					resp.StatusCode, retryAfter, retry+1, maxRetries)
				continue
			}

			body, _ := io.ReadAll(resp.Body)
			resp.Body.Close()
			return fmt.Errorf("API returned status %d: %s", resp.StatusCode, string(body))
		}

		if !success {
			failedJobs += len(chunk)
			fmt.Printf("  Chunk failed after %d retries\n", maxRetries+1)
		}
	}

	// Write summary
	summary := map[string]interface{}{
		"total":     len(batchReq.Documents),
		"completed": len(batchReq.Documents) - failedJobs,
		"failed":    failedJobs,
		"duration":  time.Since(startTime).String(),
	}
	outputData, _ := json.MarshalIndent(summary, "", "  ")
	os.WriteFile(outputFile, outputData, 0644)

	fmt.Printf("\nBatch completed in %s\n", time.Since(startTime).Round(time.Second))
	fmt.Printf("Summary: %d completed, %d failed, %d total\n",
		len(batchReq.Documents)-failedJobs, failedJobs, len(batchReq.Documents))
	fmt.Printf("Results saved to: %s\n", outputFile)
	return nil
}

func monitorBatch(ctx context.Context, apiURL, batchID string) (string, error) {
	fmt.Println("Monitoring batch progress...")

	pollingTicker := time.NewTicker(pollingInterval)
	defer pollingTicker.Stop()

	spinnerTicker := time.NewTicker(80 * time.Millisecond)
	defer spinnerTicker.Stop()

	var mu sync.RWMutex
	spinnerIndex := 0
	var lastProgress *JobProgress
	var lastLabel string

	for {
		select {
		case <-ctx.Done():
			if ctx.Err() == context.DeadlineExceeded {
				printStatus("\r⏱  Tiempo de espera excedido. El batch continúa procesándose.\n")
				return "timeout", nil
			}
			printStatus("\r❌ Operación cancelada por usuario\n")
			return "", ctx.Err()
		case <-spinnerTicker.C:
			mu.RLock()
			progress := lastProgress
			label := lastLabel
			mu.RUnlock()
			if progress != nil {
				spinnerIndex = (spinnerIndex + 1) % len(spinner)
				printStatus("\r%s %s", spinner[spinnerIndex], label)
			}
		case <-pollingTicker.C:
			status, err := getBatchStatus(ctx, apiURL, batchID)
			if err != nil {
				if ctx.Err() != nil {
					continue
				}
				lastProgress = &JobProgress{
					Status: "error",
					Error:  err.Error(),
				}
			} else {
				lastProgress = &JobProgress{
					Status:          status.Status,
					ChunksProcessed: status.Completed,
					TotalChunks:     status.Total,
				}
				if status.Failed > 0 {
					lastProgress.Error = fmt.Sprintf("%d failed", status.Failed)
				}
			}

			mu.Lock()
			lastLabel = formatProgressLabel(lastProgress)
			mu.Unlock()

			if status.Status == "completed" || status.Status == "failed" || status.Status == "partial" {
				var finalLabel string
				if status.Status == "completed" {
					finalLabel = fmt.Sprintf("✓ Batch completed: %d/%d done, %d failed",
						status.Completed, status.Total, status.Failed)
				} else if status.Status == "failed" {
					finalLabel = fmt.Sprintf("✗ Batch failed: %d/%d done, %d failed",
						status.Completed, status.Total, status.Failed)
				} else {
					finalLabel = fmt.Sprintf("⚠ Batch partial: %d/%d done, %d failed",
						status.Completed, status.Total, status.Failed)
				}
				printStatus("\r%s\n", finalLabel)
				return status.Status, nil
			}
		}
	}
}

func getBatchStatus(ctx context.Context, apiURL, batchID string) (*BatchStatusResponse, error) {
	req, err := http.NewRequestWithContext(ctx, "GET", apiURL+"/v1/batches/"+batchID+"/status", nil)
	if err != nil {
		return nil, err
	}

	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusNotFound {
		return nil, fmt.Errorf("batch not found: %s", batchID)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("API returned status %d", resp.StatusCode)
	}

	var result BatchStatusResponse
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, err
	}

	return &result, nil
}

func monitorJobSSE(ctx context.Context, apiURL string, jobID string) (string, error) {
	fmt.Println("Monitoring job progress via SSE...")

	req, err := http.NewRequestWithContext(ctx, "GET", apiURL+"/v1/jobs/"+jobID+"/stream", nil)
	if err != nil {
		return "", fmt.Errorf("failed to create SSE request: %w", err)
	}

	req.Header.Set("Accept", "text/event-stream")
	req.Header.Set("Cache-Control", "no-cache")

	client := &http.Client{Timeout: 0}
	resp, err := client.Do(req)
	if err != nil {
		return "", fmt.Errorf("failed to connect to SSE stream: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		return "", fmt.Errorf("SSE stream returned status %d: %s", resp.StatusCode, string(body))
	}

	reader := bufio.NewReader(resp.Body)

	spinnerTicker := time.NewTicker(80 * time.Millisecond)
	defer spinnerTicker.Stop()

	var mu sync.RWMutex
	spinnerIndex := 0
	lastStatus := "pending"
	var lastProgress *JobProgress
	var lastLabel string

	lastProgress = &JobProgress{Status: lastStatus}
	lastLabel = formatProgressLabel(lastProgress)
	printStatus("%s %s", spinner[0], lastLabel)

	for {
		select {
		case <-ctx.Done():
			if ctx.Err() == context.DeadlineExceeded {
				printStatus("\r⏱  Tiempo de espera excedido. El job continúa procesándose en el servidor.\n")
				printStatus("   Usa --job-id %s -o results.json para descargar cuando termine.\n", jobID)
				return "timeout", nil
			}
			printStatus("\r❌ Operación cancelada por usuario\n")
			return "", ctx.Err()
		case <-spinnerTicker.C:
			mu.RLock()
			label := lastLabel
			mu.RUnlock()
			spinnerIndex = (spinnerIndex + 1) % len(spinner)
			printStatus("\r%s %s", spinner[spinnerIndex], label)
		default:
			line, err := reader.ReadString('\n')
			if err != nil {
				if err == io.EOF {
					mu.RLock()
					printStatus("\r✓ %s\n", lastLabel)
					mu.RUnlock()
					return lastStatus, nil
				}
				printStatus("\r❌ Error en stream SSE: %v\n", err)
				return lastStatus, fmt.Errorf("error reading SSE stream: %w", err)
			}

			line = strings.TrimSpace(line)
			if line == "" || strings.HasPrefix(line, ":") {
				continue
			}

			if strings.HasPrefix(line, "data: ") {
				data := strings.TrimPrefix(line, "data: ")

				var event JobEvent
				if err := json.Unmarshal([]byte(data), &event); err != nil {
					continue
				}

				lastStatus = event.Status
				mu.Lock()
				lastProgress = &JobProgress{
					Status: event.Status,
				}
				lastLabel = formatProgressLabel(lastProgress)
				mu.Unlock()

				if isTerminalStatus(event.Status) {
					if event.Status == "completed" {
						printStatus("\r✓ %s\n", lastLabel)
					} else {
						printStatus("\r✗ %s\n", lastLabel)
					}
					return event.Status, nil
				}
			}
		}
	}
}

func getJobStatus(ctx context.Context, apiURL string, jobID string) (string, string, error) {
	req, err := http.NewRequestWithContext(ctx, "GET", apiURL+"/v1/documents/"+jobID, nil)
	if err != nil {
		return "", "", err
	}

	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return "", "", err
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusNotFound {
		return "", "", fmt.Errorf("job not found: %s", jobID)
	}

	var result GetJobResponse
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return "", "", err
	}

	return result.Status, result.CurrentStep, nil
}

// getJobProgress retrieves full progress information including metrics
func getJobProgress(ctx context.Context, apiURL string, jobID string) (*JobProgress, error) {
	req, err := http.NewRequestWithContext(ctx, "GET", apiURL+"/v1/documents/"+jobID, nil)
	if err != nil {
		return nil, err
	}

	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusNotFound {
		return nil, fmt.Errorf("job not found: %s", jobID)
	}

	var result GetJobResponse
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, err
	}

	progress := &JobProgress{
		Status:      result.Status,
		CurrentStep: result.CurrentStep,
		Steps:       result.Steps,
		Error:       result.Error,
	}

	// Calculate metrics from results if available
	if result.Results != nil {
		progress.TotalChunks = len(result.Results.Chunks)
		progress.Entities = len(result.Results.Entities)

		// Count inferences across all chunks
		for _, chunk := range result.Results.Chunks {
			progress.Inferences += len(chunk.Inferences)
		}

		// Estimate chunks processed based on chunks with data
		processedCount := 0
		for _, chunk := range result.Results.Chunks {
			if len(chunk.Embeddings) > 0 || chunk.EmbeddingCompressed != "" || len(chunk.Inferences) > 0 {
				processedCount++
			}
		}
		progress.ChunksProcessed = processedCount
	}

	return progress, nil
}

func downloadResults(ctx context.Context, apiURL string, jobID string, outputFile string) error {
	fmt.Println("Downloading results...")

	var result JobResults
	var lastErr error

	for retry := 0; retry < 5; retry++ {
		if retry > 0 {
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(time.Duration(retry) * time.Second):
			}
		}

		req, err := http.NewRequestWithContext(ctx, "GET", apiURL+"/v1/documents/"+jobID+"/download", nil)
		if err != nil {
			return err
		}

		client := &http.Client{Timeout: 10 * time.Second}
		resp, err := client.Do(req)
		if err != nil {
			lastErr = err
			continue
		}

		if resp.StatusCode != http.StatusOK {
			resp.Body.Close()
			lastErr = fmt.Errorf("failed to get results: status %d", resp.StatusCode)
			continue
		}

		if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
			resp.Body.Close()
			lastErr = err
			continue
		}
		resp.Body.Close()

		// Validate that we have at least one of: text or chunks
		if len(result.Chunks) == 0 && result.Text == "" {
			lastErr = fmt.Errorf("no results found for job %s: no chunks or text extracted", jobID)
			continue
		}

		lastErr = nil
		break
	}

	if lastErr != nil {
		return lastErr
	}

	for i, chunk := range result.Chunks {
		if chunk.EmbeddingCompressed != "" {
			embeddings, err := decompressEmbeddings(chunk.EmbeddingCompressed)
			if err != nil {
				return fmt.Errorf("failed to decompress embeddings for chunk %s: %w", chunk.ChunkID, err)
			}
			result.Chunks[i].Embeddings = embeddings
			result.Chunks[i].EmbeddingCompressed = ""
		}
	}

	outputData, err := json.MarshalIndent(result, "", "  ")
	if err != nil {
		return err
	}

	err = os.WriteFile(outputFile, outputData, 0644)
	if err != nil {
		return err
	}

	// Display inference summary if present (now inferences are per-chunk)
	if len(result.Chunks) > 0 {
		inferenceCount := 0
		for _, chunk := range result.Chunks {
			inferenceCount += len(chunk.Inferences)
		}
		if inferenceCount > 0 {
			fmt.Printf("\nInferences generated: %d total across %d chunks\n",
				inferenceCount, len(result.Chunks))
		}
	}

	created, err := getJobCreatedTime(ctx, apiURL, jobID)
	if err == nil {
		completedAt := time.Now()
		if result.CompletedAt != "" {
			if t, e := time.Parse(time.RFC3339, result.CompletedAt); e == nil {
				completedAt = t
			}
		}
		duration := completedAt.Sub(created)
		fmt.Printf("Process completed in: %v\n", duration)
	}

	return nil
}

func getJobCreatedTime(ctx context.Context, apiURL string, jobID string) (time.Time, error) {
	req, err := http.NewRequestWithContext(ctx, "GET", apiURL+"/v1/documents/"+jobID, nil)
	if err != nil {
		return time.Time{}, err
	}

	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return time.Time{}, err
	}
	defer resp.Body.Close()

	var result GetJobResponse
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return time.Time{}, err
	}

	return result.CreatedAt, nil
}

func decompressEmbeddings(encoded string) ([]float32, error) {
	compressed, err := base64.StdEncoding.DecodeString(encoded)
	if err != nil {
		return nil, fmt.Errorf("base64 decode failed: %w", err)
	}

	decompressed, err := gzip.NewReader(bytes.NewReader(compressed))
	if err != nil {
		return nil, fmt.Errorf("gzip reader failed: %w", err)
	}
	defer decompressed.Close()

	rawBytes, err := io.ReadAll(decompressed)
	if err != nil {
		return nil, fmt.Errorf("read decompressed failed: %w", err)
	}

	count := len(rawBytes) / 4
	if count*4 != len(rawBytes) {
		return nil, fmt.Errorf("invalid embedding size: %d bytes", len(rawBytes))
	}

	embeddings := make([]float32, count)
	for i := 0; i < count; i++ {
		bits := binary.LittleEndian.Uint32(rawBytes[i*4 : (i+1)*4])
		embeddings[i] = math.Float32frombits(bits)
	}

	return embeddings, nil
}
