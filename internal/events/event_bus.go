package events

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	"github.com/redis/go-redis/v9"
	"github.com/rs/zerolog"
	"textflow/pkg/logging"
)

// EventBus is a Redis pub/sub wrapper for broadcasting job lifecycle events.
// It publishes ephemeral job status updates (created, progress, completed, failed)
// to Redis channels so that external clients can monitor job progress in real-time.
//
// Important: Redis pub/sub is NOT persistent. Subscribers must be listening before
// events are published to receive them. For persistent job state, the orchestrator
// stores job status in Redis hash keys (e.g., "orchestrator:job:{id}:status").
// Subscription is optional for job completion detection; polling Redis storage is
// the authoritative method.
//
// Channel naming:
//   - "job:events" - broadcast channel for all job events
//   - "job:{jobID}:events" - private channel for job-specific events
type EventBus struct {
	client *redis.Client
	logger zerolog.Logger
}

// NewEventBus creates a new EventBus with the given Redis client and a logger.
// The returned EventBus is ready to publish job lifecycle events.
func NewEventBus(client *redis.Client) *EventBus {
	return &EventBus{
		client: client,
		logger: logging.GetLogger(),
	}
}

// Publish marshals the JobEvent to JSON and publishes it to the specified Redis channel.
// This is the internal publish method used by all PublishJob* methods.
//
// Returns an error if JSON marshaling fails or the Redis publish operation fails.
// Note: Successful publish does not guarantee delivery; subscribers must be listening
// when the event is published to receive it (Redis pub/sub semantics).
func (eb *EventBus) Publish(ctx context.Context, channel string, event *JobEvent) error {
	data, err := json.Marshal(event)
	if err != nil {
		return fmt.Errorf("failed to marshal event: %w", err)
	}

	err = eb.client.Publish(ctx, channel, data).Err()
	if err != nil {
		return fmt.Errorf("failed to publish event: %w", err)
	}

	eb.logger.Debug().
		Str("channel", channel).
		Str("event_type", string(event.EventType)).
		Str("job_id", event.JobID).
		Msg("Event published")

	return nil
}

// Subscribe creates a Redis pub/sub subscription to the specified channel.
// The caller is responsible for reading messages from the returned PubSub object
// and closing it when done.
//
// Only events published after the subscription is created will be received
// (Redis pub/sub is not persistent or buffered).
func (eb *EventBus) Subscribe(ctx context.Context, channel string) *redis.PubSub {
	pubsub := eb.client.Subscribe(ctx, channel)
	eb.logger.Info().Str("channel", channel).Msg("Subscribed to channel")
	return pubsub
}

// PublishJobCreated broadcasts a job creation event to both the broadcast "job:events"
// channel and the job-specific "job:{jobID}:events" channel.
// Called by the orchestrator when a new job is created and stored in Redis.
// Signals to subscribers that a job with the given jobID is now pending.
//
// Returns an error if publishing fails.
func (eb *EventBus) PublishJobCreated(ctx context.Context, jobID string) error {
	event := &JobEvent{
		EventType: EventJobCreated,
		JobID:     jobID,
		Timestamp: time.Now(),
		Progress:  0,
		Status:    "pending",
	}
	if err := eb.Publish(ctx, "job:events", event); err != nil {
		return err
	}
	return eb.Publish(ctx, fmt.Sprintf("job:%s:events", jobID), event)
}

// PublishJobProgress broadcasts a job progress event to both the broadcast "job:events"
// channel and the job-specific "job:{jobID}:events" channel.
// Called by workers (embeddings, entities, extraction, metadata, completion) during
// processing to report incremental progress and status updates.
//
// Parameters:
//   - jobID: The job identifier
//   - progress: Numeric progress value (0-100)
//   - status: Human-readable status string (e.g., "processing", "chunking", "embedding")
//
// Returns an error if publishing fails.
func (eb *EventBus) PublishJobProgress(ctx context.Context, jobID string, progress int, status string) error {
	event := &JobEvent{
		EventType: EventJobProgress,
		JobID:     jobID,
		Timestamp: time.Now(),
		Progress:  progress,
		Status:    status,
	}
	if err := eb.Publish(ctx, "job:events", event); err != nil {
		return err
	}
	return eb.Publish(ctx, fmt.Sprintf("job:%s:events", jobID), event)
}

