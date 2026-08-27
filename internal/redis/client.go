package redis

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"
	"github.com/rs/zerolog"
	"github.com/vmihailenco/msgpack/v5"
	"textflow/internal/config"
	"textflow/internal/models"
	"textflow/pkg/logging"
)

// activeJobsSuffix is the Redis Sorted Set key suffix used to track active (non-terminal) jobs.
// Score = Unix timestamp of job creation; used by ExpireStuckJobs for O(log N) range queries
// instead of full SCAN, preventing latency spikes with large job counts.
const activeJobsSuffix = "active_jobs"

// RedisClient manages all Redis operations for job state persistence across the pipeline.
// It is the single source of truth for job status, extracted text, embeddings, entities,
// metadata, and processing steps. All data is stored with automatic TTL expiration.
// Thread-safe for concurrent access from orchestrator, workers, and completion service.
type RedisClient struct {
	client    *redis.Client
	logger    zerolog.Logger
	jobTTL    time.Duration
	namespace string
}

// New creates and initializes a new RedisClient from configuration.
// It establishes a connection to Redis, validates connectivity with a ping,
// and sets default timeouts for reliability:
// - DialTimeout: 5 seconds (connection establishment)
// - ReadTimeout: 3 seconds (read operations)
// - WriteTimeout: 3 seconds (write operations)
// - PoolTimeout: 4 seconds (connection pool wait)
// All job data is automatically expired after cfg.JobTTL (typically 24 hours).
// Namespace is read from REDIS_NAMESPACE env or defaults to "orchestrator".
// Returns error if Redis URL is invalid or connection fails.
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

	// Explicit pool configuration to prevent connection starvation under high load.
	// Default PoolSize = runtime.NumCPU() is insufficient when 100+ goroutines
	// may need concurrent Redis access during parallel job processing.
	if opt.PoolSize == 0 {
		opt.PoolSize = 100
	}
	if opt.MinIdleConns == 0 {
		opt.MinIdleConns = 10
	}

	client := redis.NewClient(opt)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	if err := client.Ping(ctx).Err(); err != nil {
		return nil, fmt.Errorf("failed to connect to Redis: %w", err)
	}

	logger.Info().Str("addr", opt.Addr).Msg("Connected to Redis")

	// Get namespace from environment or use default
	namespace := os.Getenv("REDIS_NAMESPACE")
	if namespace == "" {
		namespace = "orchestrator"
	}

	return &RedisClient{
		client:    client,
		logger:    logger,
		jobTTL:    cfg.JobTTL,
		namespace: namespace,
	}, nil
}

// GetClient returns the underlying go-redis Client for direct access when needed.
func (c *RedisClient) GetClient() *redis.Client {
	return c.client
}

// key constructs a namespaced Redis key from parts
// Example: key("job", jobID, "status") -> "orchestrator:job:123:status"
func (c *RedisClient) key(parts ...string) string {
	allParts := append([]string{c.namespace}, parts...)
	return strings.Join(allParts, ":")
}

var defaultNamespace = "orchestrator"

func Key(parts ...string) string {
	namespace := os.Getenv("REDIS_NAMESPACE")
	if namespace == "" {
		namespace = defaultNamespace
	}
	allParts := append([]string{namespace}, parts...)
	return strings.Join(allParts, ":")
}

func GetClient() *redis.Client {
	return redisClient.client
}

var redisClient *RedisClient

func SetClient(c *RedisClient) {
	redisClient = c
}

