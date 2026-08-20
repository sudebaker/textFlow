package models

import (
	"time"
)

// Job represents a single document processing request and its lifecycle through the pipeline.
// It tracks the document source (URL or base64), processing status, and final results.
// The job flows through states: Pending → Extracting → Processing → Embedding → Entities → Inferences → Completed (or Failed).
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

// JobStatus represents the current state of a job in the processing pipeline.
// It is a string enum that tracks progression from initial submission through completion or failure.
type JobStatus string

const (
	// StatusPending indicates the job has been created but not yet started extraction.
	StatusPending JobStatus = "pending"
	// StatusExtracting indicates the job is currently extracting text from the document.
	StatusExtracting JobStatus = "extracting"
	// StatusProcessing indicates the job is being processed by the pipeline.
	StatusProcessing JobStatus = "processing"
	// StatusEmbedding indicates the job is computing embeddings for extracted chunks.
	StatusEmbedding JobStatus = "embedding"
	// StatusEntities indicates the job is performing named entity recognition.
	StatusEntities JobStatus = "entities"
	// StatusInferences indicates the job is generating micro-inferences from extracted facts.
	StatusInferences JobStatus = "inferences"
	// StatusTranscribing indicates the job is being transcribed by the audio-worker.
	StatusTranscribing JobStatus = "transcribing"
	// StatusAnalyzingImage indicates the job is being analyzed by the image-worker.
	StatusAnalyzingImage JobStatus = "analyzing_image"
	// StatusCompleted indicates the job has finished successfully and results are ready.
	StatusCompleted JobStatus = "completed"
	// StatusFailed indicates the job encountered an error during processing.
	StatusFailed JobStatus = "failed"
)

// SourceClassificationResult represents the output of document source classification.
// It identifies the document type (e.g., notarized deed, property registry, banking document)
// and provides a confidence score and classifier version for traceability.
type SourceClassificationResult struct {
	DocumentType      string  `json:"document_type"` // e.g. "notariado", "catastro", "bancario"
	Confidence        float32 `json:"confidence"`
	ClassifierVersion string  `json:"classifier_version"`
}

// MicroInference represents a single fact or assertion extracted by the LLM from a chunk.
// It includes the extracted text, confidence score, and entity references.
type MicroInference struct {
	Text       string   `json:"text"`
	Confidence float32  `json:"confidence"`
	EntityRefs []string `json:"entity_refs,omitempty"`
	EntityID   string   `json:"entity_id,omitempty"`
}

// ChunkInferences represents a bundle of inferences extracted from a single text chunk.
// It associates a chunk with all micro-inferences (facts) that were generated from its content.
type ChunkInferences struct {
	ChunkID    interface{}      `json:"chunk_id"`
	Inferences []MicroInference `json:"inferences"`
}

type EntityMinimal struct {
	Label       string  `json:"label"`
	Text        string  `json:"text"`
	Confidence  float32 `json:"confidence"`
	StartOffset int     `json:"start_offset"`
	EndOffset   int     `json:"end_offset"`
	ChunkID     string  `json:"chunk_id,omitempty"`
}

// InferenceItem represents a single micro-inference embedded inside a chunk in the final results.
type InferenceItem struct {
	Text       string    `json:"text"`
	Confidence float32   `json:"confidence"`
	EntityRefs []string  `json:"entity_refs,omitempty"`
	EntityID   string    `json:"entity_id,omitempty"`
	EntityIDs  []string  `json:"entity_id_refs,omitempty"`
	Embedding  []float32 `json:"embedding,omitempty"`
}

// JobResults represents the final aggregated results of a completed job.
// It contains extracted text, chunks with per-chunk embeddings/entities/inferences,
// a deduplicated entity map, document metadata, and source classification.
// All fields except JobID, Status, CreatedAt, and CompletedAt are optional,
// depending on which processing stages were executed.
type JobResults struct {
	JobID                string                      `json:"job_id"`
	Status               string                      `json:"status"`
	CreatedAt            string                      `json:"created_at"`
	CompletedAt          string                      `json:"completed_at"`
	Text                 string                      `json:"text"`
	SchemaVersion        string                      `json:"schema_version,omitempty"`
	Chunks               []Chunk                     `json:"chunks,omitempty"`
	Entities             map[string]EntityMinimal    `json:"entities,omitempty"`
	DocumentMetadata     map[string]interface{}      `json:"document_metadata,omitempty"`
	TextMetadata         map[string]interface{}      `json:"text_metadata,omitempty"`
	SourceClassification *SourceClassificationResult `json:"source_classification,omitempty"`
}

// Chunk represents a segment of extracted text with token metadata, per-chunk embeddings,
// entity references, and inferences. Chunks are created by the text extraction phase and serve
// as units for embedding and entity recognition.
// StartOffset and EndOffset refer to character positions in the original extracted text.
type Chunk struct {
	ChunkID     string          `json:"chunk_id"`
	Text        string          `json:"text"`
	StartOffset int             `json:"start_offset"`
	EndOffset   int             `json:"end_offset"`
	TokenCount  int             `json:"token_count,omitempty"`
	Embeddings  []float32       `json:"embeddings,omitempty"`
	EntityIDs   []string        `json:"entity_ids,omitempty"`
	Inferences  []InferenceItem `json:"inferences,omitempty"`
}

