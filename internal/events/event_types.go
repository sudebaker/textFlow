package events

import "time"

type EventType string

const (
	EventJobCreated   EventType = "job_created"
	EventJobProgress  EventType = "job_progress"
	EventJobCompleted EventType = "job_completed"
	EventJobFailed    EventType = "job_failed"
	// Stage-level events (spec 4.5): emitted as a job moves through pipeline stages.
	EventStageQueued    EventType = "stage.queued"
	EventStageStarted   EventType = "stage.started"
	EventStageCompleted EventType = "stage.completed"
	EventStageFailed    EventType = "stage.failed"
)

type JobEvent struct {
	EventType EventType              `json:"event_type"`
	JobID     string                 `json:"job_id"`
	Timestamp time.Time              `json:"timestamp"`
	Progress  int                    `json:"progress,omitempty"`
	Status    string                 `json:"status,omitempty"`
	Error     string                 `json:"error,omitempty"`
	Metadata  map[string]interface{} `json:"metadata,omitempty"`
}