// SetJobStatus stores the job status in Redis with automatic TTL expiration.
// Redis key: {namespace}:job:{jobID}:status (hash with field "status")
// TTL: jobTTL (typically 24 hours), refreshed on each write.
// When transitioning to terminal states (completed/failed), the job is removed
// from the active_jobs sorted set to keep stuck-job detection accurate.
// Returns error if Redis operation fails.
func (c *RedisClient) SetJobStatus(ctx context.Context, jobID string, status models.JobStatus) error {
	key := c.key("job", jobID, "status")
	err := c.client.HSet(ctx, key, "status", string(status)).Err()
	if err != nil {
		return fmt.Errorf("failed to set job status: %w", err)
	}
	if err := c.client.Expire(ctx, key, c.jobTTL).Err(); err != nil {
		c.logger.Error().Err(err).Str("key", key).Msg("Failed to set TTL on job status key")
		return fmt.Errorf("failed to set TTL for key %s: %w", key, err)
	}

	// Remove from active_jobs ZSET when job reaches a terminal state
	if status == models.StatusCompleted || status == models.StatusFailed || status == models.StatusCancelled {
		activeKey := c.key(activeJobsSuffix)
		if err := c.client.ZRem(ctx, activeKey, jobID).Err(); err != nil {
			// Non-fatal; job will be cleaned up by TTL or next ExpireStuckJobs run
			c.logger.Warn().Err(err).Str("job_id", jobID).Msg("Failed to remove job from active_jobs ZSET")
		}
	}

	return nil
}

// GetJobStatus retrieves the current job status from Redis.
// Redis key: {namespace}:job:{jobID}:status
// Returns models.JobStatus on success.
// Returns error with "job not found" message if job does not exist (redis.Nil).
// Returns error if Redis operation fails.
func (c *RedisClient) GetJobStatus(ctx context.Context, jobID string) (models.JobStatus, error) {
	key := c.key("job", jobID, "status")
	status, err := c.client.HGet(ctx, key, "status").Result()
	if err != nil {
		if err == redis.Nil {
			return "", fmt.Errorf("job not found: %s", jobID)
		}
		return "", fmt.Errorf("failed to get job status: %w", err)
	}
	return models.JobStatus(status), nil
}

// SetJobText stores the job text value.
// Redis key: {namespace}:job:{jobID}:text
// TTL: jobTTL (typically 24 hours), set on initial write.
// Writes the value exactly as received: either a raw text payload (legacy) or an
// artifact store reference (sha256:<hex>). The extraction worker writes a ref whose
// payload lives on the FS artifact store; Python readers resolve it via
// artifact_store.resolve_text(). This method has no production callers (dead code
// since the D3 artifact store migration) and is retained for compatibility and tests.
// Returns error if the Redis operation fails.
func (c *RedisClient) SetJobText(ctx context.Context, jobID string, text string) error {
	key := c.key("job", jobID, "text")
	err := c.client.Set(ctx, key, text, c.jobTTL).Err()
	if err != nil {
		return fmt.Errorf("failed to set job text: %w", err)
	}
	return nil
}

// GetJobText retrieves the extracted text for a job.
// Redis key: {namespace}:job:{jobID}:text
// If the stored value is an artifact reference (sha256:<hex>), the bytes are
// resolved from the artifact store filesystem and decoded as text. Otherwise
// the raw value is returned unchanged (legacy payload compatibility).
// Returns error with "job text not found" message if key does not exist (redis.Nil).
// Returns error if Redis operation, artifact resolution, or filesystem read fails.
func (c *RedisClient) GetJobText(ctx context.Context, jobID string) (string, error) {
	key := c.key("job", jobID, "text")
	text, err := c.client.Get(ctx, key).Result()
	if err != nil {
		if err == redis.Nil {
			return "", fmt.Errorf("job text not found: %s", jobID)
		}
		return "", fmt.Errorf("failed to get job text: %w", err)
	}

	resolved, wasRef, err := resolveArtifactBytes(ctx, text)
	if err != nil {
		return "", err
	}
	if wasRef {
		return string(resolved), nil
	}
	return text, nil
}

