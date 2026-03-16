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

type Pipeline struct {
	broker   *broker.RabbitMQBroker
	redis    *redisclient.RedisClient
	eventBus *events.EventBus
	config   *config.Config
}

func NewPipeline(b *broker.RabbitMQBroker, r *redisclient.RedisClient, cfg *config.Config) *Pipeline {
	return &Pipeline{
		broker:   b,
		redis:    r,
		eventBus: events.NewEventBus(r.GetClient()),
		config:   cfg,
	}
}

type PipelineResult struct {
	EmbeddingsResult []float32
	EntitiesResult   []models.Entity
	MetadataResult   map[string]interface{}
	Errors           []error
	Duration         time.Duration
}

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
