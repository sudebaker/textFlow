package pipeline

import (
	"context"
	"errors"
	"sync"
	"time"

	"ia-text-orchestrator/internal/broker"
	"ia-text-orchestrator/internal/config"
	"ia-text-orchestrator/internal/events"
	"ia-text-orchestrator/internal/models"
	redisclient "ia-text-orchestrator/internal/redis"
)

// Pipeline is a stateless orchestrator that coordinates work across three independent worker queues
// (embeddings, entities, metadata). It uses RabbitMQ to dispatch jobs and Redis to store state.
// All job state is persisted in Redis; Pipeline itself maintains no local state.
type Pipeline struct {
	broker   *broker.RabbitMQBroker
	redis    *redisclient.RedisClient
	eventBus *events.EventBus
	config   *config.Config
}

// NewPipeline creates a new Pipeline orchestrator with the given RabbitMQ broker, Redis client, and configuration.
func NewPipeline(b *broker.RabbitMQBroker, r *redisclient.RedisClient, cfg *config.Config) *Pipeline {
	return &Pipeline{
		broker:   b,
		redis:    r,
		eventBus: events.NewEventBus(r.GetClient()),
		config:   cfg,
	}
}

// PipelineResult aggregates the results from parallel execution of all three worker stages.
// Non-fatal errors from individual workers are accumulated in the Errors slice.
// If any stage fails, its corresponding result field will be zero-valued (nil or empty),
// and the error will be present in Errors.
type PipelineResult struct {
	EmbeddingsResult []float32
	EntitiesResult   []models.Entity
	MetadataResult   map[string]interface{}
	Errors           []error
	Duration         time.Duration
}

// ProcessInParallel fans out work to three independent worker queues (embeddings, entities, metadata)
// and returns immediately without waiting for workers to complete.
//
// The method spawns three goroutines that:
//   - Store the input text in Redis
//   - Dispatch a job message to each respective worker queue
//   - Publish a progress event (0% completion)
//
// Contract:
//   - Returns *PipelineResult with Duration set, regardless of worker outcome
//   - Worker errors are accumulated in result.Errors (non-fatal)
//   - Only returns error if there's a fatal issue (e.g., Redis/broker failure)
//   - Does NOT wait for workers to finish; use WaitForCompletion to poll for results
//
// The caller is responsible for calling WaitForCompletion to poll for job completion
// and retrieve actual embeddings, entities, and metadata from Redis.
func (p *Pipeline) ProcessInParallel(ctx context.Context, jobID string, text string) (*PipelineResult, error) {
	start := time.Now()
	var wg sync.WaitGroup
	var mu sync.Mutex

	var embeddingsResult []float32
	var entitiesResult []models.Entity
	var metadataResult map[string]interface{}
	var errors []error

	wg.Add(3)

	go func() {
		defer wg.Done()
		// processEmbeddings stores the job text and dispatches to the embeddings worker queue.
		// Any error is non-fatal and will be collected but not prevent other stages from running.
		err := p.processEmbeddings(ctx, jobID, text)
		mu.Lock()
		if err != nil {
			errors = append(errors, err)
		} else {
			emb, err := p.redis.GetJobEmbeddings(ctx, jobID)
			if err == nil {
				embeddingsResult = emb
			}
		}
		mu.Unlock()
	}()

	go func() {
		defer wg.Done()
		// processEntities dispatches the job to the entities worker queue.
		// Any error is non-fatal and will be collected but not prevent other stages from running.
		err := p.processEntities(ctx, jobID, text)
		mu.Lock()
		if err != nil {
			errors = append(errors, err)
		} else {
			ents, err := p.redis.GetJobEntities(ctx, jobID)
			if err == nil {
				entitiesResult = ents
			}
		}
		mu.Unlock()
	}()

	go func() {
		defer wg.Done()
		// processMetadata dispatches the job to the metadata worker queue.
		// Any error is non-fatal and will be collected but not prevent other stages from running.
		err := p.processMetadata(ctx, jobID, text)
		mu.Lock()
		if err != nil {
			errors = append(errors, err)
		} else {
			meta, err := p.redis.GetJobMetadata(ctx, jobID)
			if err == nil {
				metadataResult = meta
			}
		}
		mu.Unlock()
	}()

	wg.Wait()

	return &PipelineResult{
		EmbeddingsResult: embeddingsResult,
		EntitiesResult:   entitiesResult,
		MetadataResult:   metadataResult,
		Errors:           errors,
		Duration:         time.Since(start),
	}, nil
}