// DocumentMetadata represents PDF and document-level metadata extracted during processing.
// It includes MIME type, file size, page count, document properties, and cryptographic hash for integrity verification.
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

// Entity represents a named entity as stored by the entities-worker in the intermediate Redis key.
// It includes character offsets (Start/End) for the entity's position in the chunk text.
// For the final deduplicated entity map in JobResults, use EntityMinimal instead.
type Entity struct {
	Text       string  `json:"text"`
	Label      string  `json:"label"`
	Confidence float32 `json:"confidence"`
	ChunkID    string  `json:"chunk_id,omitempty"`
	Start      int     `json:"start"`
	End        int     `json:"end"`
}

// JobMessage represents the RabbitMQ envelope for job execution.
// It carries minimal job metadata and routing information to workers,
// with the document provided either as base64-encoded content or as a path/URL reference.
// The NotifyWebhook field is optional for asynchronous completion notifications.
type JobMessage struct {
	JobID           string      `json:"job_id"`
	DocumentPath    string      `json:"document_path,omitempty"`
	DocumentBase64  string      `json:"document_base64,omitempty"`
	DocumentURL     string      `json:"document_url,omitempty"`
	Filename        string      `json:"filename,omitempty"`
	MIMEType        string      `json:"mime_type,omitempty"`
	ContentType     ContentType `json:"content_type,omitempty"`
	Diarize         bool        `json:"diarize,omitempty"`
	Features        []string    `json:"features,omitempty"`
	PipelineVersion string      `json:"pipeline_version,omitempty"`
}

// ContentType identifies the type of uploaded content.
type ContentType string

const (
	ContentTypeDocument ContentType = "document"
	ContentTypeAudio    ContentType = "audio"
	ContentTypeImage    ContentType = "image"
)

// AudioSegment represents a single timed segment with optional speaker label.
type AudioSegment struct {
	Start   float64 `json:"start"`
	End     float64 `json:"end"`
	Text    string  `json:"text"`
	Speaker string  `json:"speaker,omitempty"`
}

// AudioMetadata holds transcription-specific metadata stored in Redis.
type AudioMetadata struct {
	Language        string  `json:"language,omitempty"`
	DurationSeconds float64 `json:"duration_seconds,omitempty"`
	HasDiarization  bool    `json:"has_diarization"`
	SegmentCount    int     `json:"segment_count,omitempty"`
}

// ImageMetadata holds image analysis metadata stored in Redis.
type ImageMetadata struct {
	Language    string  `json:"language,omitempty"`
	Description string  `json:"description,omitempty"`
	Confidence  float64 `json:"confidence,omitempty"`
}

// UploadRequest represents the HTTP request body for document upload endpoints.
// It specifies optional webhook notification for asynchronous processing callbacks.
type UploadRequest struct {
	NotifyWebhook string `json:"notify_webhook,omitempty"`
}

// CreateJobRequest represents the HTTP request body for the create job endpoint.
// Either DocumentBase64 or DocumentURL must be provided (enforced by binding tags).
// Filename is optional for user-provided context, and Features allows selective activation of pipeline stages.
// WebhookURL and WebhookSecret allow per-request webhook configuration with HMAC signature support.
type CreateJobRequest struct {
	DocumentBase64 string   `json:"document_base64" binding:"required_without=DocumentURL"`
	DocumentURL    string   `json:"document_url" binding:"required_without=DocumentBase64"`
	Filename       string   `json:"filename,omitempty"`
	Features       []string `json:"features,omitempty"` // e.g. ["inferences"]
	WebhookURL     string   `json:"webhook_url,omitempty"`
	WebhookSecret  string   `json:"webhook_secret,omitempty"`
}

// CreateJobResponse represents the HTTP response body for the create job endpoint.
// It returns the newly created job ID, initial status (pending), and a URL for polling job status.
type CreateJobResponse struct {
	JobID     string    `json:"job_id"`
	Status    JobStatus `json:"status"`
	StatusURL string    `json:"status_url"`
}

// GetJobResponse represents the HTTP response body for the get job status endpoint.
// It includes the job ID, current status, and per-stage step progress.
// The CurrentStep field indicates the active or last-completed pipeline stage.
type GetJobResponse struct {
	JobID       string            `json:"job_id"`
	Status      JobStatus         `json:"status"`
	Error       string            `json:"error,omitempty"`
	CreatedAt   time.Time         `json:"created_at"`
	Steps       map[string]string `json:"steps,omitempty"`
	CurrentStep string            `json:"current_step,omitempty"`
}

// ErrorResponse represents a standardized error envelope for HTTP error responses.
// Error is the machine-readable error code or type, and Detail provides human-readable information.
type ErrorResponse struct {
	Error  string `json:"error"`
	Detail string `json:"detail,omitempty"`
}
