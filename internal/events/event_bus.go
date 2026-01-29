package events

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	"github.com/redis/go-redis/v9"
	"github.com/rs/zerolog"
	"ia-text-orchestrator/pkg/logging"
)

type EventBus struct {
	client *redis.Client
	logger zerolog.Logger
}

func NewEventBus(client *redis.Client) *EventBus {
	return &EventBus{
		client: client,
		logger: logging.GetLogger(),
	}
}

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

func (eb *EventBus) Subscribe(ctx context.Context, channel string) *redis.PubSub {
	pubsub := eb.client.Subscribe(ctx, channel)
	eb.logger.Info().Str("channel", channel).Msg("Subscribed to channel")
	return pubsub
}

func (eb *EventBus) PublishJobCreated(ctx context.Context, jobID string) error {
	return eb.Publish(ctx, "job:events", &JobEvent{
		EventType: EventJobCreated,
		JobID:     jobID,
		Timestamp: time.Now(),
		Progress:  0,
		Status:    "pending",
	})
}

func (eb *EventBus) PublishJobProgress(ctx context.Context, jobID string, progress int, status string) error {
	return eb.Publish(ctx, "job:events", &JobEvent{
		EventType: EventJobProgress,
		JobID:     jobID,
		Timestamp: time.Now(),
		Progress:  progress,
		Status:    status,
	})
}

func (eb *EventBus) PublishJobCompleted(ctx context.Context, jobID string, metadata map[string]interface{}) error {
	return eb.Publish(ctx, "job:events", &JobEvent{
		EventType: EventJobCompleted,
		JobID:     jobID,
		Timestamp: time.Now(),
		Progress:  100,
		Status:    "completed",
		Metadata:  metadata,
	})
}

func (eb *EventBus) PublishJobFailed(ctx context.Context, jobID string, errMsg string) error {
	return eb.Publish(ctx, "job:events", &JobEvent{
		EventType: EventJobFailed,
		JobID:     jobID,
		Timestamp: time.Now(),
		Status:    "failed",
		Error:     errMsg,
	})
}

func (eb *EventBus) PublishJobEvent(ctx context.Context, jobID string, event *JobEvent) error {
	return eb.Publish(ctx, fmt.Sprintf("job:%s:events", jobID), event)
}
