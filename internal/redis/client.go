package redis

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	"github.com/redis/go-redis/v9"
	"github.com/rs/zerolog"
	"ia-text-orchestrator/internal/config"
	"ia-text-orchestrator/internal/models"
	"ia-text-orchestrator/pkg/logging"
)

type RedisClient struct {
	client *redis.Client
	logger zerolog.Logger
	jobTTL time.Duration
}

func New(cfg *config.Config) (*RedisClient, error) {
	logger := logging.GetLogger()

	// Parse Redis URL using official parser
	opt, err := redis.ParseURL(cfg.RedisURL)
	if err != nil {
		return nil, fmt.Errorf("invalid Redis URL '%s': %w", cfg.RedisURL, err)
	}

	// Add timeouts for better reliability
	if opt.DialTimeout == 0 {
		opt.DialTimeout = 5 * time.Second
	}
	if opt.ReadTimeout == 0 {
		opt.ReadTimeout = 3 * time.Second
	}
	if opt.WriteTimeout == 0 {
		opt.WriteTimeout = 3 * time.Second
	}
	if opt.PoolTimeout == 0 {
		opt.PoolTimeout = 4 * time.Second
	}

	client := redis.NewClient(opt)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	if err := client.Ping(ctx).Err(); err != nil {
		return nil, fmt.Errorf("failed to connect to Redis: %w", err)
	}

	logger.Info().Str("addr", opt.Addr).Msg("Connected to Redis")

	return &RedisClient{
		client: client,
		logger: logger,
		jobTTL: cfg.JobTTL,
	}, nil
}

func (c *RedisClient) GetClient() *redis.Client {
	return c.client
}

func (c *RedisClient) SetJobStatus(ctx context.Context, jobID string, status models.JobStatus) error {
	key := fmt.Sprintf("job:%s:status", jobID)
	err := c.client.HSet(ctx, key, "status", string(status)).Err()
	if err != nil {
		return fmt.Errorf("failed to set job status: %w", err)
	}
	c.client.Expire(ctx, key, c.jobTTL)
	return nil
}

func (c *RedisClient) GetJobStatus(ctx context.Context, jobID string) (models.JobStatus, error) {
	key := fmt.Sprintf("job:%s:status", jobID)
	status, err := c.client.HGet(ctx, key, "status").Result()
	if err != nil {
		if err == redis.Nil {
			return "", fmt.Errorf("job not found: %s", jobID)
		}
		return "", fmt.Errorf("failed to get job status: %w", err)
	}
	return models.JobStatus(status), nil
}

func (c *RedisClient) SetJobText(ctx context.Context, jobID string, text string) error {
	key := fmt.Sprintf("job:%s:text", jobID)
	err := c.client.Set(ctx, key, text, c.jobTTL).Err()
	if err != nil {
		return fmt.Errorf("failed to set job text: %w", err)
	}
	return nil
}

func (c *RedisClient) GetJobText(ctx context.Context, jobID string) (string, error) {
	key := fmt.Sprintf("job:%s:text", jobID)
	text, err := c.client.Get(ctx, key).Result()
	if err != nil {
		if err == redis.Nil {
			return "", fmt.Errorf("job text not found: %s", jobID)
		}
		return "", fmt.Errorf("failed to get job text: %w", err)
	}
	return text, nil
}

func (c *RedisClient) SetJobResults(ctx context.Context, jobID string, results *models.JobResults) error {
	key := fmt.Sprintf("job:%s:results", jobID)
	data, err := json.Marshal(results)
	if err != nil {
		return fmt.Errorf("failed to marshal job results: %w", err)
	}
	err = c.client.Set(ctx, key, data, c.jobTTL).Err()
	if err != nil {
		return fmt.Errorf("failed to set job results: %w", err)
	}
	return nil
}

func (c *RedisClient) GetJobResults(ctx context.Context, jobID string) (*models.JobResults, error) {
	key := fmt.Sprintf("job:%s:results", jobID)
	data, err := c.client.Get(ctx, key).Bytes()
	if err != nil {
		if err == redis.Nil {
			return nil, fmt.Errorf("job results not found: %s", jobID)
		}
		return nil, fmt.Errorf("failed to get job results: %w", err)
	}
	var results models.JobResults
	if err := json.Unmarshal(data, &results); err != nil {
		return nil, fmt.Errorf("failed to unmarshal job results: %w", err)
	}
	return &results, nil
}

func (c *RedisClient) SetJobEmbeddings(ctx context.Context, jobID string, embeddings []float32) error {
	key := fmt.Sprintf("job:%s:embeddings", jobID)
	data, err := json.Marshal(embeddings)
	if err != nil {
		return fmt.Errorf("failed to marshal embeddings: %w", err)
	}
	err = c.client.Set(ctx, key, data, c.jobTTL).Err()
	if err != nil {
		return fmt.Errorf("failed to set job embeddings: %w", err)
	}
	return nil
}

func (c *RedisClient) GetJobEmbeddings(ctx context.Context, jobID string) ([]float32, error) {
	key := fmt.Sprintf("job:%s:embeddings", jobID)
	data, err := c.client.Get(ctx, key).Bytes()
	if err != nil {
		if err == redis.Nil {
			return nil, fmt.Errorf("job embeddings not found: %s", jobID)
		}
		return nil, fmt.Errorf("failed to get job embeddings: %w", err)
	}
	var embeddings []float32
	if err := json.Unmarshal(data, &embeddings); err != nil {
		return nil, fmt.Errorf("failed to unmarshal embeddings: %w", err)
	}
	return embeddings, nil
}

