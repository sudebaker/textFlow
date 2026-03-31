package handlers

import "time"

type BatchDocument struct {
	Text     string                 `json:"text"`
	Filename string                 `json:"filename,omitempty"`
	Metadata map[string]interface{} `json:"metadata,omitempty"`
}

type BatchRequest struct {
	Documents      []BatchDocument `json:"documents" binding:"required,min=1"`
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
	BatchID     string        `json:"batch_id"`
	Status      string        `json:"status"`
	Total       int           `json:"total"`
	Completed   int           `json:"completed"`
	Failed      int           `json:"failed"`
	Pending     int           `json:"pending"`
	Jobs        []BatchJobRef `json:"jobs"`
	CreatedAt   time.Time     `json:"created_at"`
	CompletedAt *time.Time    `json:"completed_at,omitempty"`
}