// SetJobResults would store the complete pipeline results as JSON.
// Redis key: {namespace}:job:{jobID}:results
// TTL: jobTTL (typically 24 hours), set on initial write.
// This Redis write was removed from the pipeline during the D3 migration: the
// completion worker now persists aggregated results to results-data/{jobID}.json.
// The method is retained for compatibility and tests; it has no production callers.
// Returns error if marshaling or the Redis operation fails.
func (c *RedisClient) SetJobResults(ctx context.Context, jobID string, results *models.JobResults) error {
	key := c.key("job", jobID, "results")
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

// GetJobResults retrieves the complete pipeline results.
// Redis key: {namespace}:job:{jobID}:results
// If the stored value is an artifact reference (sha256:<hex>), the JSON is
// resolved from the artifact store filesystem before unmarshaling. Otherwise
// the raw value is unmarshaled unchanged (legacy payload compatibility).
// Returns unmarshaled models.JobResults on success.
// Returns error with "job results not found" message if key does not exist (redis.Nil).
// Returns error if Redis operation, artifact resolution, or JSON unmarshaling fails.
func (c *RedisClient) GetJobResults(ctx context.Context, jobID string) (*models.JobResults, error) {
	key := c.key("job", jobID, "results")
	data, err := c.client.Get(ctx, key).Bytes()
	if err != nil {
		if err == redis.Nil {
			return nil, fmt.Errorf("job results not found: %s", jobID)
		}
		return nil, fmt.Errorf("failed to get job results: %w", err)
	}

	resolved, wasRef, err := resolveArtifactBytes(ctx, string(data))
	if err != nil {
		return nil, err
	}
	if wasRef {
		data = resolved
	}

	var results models.JobResults
	if err := json.Unmarshal(data, &results); err != nil {
		return nil, fmt.Errorf("failed to unmarshal job results: %w", err)
	}
	return &results, nil
}

// SetJobEmbeddings would store chunk embedding vectors using MessagePack binary serialization.
// Redis key: {namespace}:job:{jobID}:embeddings
// TTL: jobTTL (typically 24 hours), set on initial write.
// The embeddings worker (BAAI/bge-m3) now writes an artifact store reference
// (sha256:<hex>) whose MessagePack payload lives on the FS artifact store, instead of
// the raw blob. This method has no production callers (dead code since the D3 artifact
// store migration) and is retained for compatibility and tests.
// Returns error if marshaling or the Redis operation fails.
func (c *RedisClient) SetJobEmbeddings(ctx context.Context, jobID string, embeddings map[string][]float32) error {
	key := c.key("job", jobID, "embeddings")
	data, err := msgpack.Marshal(embeddings)
	if err != nil {
		return fmt.Errorf("failed to marshal embeddings: %w", err)
	}
	err = c.client.Set(ctx, key, data, c.jobTTL).Err()
	if err != nil {
		return fmt.Errorf("failed to set job embeddings: %w", err)
	}
	return nil
}

// GetJobEmbeddings retrieves chunk embedding vectors for a job.
// Redis key: {namespace}:job:{jobID}:embeddings
// If the stored value is an artifact reference (sha256:<hex>), the MessagePack
// payload is resolved from the artifact store filesystem before unmarshaling.
// Otherwise the raw value is unmarshaled unchanged (legacy payload compatibility).
// Returns map[chunk_id][]float32 on success.
// Returns error with "job embeddings not found" message if key does not exist (redis.Nil).
// Returns error if Redis operation, artifact resolution, or MessagePack unmarshaling fails.
func (c *RedisClient) GetJobEmbeddings(ctx context.Context, jobID string) (map[string][]float32, error) {
	key := c.key("job", jobID, "embeddings")
	data, err := c.client.Get(ctx, key).Bytes()
	if err != nil {
		if err == redis.Nil {
			return nil, fmt.Errorf("job embeddings not found: %s", jobID)
		}
		return nil, fmt.Errorf("failed to get job embeddings: %w", err)
	}

	resolved, wasRef, err := resolveArtifactBytes(ctx, string(data))
	if err != nil {
		return nil, err
	}
	if wasRef {
		data = resolved
	}

	var embeddings map[string][]float32
	if err := msgpack.Unmarshal(data, &embeddings); err != nil {
		return nil, fmt.Errorf("failed to unmarshal embeddings: %w", err)
	}
	return embeddings, nil
}

// SetJobEntities stores recognized named entities from NER processing as JSON.
// Redis key: {namespace}:job:{jobID}:entities
// TTL: jobTTL (typically 24 hours), set on initial write.
// Stores []models.Entity from entities worker (GLiNER).
// Returns error if marshaling or Redis operation fails.
func (c *RedisClient) SetJobEntities(ctx context.Context, jobID string, entities []models.Entity) error {
	key := c.key("job", jobID, "entities")
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

// GetJobEntities retrieves recognized entities for a job.
// Redis key: {namespace}:job:{jobID}:entities
// Returns []models.Entity on success.
// Returns error with "job entities not found" message if key does not exist (redis.Nil).
// Returns error if Redis operation or JSON unmarshaling fails.
func (c *RedisClient) GetJobEntities(ctx context.Context, jobID string) ([]models.Entity, error) {
	key := c.key("job", jobID, "entities")
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

// SetJobMetadata stores document and processing metadata as JSON.
// Redis key: {namespace}:job:{jobID}:metadata
// TTL: jobTTL (typically 24 hours), set on initial write.
// Stores arbitrary map[string]interface{} including document properties and worker output.
// Returns error if marshaling or Redis operation fails.
func (c *RedisClient) SetJobMetadata(ctx context.Context, jobID string, metadata map[string]interface{}) error {
	key := c.key("job", jobID, "metadata")
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

// GetJobMetadata retrieves document and processing metadata for a job.
// Redis key: {namespace}:job:{jobID}:metadata
// Returns map[string]interface{} on success.
// Returns error with "job metadata not found" message if key does not exist (redis.Nil).
// Returns error if Redis operation or JSON unmarshaling fails.
func (c *RedisClient) GetJobMetadata(ctx context.Context, jobID string) (map[string]interface{}, error) {
	key := c.key("job", jobID, "metadata")
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

// UpdateJobStep records the completion status of a processing step.
// Redis key: {namespace}:job:{jobID}:steps (hash with field = step name, value = status)
// TTL: jobTTL (typically 24 hours), refreshed on each write.
// Step is a processing stage name (e.g., "extraction", "embeddings", "entities").
// Status is the completion result (e.g., "completed", "failed").
// Returns error if Redis operation fails.
func (c *RedisClient) UpdateJobStep(ctx context.Context, jobID string, step string, status string) error {
	key := c.key("job", jobID, "steps")
	err := c.client.HSet(ctx, key, step, status).Err()
	if err != nil {
		return fmt.Errorf("failed to update job step: %w", err)
	}
	if err := c.client.Expire(ctx, key, c.jobTTL).Err(); err != nil {
		c.logger.Error().Err(err).Str("key", key).Msg("Failed to set TTL on job steps key")
		return fmt.Errorf("failed to set TTL for key %s: %w", key, err)
	}
	return nil
}

// GetJobSteps retrieves all processing step statuses for a job.
// Redis key: {namespace}:job:{jobID}:steps
// Returns map[string]string where keys are step names and values are step statuses.
// Returns empty map if no steps exist (does not error on missing key).
// Returns error if Redis operation fails.
func (c *RedisClient) GetJobSteps(ctx context.Context, jobID string) (map[string]string, error) {
	key := c.key("job", jobID, "steps")
	steps, err := c.client.HGetAll(ctx, key).Result()
	if err != nil {
		return nil, fmt.Errorf("failed to get job steps: %w", err)
	}
	return steps, nil
}

// SetJobCreated records the job creation timestamp as Unix seconds.
// Redis key: {namespace}:job:{jobID}:meta (hash with field "created_at")
// Also registers the job in the active_jobs sorted set (score = creation Unix timestamp)
// for efficient stuck-job detection without full SCAN. See ExpireStuckJobs.
// TTL: jobTTL (typically 24 hours), refreshed on each write.
// Timestamp is set to current time at creation.
// Returns error if Redis operation fails.
func (c *RedisClient) SetJobCreated(ctx context.Context, jobID string) error {
	now := time.Now()
	key := c.key("job", jobID, "meta")
	err := c.client.HSet(ctx, key, "created_at", now.Unix()).Err()
	if err != nil {
		return fmt.Errorf("failed to set job created time: %w", err)
	}
	if err := c.client.Expire(ctx, key, c.jobTTL).Err(); err != nil {
		c.logger.Error().Err(err).Str("key", key).Msg("Failed to set TTL on job meta key")
		return fmt.Errorf("failed to set TTL for key %s: %w", key, err)
	}

	// Register in active_jobs sorted set for O(log N) stuck-job detection
	activeKey := c.key(activeJobsSuffix)
	if err := c.client.ZAdd(ctx, activeKey, redis.Z{
		Score:  float64(now.Unix()),
		Member: jobID,
	}).Err(); err != nil {
		// Non-fatal: SCAN fallback still works; log and continue
		c.logger.Warn().Err(err).Str("job_id", jobID).Msg("Failed to register job in active_jobs ZSET")
	}

	return nil
}

// SetJobWebhook stores per-request webhook configuration in the job's meta hash.
// Redis key: {namespace}:job:{jobID}:meta (hash with fields "webhook_url" and "webhook_secret")
// TTL: jobTTL (typically 24 hours).
// Both webhookURL and webhookSecret are stored as-is; empty values are allowed but not required.
// Returns error if Redis operation fails.
func (c *RedisClient) SetJobWebhook(ctx context.Context, jobID, webhookURL, webhookSecret string) error {
	key := c.key("job", jobID, "meta")
	err := c.client.HSet(ctx, key, map[string]interface{}{
		"webhook_url":    webhookURL,
		"webhook_secret": webhookSecret,
	}).Err()
	if err != nil {
		return fmt.Errorf("failed to set job webhook: %w", err)
	}
	if err := c.client.Expire(ctx, key, c.jobTTL).Err(); err != nil {
		c.logger.Error().Err(err).Str("key", key).Msg("Failed to set TTL on job meta key")
		return fmt.Errorf("failed to set TTL for key %s: %w", key, err)
	}
	return nil
}

// GetJobCreated retrieves the job creation timestamp.
// Redis key: {namespace}:job:{jobID}:meta
// Returns time.Time converted from stored Unix seconds.
// Returns error with "job created time not found" message if key does not exist (redis.Nil).
// Returns error if Redis operation or timestamp parsing fails.
func (c *RedisClient) GetJobCreated(ctx context.Context, jobID string) (time.Time, error) {
	key := c.key("job", jobID, "meta")
	createdAt, err := c.client.HGet(ctx, key, "created_at").Int64()
	if err != nil {
		if err == redis.Nil {
			return time.Time{}, fmt.Errorf("job created time not found: %s", jobID)
		}
		return time.Time{}, fmt.Errorf("failed to get job created time: %w", err)
	}
	return time.Unix(createdAt, 0), nil
}

// SetJobCompleted records the job completion timestamp as Unix seconds.
// Redis key: {namespace}:job:{jobID}:meta (hash with field "completed_at")
// TTL: jobTTL (typically 24 hours), refreshed on each write.
// Timestamp is set to current time when job finishes (successfully or with error).
// Returns error if Redis operation fails.
func (c *RedisClient) SetJobCompleted(ctx context.Context, jobID string) error {
	key := c.key("job", jobID, "meta")
	err := c.client.HSet(ctx, key, "completed_at", time.Now().Unix()).Err()
	if err != nil {
		return fmt.Errorf("failed to set job completed time: %w", err)
	}
	if err := c.client.Expire(ctx, key, c.jobTTL).Err(); err != nil {
		c.logger.Error().Err(err).Str("key", key).Msg("Failed to set TTL on job meta key")
		return fmt.Errorf("failed to set TTL for key %s: %w", key, err)
	}
	return nil
}

// SetJobError stores the error message when job processing fails.
// Redis key: {namespace}:job:{jobID}:error
// TTL: jobTTL (typically 24 hours), set on initial write.
// Error message is a human-readable description of the failure.
// Returns error if Redis operation fails.
func (c *RedisClient) SetJobError(ctx context.Context, jobID string, errorMsg string) error {
	key := c.key("job", jobID, "error")
	err := c.client.Set(ctx, key, errorMsg, c.jobTTL).Err()
	if err != nil {
		return fmt.Errorf("failed to set job error: %w", err)
	}
	return nil
}

// GetJobError retrieves the error message for a failed job.
// Redis key: {namespace}:job:{jobID}:error
// Returns empty string (not error) if no error is stored (redis.Nil).
// Returns the error message string on success.
// Returns error if Redis operation fails (connection issues).
func (c *RedisClient) GetJobError(ctx context.Context, jobID string) (string, error) {
	key := c.key("job", jobID, "error")
	errMsg, err := c.client.Get(ctx, key).Result()
	if err != nil {
		if err == redis.Nil {
			return "", nil
		}
		return "", fmt.Errorf("failed to get job error: %w", err)
	}
	return errMsg, nil
}

// SetJobFeatures stores feature flags or feature names as JSON array.
// Redis key: {namespace}:job:{jobID}:features
// TTL: jobTTL (typically 24 hours), set on initial write.
// Stores []string of features detected or enabled for this job.
// Returns error if marshaling or Redis operation fails.
func (c *RedisClient) SetJobFeatures(ctx context.Context, jobID string, features []string) error {
	key := c.key("job", jobID, "features")
	data, err := json.Marshal(features)
	if err != nil {
		return fmt.Errorf("failed to marshal features: %w", err)
	}
	err = c.client.Set(ctx, key, data, c.jobTTL).Err()
	if err != nil {
		return fmt.Errorf("failed to set job features: %w", err)
	}
	return nil
}

// GetJobFeatures retrieves feature flags or feature names for a job.
// Redis key: {namespace}:job:{jobID}:features
// Returns []string on success.
// Returns empty slice (not error) if no features are stored (redis.Nil).
// Returns error if Redis operation or JSON unmarshaling fails.
func (c *RedisClient) GetJobFeatures(ctx context.Context, jobID string) ([]string, error) {
	key := c.key("job", jobID, "features")
	data, err := c.client.Get(ctx, key).Result()
	if err != nil {
		if err == redis.Nil {
			return []string{}, nil
		}
		return nil, fmt.Errorf("failed to get job features: %w", err)
	}
	var features []string
	if err := json.Unmarshal([]byte(data), &features); err != nil {
		return nil, fmt.Errorf("failed to unmarshal features: %w", err)
	}
	return features, nil
}

// DeleteJob removes all Redis keys associated with a job, including:
// - status, text, results, embeddings, entities, metadata, steps, error
// - created/completed timestamps, features, LLM configuration
// - supplementary data: chunks, classifications, inferences, raw entities
// This is called when cleaning up after job completion or on explicit deletion requests.
// Returns error if any Redis operation fails (partial cleanup may occur).
func (c *RedisClient) DeleteJob(ctx context.Context, jobID string) error {
	keys := []string{
		c.key("job", jobID, "status"),
		c.key("job", jobID, "text"),
		c.key("job", jobID, "results"),
		c.key("job", jobID, "embeddings"),
		c.key("job", jobID, "inference_embeddings"),
		c.key("job", jobID, "entities"),
		c.key("job", jobID, "entities_raw"),
		c.key("job", jobID, "metadata"),
		c.key("job", jobID, "steps"),
		c.key("job", jobID, "meta"),
		c.key("job", jobID, "error"),
		c.key("job", jobID, "features"),
		c.key("job", jobID, "llm_url"),
		c.key("job", jobID, "source_classification"),
		c.key("job", jobID, "micro_inferences"),
		c.key("job", jobID, "micro_inferences_raw"),
		c.key("job", jobID, "inferences", "remaining"),
		c.key("job", jobID, "inferences", "assembly_lock"),
		c.key("job", jobID, "chunks"),
		c.key("job", jobID, "metadata:document"),
		c.key("job", jobID, "metadata:text"),
	}
	err := c.client.Del(ctx, keys...).Err()
	if err != nil {
		return fmt.Errorf("failed to delete job: %w", err)
	}
	return nil
}

// HealthCheck verifies Redis connectivity with a ping.
// Uses 2-second timeout.
// Returns nil if Redis is healthy, error otherwise.
// Called periodically to detect connection failures.
func (c *RedisClient) HealthCheck() error {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	return c.client.Ping(ctx).Err()
}

// ExpireStuckJobs finds jobs that have been in "pending", "processing", or "extracting" state
// for longer than the specified timeout and marks them as failed.
//
// Algorithm (O(log N + M) using Sorted Set instead of SCAN):
// 1. Queries the active_jobs ZSET for jobs with score < (now - timeout) — O(log N + M)
// 2. For each candidate, verifies job is still in a pending/processing state
// 3. If timeout exceeded: marks job as failed and removes from active_jobs ZSET
//
// Falls back to legacy SCAN if the ZSET is empty (e.g., after a restart before ZSET is populated).
//
// This is critical for preventing zombie jobs that hang forever due to worker crashes
// or network failures. Typically called by a background maintenance goroutine.
//
// Returns error if Redis operation fails. Individual job update failures are logged
// but do not stop processing other jobs.
func (c *RedisClient) ExpireStuckJobs(ctx context.Context, timeout time.Duration) error {
	now := time.Now()
	cutoffScore := fmt.Sprintf("%d", now.Unix()-int64(timeout.Seconds()))
	activeKey := c.key(activeJobsSuffix)

	// Query ZSET for jobs older than cutoff — O(log N + M)
	jobIDs, err := c.client.ZRangeByScore(ctx, activeKey, &redis.ZRangeBy{
		Min: "-inf",
		Max: cutoffScore,
	}).Result()
	if err != nil && err != redis.Nil {
		return fmt.Errorf("failed to query active_jobs ZSET: %w", err)
	}

	// If ZSET has no entries (e.g., first run after restart), fall back to legacy SCAN
	if len(jobIDs) == 0 {
		total, _ := c.client.ZCard(ctx, activeKey).Result()
		if total == 0 {
			return c.expireStuckJobsViaScan(ctx, timeout)
		}
		return nil // No stuck jobs
	}

	for _, jobID := range jobIDs {
		// Check current status
		statusStr, err := c.client.HGet(ctx, c.key("job", jobID, "status"), "status").Result()
		if err != nil && err != redis.Nil {
			c.logger.Warn().Err(err).Str("job_id", jobID).Msg("Failed to read job status")
			continue
		}

		if statusStr == "pending" || statusStr == "processing" || statusStr == "extracting" {
			// Calculate elapsed from ZSET score (no extra Redis call needed)
			scoreCmd := c.client.ZScore(ctx, activeKey, jobID)
			elapsed := now.Sub(time.Unix(int64(scoreCmd.Val()), 0))

			c.logger.Warn().
				Str("job_id", jobID).
				Dur("elapsed", elapsed).
				Dur("timeout", timeout).
				Str("status", statusStr).
				Msg("Job exceeded timeout, marking as failed")

			errorMsg := fmt.Sprintf("Job timeout after %v", timeout)
			if err := c.SetJobError(ctx, jobID, errorMsg); err != nil {
				c.logger.Error().Err(err).Str("job_id", jobID).Msg("Failed to set job error")
			}
			if err := c.client.HSet(ctx, c.key("job", jobID, "status"),
				"status", "failed",
				"error", errorMsg).Err(); err != nil {
				c.logger.Error().Err(err).Str("job_id", jobID).Msg("Failed to update job status")
			}
			// Remove from ZSET — job is now terminal
			c.client.ZRem(ctx, activeKey, jobID) //nolint:errcheck
		} else {
			// Job is in a terminal state but still in ZSET — clean up
			c.client.ZRem(ctx, activeKey, jobID) //nolint:errcheck
		}
	}

	return nil
}

// expireStuckJobsViaScan is the legacy fallback for ExpireStuckJobs when the active_jobs
// ZSET is empty (typically on first run after a restart). Uses SCAN O(N) over all job:*:meta keys.
func (c *RedisClient) expireStuckJobsViaScan(ctx context.Context, timeout time.Duration) error {
	var cursor uint64
	var count int64 = 100

	for {
		keys, newCursor, err := c.client.Scan(ctx, cursor, c.key("job", "*", "meta"), count).Result()
		if err != nil {
			return fmt.Errorf("failed to scan job meta keys: %w", err)
		}

		now := time.Now()

		for _, metaKey := range keys {
			parts := strings.Split(metaKey, ":")
			if len(parts) < 4 {
				continue
			}
			jobID := parts[2]

			createdAtStr, err := c.client.HGet(ctx, metaKey, "created_at").Result()
			if err != nil {
				if err == redis.Nil {
					continue
				}
				c.logger.Warn().Err(err).Str("job_id", jobID).Msg("Failed to read job created_at")
				continue
			}

			createdAt, err := time.Parse(time.RFC3339, createdAtStr)
			if err != nil {
				unixSeconds := 0
				fmt.Sscanf(createdAtStr, "%d", &unixSeconds)
				if unixSeconds > 0 {
					createdAt = time.Unix(int64(unixSeconds), 0)
				} else {
					c.logger.Warn().Str("job_id", jobID).Str("created_at", createdAtStr).
						Msg("Failed to parse job created_at timestamp")
					continue
				}
			}

			if now.Sub(createdAt) > timeout {
				statusStr, err := c.client.HGet(ctx, c.key("job", jobID, "status"), "status").Result()
				if err != nil && err != redis.Nil {
					c.logger.Warn().Err(err).Str("job_id", jobID).Msg("Failed to read job status")
					continue
				}

				if statusStr == "pending" || statusStr == "processing" || statusStr == "extracting" {
					c.logger.Warn().
						Str("job_id", jobID).
						Dur("elapsed", now.Sub(createdAt)).
						Dur("timeout", timeout).
						Str("status", statusStr).
						Msg("Job exceeded timeout (via SCAN fallback), marking as failed")

					errorMsg := fmt.Sprintf("Job timeout after %v", timeout)
					if err := c.SetJobError(ctx, jobID, errorMsg); err != nil {
						c.logger.Error().Err(err).Str("job_id", jobID).Msg("Failed to set job error")
					}
					if err := c.client.HSet(ctx, c.key("job", jobID, "status"),
						"status", "failed",
						"error", errorMsg).Err(); err != nil {
						c.logger.Error().Err(err).Str("job_id", jobID).Msg("Failed to update job status")
					}
				}
			}
		}

		cursor = newCursor
		if cursor == 0 {
			break
		}
	}

	return nil
}

// GetActiveJobCount returns the number of active (non-terminal) jobs.
// Uses ZCARD on the active_jobs sorted set for O(1) complexity.
// Called by AdmissionController to enforce concurrent job limits.
// Returns 0 if the ZSET doesn't exist or on error (fail open).
func (c *RedisClient) GetActiveJobCount(ctx context.Context) (int64, error) {
	activeKey := c.key(activeJobsSuffix)
	count, err := c.client.ZCard(ctx, activeKey).Result()
	if err != nil {
		if err == redis.Nil {
			return 0, nil
		}
		return 0, fmt.Errorf("failed to count active jobs: %w", err)
	}
	return count, nil
}

// Close gracefully closes the Redis connection.
// Called during orchestrator/worker shutdown.
// Returns error if close operation fails.
func (c *RedisClient) Close() error {
	return c.client.Close()
}