func (c *RedisClient) SetJobEntities(ctx context.Context, jobID string, entities []models.Entity) error {
	key := fmt.Sprintf("job:%s:entities", jobID)
	data, err := json.Marshal(entities)
	if err != nil {
		return fmt.Errorf("failed to marshal entities: %w", err)
	}
	err = c.client.Set(ctx, key, data, c.jobTTL).Err()
	if err != nil {
		return fmt.Errorf("failed to set job entities: %w", err)
	}
	return nil
}

func (c *RedisClient) GetJobEntities(ctx context.Context, jobID string) ([]models.Entity, error) {
	key := fmt.Sprintf("job:%s:entities", jobID)
	data, err := c.client.Get(ctx, key).Bytes()
	if err != nil {
		if err == redis.Nil {
			return nil, fmt.Errorf("job entities not found: %s", jobID)
		}
		return nil, fmt.Errorf("failed to get job entities: %w", err)
	}
	var entities []models.Entity
	if err := json.Unmarshal(data, &entities); err != nil {
		return nil, fmt.Errorf("failed to unmarshal entities: %w", err)
	}
	return entities, nil
}

func (c *RedisClient) SetJobMetadata(ctx context.Context, jobID string, metadata map[string]interface{}) error {
	key := fmt.Sprintf("job:%s:metadata", jobID)
	data, err := json.Marshal(metadata)
	if err != nil {
		return fmt.Errorf("failed to marshal metadata: %w", err)
	}
	err = c.client.Set(ctx, key, data, c.jobTTL).Err()
	if err != nil {
		return fmt.Errorf("failed to set job metadata: %w", err)
	}
	return nil
}

func (c *RedisClient) GetJobMetadata(ctx context.Context, jobID string) (map[string]interface{}, error) {
	key := fmt.Sprintf("job:%s:metadata", jobID)
	data, err := c.client.Get(ctx, key).Bytes()
	if err != nil {
		if err == redis.Nil {
			return nil, fmt.Errorf("job metadata not found: %s", jobID)
		}
		return nil, fmt.Errorf("failed to get job metadata: %w", err)
	}
	var metadata map[string]interface{}
	if err := json.Unmarshal(data, &metadata); err != nil {
		return nil, fmt.Errorf("failed to unmarshal metadata: %w", err)
	}
	return metadata, nil
}

func (c *RedisClient) UpdateJobStep(ctx context.Context, jobID string, step string, status string) error {
	key := fmt.Sprintf("job:%s:steps", jobID)
	err := c.client.HSet(ctx, key, step, status).Err()
	if err != nil {
		return fmt.Errorf("failed to update job step: %w", err)
	}
	c.client.Expire(ctx, key, c.jobTTL)
	return nil
}

func (c *RedisClient) GetJobSteps(ctx context.Context, jobID string) (map[string]string, error) {
	key := fmt.Sprintf("job:%s:steps", jobID)
	steps, err := c.client.HGetAll(ctx, key).Result()
	if err != nil {
		return nil, fmt.Errorf("failed to get job steps: %w", err)
	}
	return steps, nil
}

func (c *RedisClient) SetJobCreated(ctx context.Context, jobID string) error {
	key := fmt.Sprintf("job:%s:meta", jobID)
	err := c.client.HSet(ctx, key, "created_at", time.Now().Unix()).Err()
	if err != nil {
		return fmt.Errorf("failed to set job created time: %w", err)
	}
	c.client.Expire(ctx, key, c.jobTTL)
	return nil
}

func (c *RedisClient) SetJobCompleted(ctx context.Context, jobID string) error {
	key := fmt.Sprintf("job:%s:meta", jobID)
	err := c.client.HSet(ctx, key, "completed_at", time.Now().Unix()).Err()
	if err != nil {
		return fmt.Errorf("failed to set job completed time: %w", err)
	}
	c.client.Expire(ctx, key, c.jobTTL)
	return nil
}

func (c *RedisClient) SetJobError(ctx context.Context, jobID string, errorMsg string) error {
	key := fmt.Sprintf("job:%s:error", jobID)
	err := c.client.Set(ctx, key, errorMsg, c.jobTTL).Err()
	if err != nil {
		return fmt.Errorf("failed to set job error: %w", err)
	}
	return nil
}

func (c *RedisClient) GetJobError(ctx context.Context, jobID string) (string, error) {
	key := fmt.Sprintf("job:%s:error", jobID)
	errMsg, err := c.client.Get(ctx, key).Result()
	if err != nil {
		if err == redis.Nil {
			return "", nil
		}
		return "", fmt.Errorf("failed to get job error: %w", err)
	}
	return errMsg, nil
}

func (c *RedisClient) DeleteJob(ctx context.Context, jobID string) error {
	keys := []string{
		fmt.Sprintf("job:%s:status", jobID),
		fmt.Sprintf("job:%s:text", jobID),
		fmt.Sprintf("job:%s:results", jobID),
		fmt.Sprintf("job:%s:embeddings", jobID),
		fmt.Sprintf("job:%s:entities", jobID),
		fmt.Sprintf("job:%s:metadata", jobID),
		fmt.Sprintf("job:%s:steps", jobID),
		fmt.Sprintf("job:%s:meta", jobID),
		fmt.Sprintf("job:%s:error", jobID),
	}
	err := c.client.Del(ctx, keys...).Err()
	if err != nil {
		return fmt.Errorf("failed to delete job: %w", err)
	}
	return nil
}

func (c *RedisClient) HealthCheck() error {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	return c.client.Ping(ctx).Err()
}

func (c *RedisClient) Close() error {
	return c.client.Close()
}
