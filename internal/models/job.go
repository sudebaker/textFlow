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
	StatusInferences JobStatus = "inferences"
	StatusCompleted  JobStatus = "completed"
	StatusFailed     JobStatus = "failed"
)

type SourceClassificationResult struct {
	DocumentType      string  `json:"document_type"` // e.g. "notariado", "catastro", "bancario"
	Confidence        float32 `json:"confidence"`
	ClassifierVersion string  `json:"classifier_version"`
}

type MicroInference struct {
	Text       string   `json:"text"`
	Confidence float32  `json:"confidence"`
	Entities   []string `json:"entities,omitempty"`
}

type ChunkInferences struct {
	ChunkID    interface{}      `json:"chunk_id"`
	Inferences []MicroInference `json:"inferences"`
}

type JobResults struct {
	JobID                string                      `json:"job_id"`
	Status               string                      `json:"status"`
	CreatedAt            string                      `json:"created_at"`
	CompletedAt          string                      `json:"completed_at"`
	Text                 string                      `json:"text"`
	Chunks               []Chunk                     `json:"chunks,omitempty"`
	Embeddings           map[string]interface{}      `json:"embeddings,omitempty"`
	Entities             []Entity                    `json:"entities,omitempty"`
	DocumentMetadata     map[string]interface{}      `json:"document_metadata,omitempty"`
	TextMetadata         map[string]interface{}      `json:"text_metadata,omitempty"`
	SourceClassification *SourceClassificationResult `json:"source_classification,omitempty"`
	MicroInferences      []ChunkInferences           `json:"micro_inferences,omitempty"`
}

type Chunk struct {
	ChunkID     string `json:"chunk_id"`
	Text        string `json:"text"`
	StartOffset int    `json:"start_offset"`
	EndOffset   int    `json:"end_offset"`
	TokenCount  int    `json:"token_count,omitempty"`
}

type DocumentMetadata struct {
	MIMEType     string `json:"mime_type"`
	SizeBytes    int64  `json:"size_bytes"`
	Pages        int    `json:"pages,omitempty"`
	Filename     string `json:"filename,omitempty"`
	Author       string `json:"author,omitempty"`
	Title        string `json:"title,omitempty"`
	CreationDate string `json:"creation_date,omitempty"`
	SHA256       string `json:"sha256,omitempty"`
}

type Entity struct {
	Text       string  `json:"text"`
	Label      string  `json:"label"`
	Confidence float32 `json:"confidence"`
	ChunkID    string  `json:"chunk_id,omitempty"`
	Start      int     `json:"start"`
	End        int     `json:"end"`
}

type JobMessage struct {
	JobID          string `json:"job_id"`
	DocumentPath   string `json:"document_path,omitempty"`
	DocumentBase64 string `json:"document_base64,omitempty"`
	DocumentURL    string `json:"document_url,omitempty"`
	Filename       string `json:"filename,omitempty"`
	MIMEType       string `json:"mime_type,omitempty"`
	NotifyWebhook  string `json:"notify_webhook,omitempty"`
}

type UploadRequest struct {
	NotifyWebhook string `json:"notify_webhook,omitempty"`
}

type CreateJobRequest struct {
	DocumentBase64 string   `json:"document_base64" binding:"required_without=DocumentURL"`
	DocumentURL    string   `json:"document_url" binding:"required_without=DocumentBase64"`
	Filename       string   `json:"filename,omitempty"`
	Features       []string `json:"features,omitempty"` // e.g. ["inferences"]
}

type CreateJobResponse struct {
	JobID     string    `json:"job_id"`
	Status    JobStatus `json:"status"`
	StatusURL string    `json:"status_url"`
}

type GetJobResponse struct {
	JobID     string            `json:"job_id"`
	Status    JobStatus         `json:"status"`
	Results   *JobResults       `json:"results,omitempty"`
	Error     string            `json:"error,omitempty"`
	CreatedAt time.Time         `json:"created_at"`
	Steps     map[string]string `json:"steps,omitempty"`
}

type ErrorResponse struct {
	Error  string `json:"error"`
	Detail string `json:"detail,omitempty"`
}