// processEmbeddings stores the job text in Redis and dispatches a job message to the embeddings worker queue.
// It also publishes a 0% progress event. Returns an error only if the Redis or broker operation fails.
func (p *Pipeline) processEmbeddings(ctx context.Context, jobID, text string) error {
	if err := p.redis.SetJobText(ctx, jobID, text); err != nil {
		return err
	}

	jobMsg := &models.JobMessage{
		JobID: jobID,
	}

	if err := p.broker.Publish(ctx, p.config.EmbeddingsQueue, jobMsg); err != nil {
		return err
	}

	// Publicar evento de progreso
	_ = p.eventBus.PublishJobProgress(ctx, jobID, 0, "embedding")

	return nil
}

// processEntities dispatches a job message to the entities worker queue and publishes a 0% progress event.
// Returns an error only if the broker operation fails.
func (p *Pipeline) processEntities(ctx context.Context, jobID, text string) error {
	jobMsg := &models.JobMessage{
		JobID: jobID,
	}

	if err := p.broker.Publish(ctx, p.config.EntitiesQueue, jobMsg); err != nil {
		return err
	}

	// Publicar evento de progreso
	_ = p.eventBus.PublishJobProgress(ctx, jobID, 0, "entities")

	return nil
}

// processMetadata dispatches a job message to the metadata worker queue and publishes a 0% progress event.
// Returns an error only if the broker operation fails.
func (p *Pipeline) processMetadata(ctx context.Context, jobID, text string) error {
	jobMsg := &models.JobMessage{
		JobID: jobID,
	}

	if err := p.broker.Publish(ctx, p.config.MetadataQueue, jobMsg); err != nil {
		return err
	}

	// Publicar evento de progreso
	_ = p.eventBus.PublishJobProgress(ctx, jobID, 0, "metadata")

	return nil
}

// WaitForCompletion polls Redis until a job reaches a terminal state (completed or failed).
// It respects both the context deadline and the timeout parameter, using whichever is sooner.
//
// Polling behavior:
//   - Polls every 500ms by default
//   - If ctx.Deadline() is set, uses that as the deadline
//   - Otherwise, uses time.Now().Add(timeout) as the deadline
//   - Returns immediately if ctx is cancelled
//
// Return values:
//   - (*models.JobResults, nil) when job reaches StatusCompleted
//   - (nil, error) if job reaches StatusFailed (error message from Redis)
//   - (nil, ctx.Err()) if context is cancelled before completion
//   - (nil, "timeout waiting for job completion") if deadline is exceeded
func (p *Pipeline) WaitForCompletion(ctx context.Context, jobID string, timeout time.Duration) (*models.JobResults, error) {
	deadline, ok := ctx.Deadline()
	if !ok {
		deadline = time.Now().Add(timeout)
	}

	ticker := time.NewTicker(500 * time.Millisecond)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-ticker.C:
			status, err := p.redis.GetJobStatus(ctx, jobID)
			if err != nil {
				continue
			}

			switch status {
			case models.StatusCompleted:
				return p.redis.GetJobResults(ctx, jobID)
			case models.StatusFailed:
				errorMsg, _ := p.redis.GetJobError(ctx, jobID)
				return nil, errors.New(errorMsg)
			}

			if time.Now().After(deadline) {
				return nil, errors.New("timeout waiting for job completion")
			}
		}
	}
}
