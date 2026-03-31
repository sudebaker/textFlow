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
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"
)

const (
	pollingInterval = 3 * time.Second
	defaultTimeout  = 10 * time.Minute
	defaultAPIURL   = "http://localhost:8080"
)

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
	Text       string   `json:"text"`
	Confidence float32  `json:"confidence"`
	EntityRefs []string `json:"entity_refs,omitempty"`
	EntityID   string   `json:"entity_id,omitempty"`
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
		err := runBatchMode(ctx, apiURL, batchFile, outputFile, webhookURL, webhookSecret)
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
		jobID, err := uploadDocument(ctx, apiURL, inputFile, inferencesEnabled, webhookURL, webhookSecret)
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
	fmt.Println("  -i, --input <file>          Path to document file or URL (required for single mode)")
	fmt.Println("  -o, --output <file>         Path to save results JSON (required)")
	fmt.Println("  -u, --url <url>             API base URL (default: http://localhost:8080)")
	fmt.Println("  -f, --inferences            Enable inference generation (requires vLLM)")
	fmt.Println("  -w, --webhook <url>         Webhook URL for job completion notification")
	fmt.Println("  --webhook-secret <secret>   Secret for webhook signature verification")
	fmt.Println("  --sse                      Use SSE streaming instead of polling")
	fmt.Println("  --timeout <duration>       Timeout for entire operation (default: 10m)")
	fmt.Println("  -b, --batch [file]         Batch processing mode (reads JSON file with documents)")
	fmt.Println("  --job-id <id>              Resume or download results for an existing job ID")
	fmt.Println("  -h, --help                 Show this help message")
	fmt.Println("")
	fmt.Println("Single Job Mode:")
	fmt.Println("  client -i /path/to/file.pdf -o /path/to/output.json")
	fmt.Println("  client -i https://example.com/file.pdf -o output.json -w https://myapp.com/webhook")
	fmt.Println("  client -i /path/to/file.pdf -o output.json --sse")
	fmt.Println("  client -i /path/to/large.pdf -o output.json --timeout 1h")
	fmt.Println("")
	fmt.Println("Resume Mode:")
	fmt.Println("  client --job-id <id> -o output.json")
	fmt.Println("")
	fmt.Println("Batch Mode:")
	fmt.Println("  client -b documents.json -o results.json")
	fmt.Println("  client -b documents.json -o results.json -w https://myapp.com/webhook")
}

func uploadDocument(ctx context.Context, apiURL string, inputFile string, inferencesEnabled bool, webhookURL, webhookSecret string) (string, error) {
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
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusAccepted {
		body, _ := io.ReadAll(resp.Body)
		return "", fmt.Errorf("API returned status %d: %s", resp.StatusCode, string(body))
	}

	var result CreateJobResponse
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return "", fmt.Errorf("failed to parse response: %w", err)
	}

	fmt.Printf("Job created: %s\n", result.JobID)

	return result.JobID, nil
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

func monitorJob(ctx context.Context, apiURL string, jobID string) (string, error) {
	fmt.Println("Monitoring job progress...")
	fmt.Printf("Status: pending")

	ticker := time.NewTicker(pollingInterval)
	defer ticker.Stop()

	spinnerIndex := 0

	for {
		select {
		case <-ctx.Done():
			return "", ctx.Err()
		case <-ticker.C:
			spinnerIndex = (spinnerIndex + 1) % len(spinner)
			currentStatus, currentStep, err := getJobStatus(ctx, apiURL, jobID)
			if err != nil {
				fmt.Printf("\r%s Error polling status: %v   ", spinner[spinnerIndex], err)
				continue
			}

			if currentStatus == "processing" && currentStep != "" {
				fmt.Printf("\r%s %-40s", spinner[spinnerIndex], fmt.Sprintf("%s | step: %s", currentStatus, currentStep))
			} else {
				fmt.Printf("\r%s %-40s", spinner[spinnerIndex], currentStatus)
			}

			if currentStatus == "completed" || currentStatus == "failed" {
				fmt.Println()
				return currentStatus, nil
			}
		}
	}
}

