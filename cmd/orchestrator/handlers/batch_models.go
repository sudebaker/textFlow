package handlers

import "time"

type BatchDocument struct {
	Text     string                 `json:"text"`
	Filename string                 `json:"filename,omitempty"`
	Profile  string                 `json:"profile,omitempty"`
	Metadata map[string]interface{} `json:"metadata,omitempty"`
}

type BatchRequest struct {
	Documents      []BatchDocument `json:"documents" binding:"required,min=1"`
	Profile        string          `json:"profile,omitempty"`
	MaxConcurrency int             `json:"max_concurrency,omitempty"`
	WebhookURL     string          `json:"webhook_url,omitempty"`
	WebhookSecret  string          `json:"webhook_secret,omitempty"`
	Features       []string        `json:"features,omitempty"`
}

type BatchJobRef struct {
	ID       string `json:"id"`
	Filename string `json:"filename,omitempty"`
	Status   string `json:"status"`
}

// RejectedDocument reports a document excluded at admission (invalid profile).
type RejectedDocument struct {
	Index    int    `json:"index"`
	Filename string `json:"filename,omitempty"`
	Reason   string `json:"reason"`
}

type BatchResponse struct {
	BatchID   string             `json:"batch_id"`
	Total     int                `json:"total"`
	Jobs      []BatchJobRef      `json:"jobs"`
	Rejected  []RejectedDocument `json:"rejected,omitempty"`
	StatusURL string             `json:"status_url"`
	CreatedAt time.Time          `json:"created_at"`
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
