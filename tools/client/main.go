package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
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
	DocumentBase64 string `json:"document_base64,omitempty"`
	DocumentURL    string `json:"document_url,omitempty"`
	Filename       string `json:"filename,omitempty"`
}

type CreateJobResponse struct {
	JobID     string `json:"job_id"`
	Status    string `json:"status"`
	StatusURL string `json:"status_url"`
}

type GetJobResponse struct {
	JobID     string      `json:"job_id"`
	Status    string      `json:"status"`
	Results   *JobResults `json:"results,omitempty"`
	Error     string      `json:"error,omitempty"`
	CreatedAt time.Time   `json:"created_at"`
}

type JobResults struct {
	JobID            string                 `json:"job_id"`
	Status           string                 `json:"status"`
	CreatedAt        string                 `json:"created_at"`
	CompletedAt      string                 `json:"completed_at"`
	Text             string                 `json:"text"`
	Chunks           []Chunk                `json:"chunks,omitempty"`
	Embeddings       map[string]interface{} `json:"embeddings,omitempty"`
	Entities         []Entity               `json:"entities,omitempty"`
	DocumentMetadata map[string]interface{} `json:"document_metadata,omitempty"`
	TextMetadata     map[string]interface{} `json:"text_metadata,omitempty"`
}

type Chunk struct {
	ChunkID     string `json:"chunk_id"`
	Text        string `json:"text"`
	StartOffset int    `json:"start_offset"`
	EndOffset   int    `json:"end_offset"`
	TokenCount  int    `json:"token_count,omitempty"`
}

type Entity struct {
	Text       string  `json:"text"`
	Label      string  `json:"label"`
	Confidence float32 `json:"confidence"`
	ChunkID    string  `json:"chunk_id,omitempty"`
	Start      int     `json:"start"`
	End        int     `json:"end"`
}

var (
	spinner = []string{"⠋", "⠙", "⠹", "⠸", "⠼", "⠒", "⠂", "⠂", "⠒", "⠲", "⠴", "⠤", "⠄", "⠄", "⠤", "⠴", "⠶", "⠦", "⠰", "⠠", "⠰", "⠦", "⠶", "⠴", "⠤", "⠄", "⠄", "⠤", "⠴", "⠶", "⠦", "⠰"}
)

func main() {
	var (
		inputFile  string
		outputFile string
		apiURL     string
		showHelp   bool
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

	if inputFile == "" || outputFile == "" {
		printUsage()
		os.Exit(1)
	}

	if apiURL == "" {
		apiURL = defaultAPIURL
	}

	ctx, cancel := context.WithTimeout(context.Background(), defaultTimeout)
	defer cancel()

	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, syscall.SIGINT, syscall.SIGTERM)

	go func() {
		<-sigChan
		fmt.Println("\nProcess interrupted by user")
		cancel()
		os.Exit(130)
	}()

	jobID, err := uploadDocument(ctx, apiURL, inputFile)
	if err != nil {
		fmt.Printf("Error uploading document: %v\n", err)
		os.Exit(1)
	}

	status, err := monitorJob(ctx, apiURL, jobID)
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
	os.Exit(0)
}

func printUsage() {
	fmt.Println("Usage: client [options]")
	fmt.Println("")
	fmt.Println("Options:")
	fmt.Println("  -i, --input <file>     Path to document file or URL (required)")
	fmt.Println("  -o, --output <file>    Path to save results JSON (required)")
	fmt.Println("  -u, --url <url>        API base URL (default: http://localhost:8080)")
	fmt.Println("  -h, --help             Show this help message")
	fmt.Println("")
	fmt.Println("Example:")
	fmt.Println("  client -i /path/to/file.pdf -o /path/to/output.json -u http://localhost:8080")
	fmt.Println("  client -i https://example.com/file.pdf -o output.json")
}

func uploadDocument(ctx context.Context, apiURL string, inputFile string) (string, error) {
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
	status := "pending"

	for {
		select {
		case <-ctx.Done():
			return "", ctx.Err()
		case <-ticker.C:
			spinnerIndex = (spinnerIndex + 1) % len(spinner)
			currentStatus, err := getJobStatus(ctx, apiURL, jobID)
			if err != nil {
				fmt.Printf("\r%s Error polling status: %v   ", spinner[spinnerIndex], err)
				continue
			}

			if currentStatus != status {
				fmt.Printf("\r%s Status: %-20s", spinner[spinnerIndex], currentStatus)
				status = currentStatus
			} else {
				fmt.Printf("\r%s Status: %-20s", spinner[spinnerIndex], status)
			}

			if currentStatus == "completed" || currentStatus == "failed" {
				fmt.Println()
				return currentStatus, nil
			}
		}
	}
}

func getJobStatus(ctx context.Context, apiURL string, jobID string) (string, error) {
	req, err := http.NewRequestWithContext(ctx, "GET", apiURL+"/v1/documents/"+jobID, nil)
	if err != nil {
		return "", err
	}

	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusNotFound {
		return "", fmt.Errorf("job not found: %s", jobID)
	}

	var result GetJobResponse
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return "", err
	}

	return result.Status, nil
}

func downloadResults(ctx context.Context, apiURL string, jobID string, outputFile string) error {
	fmt.Println("Downloading results...")

	req, err := http.NewRequestWithContext(ctx, "GET", apiURL+"/v1/documents/"+jobID, nil)
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

	outputData, err := json.MarshalIndent(result.Results, "", "  ")
	if err != nil {
		return err
	}

	err = os.WriteFile(outputFile, outputData, 0644)
	if err != nil {
		return err
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