// PublishJobCompleted broadcasts a job completion event to both the broadcast "job:events"
// channel and the job-specific "job:{jobID}:events" channel.
// Called by the orchestrator when all processing is finished and results are stored.
// Signals to subscribers that the job has finished successfully.
//
// Note: This is a notification event only. The authoritative job status is in
// Redis storage (key "orchestrator:job:{jobID}:status"). Clients should not rely
// solely on this event; subscription is optional if polling Redis is preferred.
//
// Parameters:
//   - jobID: The job identifier
//   - metadata: Optional completion metadata (e.g., result summaries, result locations)
//
// Returns an error if publishing fails.
func (eb *EventBus) PublishJobCompleted(ctx context.Context, jobID string, metadata map[string]interface{}) error {
	event := &JobEvent{
		EventType: EventJobCompleted,
		JobID:     jobID,
		Timestamp: time.Now(),
		Progress:  100,
		Status:    "completed",
		Metadata:  metadata,
	}
	if err := eb.Publish(ctx, "job:events", event); err != nil {
		return err
	}
	return eb.Publish(ctx, fmt.Sprintf("job:%s:events", jobID), event)
}

// PublishJobFailed broadcasts a job failure event to both the broadcast "job:events"
// channel and the job-specific "job:{jobID}:events" channel.
// Called by the orchestrator or workers when processing fails (e.g., unstructured API
// error, worker crash, invalid input).
// Signals to subscribers that the job has terminated with an error.
//
// Note: Like PublishJobCompleted, this is a notification event. The authoritative
// job status and error details are stored in Redis. Subscription is optional.
//
// Parameters:
//   - jobID: The job identifier
//   - errMsg: Error message describing the failure reason
//
// Returns an error if publishing fails.
func (eb *EventBus) PublishJobFailed(ctx context.Context, jobID string, errMsg string) error {
	event := &JobEvent{
		EventType: EventJobFailed,
		JobID:     jobID,
		Timestamp: time.Now(),
		Status:    "failed",
		Error:     errMsg,
	}
	if err := eb.Publish(ctx, "job:events", event); err != nil {
		return err
	}
	return eb.Publish(ctx, fmt.Sprintf("job:%s:events", jobID), event)
}

// PublishJobEvent publishes a generic JobEvent to a job-specific channel.
// This publishes to "job:{jobID}:events" instead of the broadcast "job:events" channel,
// allowing clients to subscribe to events for a single job rather than all jobs.
//
// Useful for cases where a caller wants to send a custom event or forward an event
// to the job-specific channel without using the PublishJob* convenience methods.
//
// Parameters:
//   - jobID: The job identifier
//   - event: The JobEvent to publish (must be fully populated by caller)
//
// Returns an error if publishing fails.
func (eb *EventBus) PublishJobEvent(ctx context.Context, jobID string, event *JobEvent) error {
	return eb.Publish(ctx, fmt.Sprintf("job:%s:events", jobID), event)
}

// PublishStageEvent broadcasts a stage-level event (spec 4.5) to both the
// broadcast "job:events" channel and the job-specific "job:{jobID}:events"
// channel. Stage events are stage.queued / stage.started / stage.completed /
// stage.failed and carry the stage name in Metadata["stage"].
//
// Parameters:
//   - jobID: The job identifier
//   - eventType: One of EventStageQueued/Started/Completed/Failed
//   - stage: The pipeline stage name (e.g. "extraction", "embeddings", "image")
//   - metadata: Optional extra metadata merged into the event
//
// Returns an error if publishing fails.
func (eb *EventBus) PublishStageEvent(ctx context.Context, jobID string, eventType EventType, stage string, metadata map[string]interface{}) error {
	merged := map[string]interface{}{"stage": stage}
	for k, v := range metadata {
		merged[k] = v
	}
	event := &JobEvent{
		EventType: eventType,
		JobID:     jobID,
		Timestamp: time.Now(),
		Metadata:  merged,
	}
	if err := eb.Publish(ctx, "job:events", event); err != nil {
		return err
	}
	return eb.Publish(ctx, fmt.Sprintf("job:%s:events", jobID), event)
}
