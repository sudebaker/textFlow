package models

import (
	"time"
)

type Job struct {
	ID             string      `json:"id"`
	Status         JobStatus   `json:"status"`
	DocumentURL    string      `json:"document_url,omitempty"`
	DocumentBase64 string      `json:"document_base64,omitempty"`
	Results        *JobResults `json:"results,omitempty"`
	Error          string      `json:"error,omitempty"`
	CreatedAt      time.Time   `json:"created_at"`
	CompletedAt    *time.Time  `json:"completed_at,omitempty"`
	Retries        int         `json:"retries,omitempty"`
}

type JobStatus string

const (
	StatusPending    JobStatus = "pending"
	StatusExtracting JobStatus = "extracting"
	StatusProcessing JobStatus = "processing"
	StatusEmbedding  JobStatus = "embedding"
	StatusEntities   JobStatus = "entities"
	StatusCompleted  JobStatus = "completed"
	StatusFailed     JobStatus = "failed"
)

type JobResults struct {
	Text       string                 `json:"text"`
	Embeddings []float32              `json:"embeddings,omitempty"`
	Entities   []Entity               `json:"entities,omitempty"`
	Metadata   map[string]interface{} `json:"metadata,omitempty"`
}

type DocumentMetadata struct {
	MIMEType  string `json:"mime_type"`
	SizeBytes int64  `json:"size_bytes"`
	Pages     int    `json:"pages,omitempty"`
	Filename  string `json:"filename,omitempty"`
}

type Entity struct {
	Text       string  `json:"text"`
	Label      string  `json:"label"`
	Confidence float32 `json:"confidence"`
	Start      int     `json:"start"`
	End        int     `json:"end"`
}

type JobMessage struct {
	JobID          string `json:"job_id"`
	DocumentBase64 string `json:"document_base64,omitempty"`
	DocumentURL    string `json:"document_url,omitempty"`
	MIMEType       string `json:"mime_type,omitempty"`
}

type CreateJobRequest struct {
	DocumentBase64 string `json:"document_base64" binding:"required_without=DocumentURL"`
	DocumentURL    string `json:"document_url" binding:"required_without=DocumentBase64"`
	Filename       string `json:"filename,omitempty"`
}

type CreateJobResponse struct {
	JobID     string    `json:"job_id"`
	Status    JobStatus `json:"status"`
	StatusURL string    `json:"status_url"`
}

type GetJobResponse struct {
	JobID     string      `json:"job_id"`
	Status    JobStatus   `json:"status"`
	Results   *JobResults `json:"results,omitempty"`
	Error     string      `json:"error,omitempty"`
	CreatedAt time.Time   `json:"created_at"`
}

type ErrorResponse struct {
	Error  string `json:"error"`
	Detail string `json:"detail,omitempty"`
}