func runBatchMode(ctx context.Context, apiURL, batchFile, outputFile, webhookURL, webhookSecret string) error {
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

	fmt.Printf("Creating batch with %d documents...\n", len(batchReq.Documents))

	jsonData, err := json.Marshal(batchReq)
	if err != nil {
		return fmt.Errorf("failed to marshal batch request: %w", err)
	}

	req, err := http.NewRequestWithContext(ctx, "POST", apiURL+"/v1/documents/batch", bytes.NewBuffer(jsonData))
	if err != nil {
		return fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")

	client := &http.Client{Timeout: 60 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("failed to connect to API: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusAccepted {
		body, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("API returned status %d: %s", resp.StatusCode, string(body))
	}

	var result BatchResponse
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return fmt.Errorf("failed to parse response: %w", err)
	}

	fmt.Printf("Batch created: %s\n", result.BatchID)
	fmt.Printf("Total jobs: %d\n", result.Total)

	status, err := monitorBatch(ctx, apiURL, result.BatchID)
	if err != nil {
		return fmt.Errorf("error monitoring batch: %w", err)
	}

	finalStatus, err := getBatchStatus(ctx, apiURL, result.BatchID)
	if err != nil {
		return fmt.Errorf("failed to get batch status: %w", err)
	}

	outputData, err := json.MarshalIndent(finalStatus, "", "  ")
	if err != nil {
		return fmt.Errorf("failed to marshal batch results: %w", err)
	}

	err = os.WriteFile(outputFile, outputData, 0644)
	if err != nil {
		return fmt.Errorf("failed to write output file: %w", err)
	}

	fmt.Printf("\nBatch %s\n", status)
	fmt.Printf("Results saved to: %s\n", outputFile)
	fmt.Printf("Summary: %d completed, %d failed, %d total\n",
		finalStatus.Completed, finalStatus.Failed, finalStatus.Total)

	return nil
}

func monitorBatch(ctx context.Context, apiURL, batchID string) (string, error) {
	fmt.Println("Monitoring batch progress...")
	fmt.Printf("Status: pending")

	ticker := time.NewTicker(pollingInterval)
	defer ticker.Stop()

	spinnerIndex := 0
	lastStatus := ""

	for {
		select {
		case <-ctx.Done():
			return "", ctx.Err()
		case <-ticker.C:
			spinnerIndex = (spinnerIndex + 1) % len(spinner)
			status, err := getBatchStatus(ctx, apiURL, batchID)
			if err != nil {
				fmt.Printf("\r%s Error polling batch status: %v   ", spinner[spinnerIndex], err)
				continue
			}

			displayStatus := fmt.Sprintf("%s (%d/%d done, %d failed)",
				status.Status, status.Completed, status.Total, status.Failed)

			if status.Status != lastStatus {
				fmt.Printf("\r%s Status: %-40s", spinner[spinnerIndex], displayStatus)
				lastStatus = status.Status
			} else {
				fmt.Printf("\r%s Status: %-40s", spinner[spinnerIndex], displayStatus)
			}

			if status.Status == "completed" || status.Status == "failed" || status.Status == "partial" {
				fmt.Println()
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
	lastStatus := "pending"
	spinnerIndex := 0

	fmt.Printf("Status: %s", lastStatus)

	for {
		select {
		case <-ctx.Done():
			return "", ctx.Err()
		default:
			line, err := reader.ReadString('\n')
			if err != nil {
				if err == io.EOF {
					return lastStatus, nil
				}
				return lastStatus, fmt.Errorf("error reading SSE stream: %w", err)
			}

			line = strings.TrimSpace(line)
			if line == "" {
				continue
			}

			if strings.HasPrefix(line, ":") {
				continue
			}

			if strings.HasPrefix(line, "data: ") {
				data := strings.TrimPrefix(line, "data: ")

				var event JobEvent
				if err := json.Unmarshal([]byte(data), &event); err != nil {
					continue
				}

				spinnerIndex = (spinnerIndex + 1) % len(spinner)
				lastStatus = event.Status

				fmt.Printf("\r%s Status: %-20s", spinner[spinnerIndex], lastStatus)

				if event.Status == "completed" || event.Status == "failed" {
					fmt.Println()
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

func downloadResults(ctx context.Context, apiURL string, jobID string, outputFile string) error {
	fmt.Println("Downloading results...")

	req, err := http.NewRequestWithContext(ctx, "GET", apiURL+"/v1/documents/"+jobID+"/download", nil)
	if err != nil {
		return err
	}

	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("failed to get results: status %d", resp.StatusCode)
	}

	var result GetJobResponse
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return err
	}

	if result.Results == nil {
		return fmt.Errorf("no results found for job %s", jobID)
	}

	for i, chunk := range result.Results.Chunks {
		if chunk.EmbeddingCompressed != "" {
			embeddings, err := decompressEmbeddings(chunk.EmbeddingCompressed)
			if err != nil {
				return fmt.Errorf("failed to decompress embeddings for chunk %s: %w", chunk.ChunkID, err)
			}
			result.Results.Chunks[i].Embeddings = embeddings
			result.Results.Chunks[i].EmbeddingCompressed = ""
		}
	}

	outputData, err := json.MarshalIndent(result.Results, "", "  ")
	if err != nil {
		return err
	}

	err = os.WriteFile(outputFile, outputData, 0644)
	if err != nil {
		return err
	}

	// Display inference summary if present (now inferences are per-chunk)
	if result.Results != nil {
		inferenceCount := 0
		for _, chunk := range result.Results.Chunks {
			inferenceCount += len(chunk.Inferences)
		}
		if inferenceCount > 0 {
			fmt.Printf("\nInferences generated: %d total across %d chunks\n",
				inferenceCount, len(result.Results.Chunks))
		}
	}

	created, err := getJobCreatedTime(ctx, apiURL, jobID)
	if err == nil {
		completedAt := time.Now()
		if result.Results.CompletedAt != "" {
			if t, e := time.Parse(time.RFC3339, result.Results.CompletedAt); e == nil {
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

func downloadCompressedResults(ctx context.Context, apiURL string, jobID string, outputFile string) error {
	fmt.Println("Downloading compressed results...")

	req, err := http.NewRequestWithContext(ctx, "GET", apiURL+"/v1/documents/"+jobID+"/download", nil)
	if err != nil {
		return err
	}

	req.Header.Set("Accept-Encoding", "gzip")

	client := &http.Client{Timeout: 30 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("failed to get results: status %d", resp.StatusCode)
	}

	var reader io.ReadCloser
	if resp.Header.Get("Content-Encoding") == "gzip" {
		reader, err = gzip.NewReader(resp.Body)
		if err != nil {
			return fmt.Errorf("failed to create gzip reader: %w", err)
		}
		defer reader.Close()
	} else {
		reader = resp.Body
	}

	data, err := io.ReadAll(reader)
	if err != nil {
		return fmt.Errorf("failed to read response: %w", err)
	}

	err = os.WriteFile(outputFile, data, 0644)
	if err != nil {
		return err
	}

	fmt.Printf("Results saved to: %s\n", outputFile)
	return nil
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
