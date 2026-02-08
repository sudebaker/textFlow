package testutils

import (
	"context"
	"encoding/json"
	"fmt"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"ia-text-orchestrator/internal/config"
	"ia-text-orchestrator/internal/models"
)

// TestRedisClient is a fixture for testing Redis operations
type TestRedisClient struct {
	client *redis.Client
	mr     *miniredis.Miniredis
	t      *testing.T
}

// NewTestRedisClient creates a new test Redis client
func NewTestRedisClient(t *testing.T) *TestRedisClient {
	mr, err := miniredis.Run()
	require.NoError(t, err)

	client := redis.NewClient(&redis.Options{
		Addr: mr.Addr(),
	})

	return &TestRedisClient{
		client: client,
		mr:     mr,
		t:      t,
	}
}

// Close closes the test Redis instance
func (tc *TestRedisClient) Close() {
	if tc.client != nil {
		tc.client.Close()
	}
	if tc.mr != nil {
		tc.mr.Close()
	}
}

// Ping checks if Redis is responding
func (tc *TestRedisClient) Ping(ctx context.Context) error {
	return tc.client.Ping(ctx).Err()
}

// Get gets a value by key
func (tc *TestRedisClient) Get(ctx context.Context, key string) (string, error) {
	return tc.client.Get(ctx, key).Result()
}

// Set sets a value
func (tc *TestRedisClient) Set(ctx context.Context, key, value string) error {
	return tc.client.Set(ctx, key, value, 0).Err()
}

// Del deletes a key
func (tc *TestRedisClient) Del(ctx context.Context, keys ...string) error {
	if len(keys) == 0 {
		return nil
	}
	return tc.client.Del(ctx, keys...).Err()
}

// Exists checks if key exists
func (tc *TestRedisClient) Exists(ctx context.Context, key string) (int64, error) {
	return tc.client.Exists(ctx, key).Result()
}

// HGet gets a field from a hash
func (tc *TestRedisClient) HGet(ctx context.Context, key, field string) (string, error) {
	return tc.client.HGet(ctx, key, field).Result()
}

// HSet sets a field in a hash
func (tc *TestRedisClient) HSet(ctx context.Context, key, field, value string) error {
	return tc.client.HSet(ctx, key, field, value).Err()
}

// HGetAll gets all fields from a hash
func (tc *TestRedisClient) HGetAll(ctx context.Context, key string) (map[string]string, error) {
	return tc.client.HGetAll(ctx, key).Result()
}

// MustSetJobStatus sets a job status and fails the test on error
func (tc *TestRedisClient) MustSetJobStatus(ctx context.Context, jobID, status string) {
	key := "orchestrator:job:" + jobID + ":status"
	err := tc.client.Set(ctx, key, status, 0).Err()
	require.NoError(tc.t, err)
}

// MustGetJobStatus gets a job status and asserts it matches expected
func (tc *TestRedisClient) MustGetJobStatus(ctx context.Context, jobID, expected string) string {
	key := "orchestrator:job:" + jobID + ":status"
	val, err := tc.client.Get(ctx, key).Result()
	require.NoError(tc.t, err)
	assert.Equal(tc.t, expected, val)
	return val
}

// MustSetJobData sets job data (text, embeddings, entities, metadata)
func (tc *TestRedisClient) MustSetJobData(ctx context.Context, jobID, dataType string, data interface{}) {
	key := "orchestrator:job:" + jobID + ":" + dataType
	var value string

	switch v := data.(type) {
	case string:
		value = v
	case []float64:
		jsonBytes, err := json.Marshal(v)
		require.NoError(tc.t, err)
		value = string(jsonBytes)
	default:
		jsonBytes, err := json.Marshal(v)
		require.NoError(tc.t, err)
		value = string(jsonBytes)
	}

	err := tc.client.Set(ctx, key, value, 0).Err()
	require.NoError(tc.t, err)
}

// Helper to serialize float64 slice
func serializeFloat64Slice(v []float64) string {
	result := "["
	for i, f := range v {
		if i > 0 {
			result += ","
		}
		result += formatFloat(f)
	}
	return result + "]"
}

func formatFloat(f float64) string {
	return float64ToString(f)
}

func float64ToString(f float64) string {
	return string(rune('0' + int(f))) // Simplified
}

// TestConfig is a fixture for testing configuration
type TestConfig struct {
	cfg *config.Config
}

// NewTestConfig creates a new test configuration
func NewTestConfig() *TestConfig {
	return &TestConfig{
		cfg: &config.Config{
			RabbitMQURL:        "amqp://guest:guest@localhost:5672/",
			RedisURL:           "redis://localhost:6379",
			UnstructuredURL:    "http://localhost:8000",
			ResourceManagerURL: "http://localhost:9090",
			HTTPPort:           8080,
			LogLevel:           "debug",
			JobTimeout:         5 * time.Minute,
			JobTTL:             24 * time.Hour,
			MaxRetries:         3,
			RetryDelay:         1 * time.Second,
			EmbeddingsQueue:    "embeddings",
			EntitiesQueue:      "entities",
			ExtractQueue:       "extract_text",
			MetadataQueue:      "metadata",
		},
	}
}

// Config returns the test configuration
func (tc *TestConfig) Config() *config.Config {
	return tc.cfg
}

// WithRedisURL updates the Redis URL
func (tc *TestConfig) WithRedisURL(url string) *TestConfig {
	tc.cfg.RedisURL = url
	return tc
}

// WithRabbitMQURL updates the RabbitMQ URL
func (tc *TestConfig) WithRabbitMQURL(url string) *TestConfig {
	tc.cfg.RabbitMQURL = url
	return tc
}

// WithHTTPPort updates the HTTP port
func (tc *TestConfig) WithHTTPPort(port int) *TestConfig {
	tc.cfg.HTTPPort = port
	return tc
}

// TestJob is a fixture for creating test jobs
type TestJob struct {
	jobID      string
	status     string
	text       string
	embeddings []float64
	entities   []models.Entity
	metadata   map[string]interface{}
	steps      map[string]string
}

// NewTestJob creates a new test job
func NewTestJob(jobID string) *TestJob {
	return &TestJob{
		jobID:    jobID,
		status:   string(models.StatusPending),
		steps:    make(map[string]string),
		metadata: make(map[string]interface{}),
	}
}

// WithStatus sets the job status
func (tj *TestJob) WithStatus(status string) *TestJob {
	tj.status = status
	return tj
}

// WithText sets the extracted text
func (tj *TestJob) WithText(text string) *TestJob {
	tj.text = text
	return tj
}

// WithEmbeddings sets the embeddings
func (tj *TestJob) WithEmbeddings(embeddings []float64) *TestJob {
	tj.embeddings = embeddings
	return tj
}

// WithEntity adds an entity
func (tj *TestJob) WithEntity(entity models.Entity) *TestJob {
	tj.entities = append(tj.entities, entity)
	return tj
}

// WithMetadata sets metadata
func (tj *TestJob) WithMetadata(key string, value interface{}) *TestJob {
	tj.metadata[key] = value
	return tj
}

// WithStep adds a step status
func (tj *TestJob) WithStep(step, status string) *TestJob {
	tj.steps[step] = status
	return tj
}

// Build builds the test job and stores it in Redis
func (tj *TestJob) Build(ctx context.Context, redis *TestRedisClient) {
	// Set status
	redis.MustSetJobStatus(ctx, tj.jobID, tj.status)

	// Set text
	if tj.text != "" {
		redis.MustSetJobData(ctx, tj.jobID, "text", tj.text)
	}

	// Set embeddings
	if tj.embeddings != nil {
		redis.MustSetJobData(ctx, tj.jobID, "embeddings", tj.embeddings)
	}

	// Set metadata
	for k, v := range tj.metadata {
		key := "orchestrator:job:" + tj.jobID + ":meta"
		value := fmt.Sprintf("%v", v)
		_ = redis.HSet(ctx, key, k, value)
	}

	// Set steps
	for step, status := range tj.steps {
		key := "orchestrator:job:" + tj.jobID + ":steps"
		_ = redis.HSet(ctx, key, step, status)
	}
}

// JobID returns the job ID
func (tj *TestJob) JobID() string {
	return tj.jobID
}

// Status returns the job status
func (tj *TestJob) Status() string {
	return tj.status
}

// AssertionHelpers provides assertion helpers for tests
type AssertionHelpers struct {
	t *testing.T
}

// NewAssertionHelpers creates new assertion helpers
func NewAssertionHelpers(t *testing.T) *AssertionHelpers {
	return &AssertionHelpers{t: t}
}

// AssertJobStatus asserts the job status matches expected
func (ah *AssertionHelpers) AssertJobStatus(ctx context.Context, redis *TestRedisClient, jobID, expected string) {
	actual, err := redis.Get(ctx, "orchestrator:job:"+jobID+":status")
	assert.NoError(ah.t, err)
	assert.Equal(ah.t, expected, actual)
}

// AssertJobExists asserts the job exists in Redis
func (ah *AssertionHelpers) AssertJobExists(ctx context.Context, redis *TestRedisClient, jobID string) {
	exists, err := redis.Exists(ctx, "orchestrator:job:"+jobID+":status")
	assert.NoError(ah.t, err)
	assert.True(ah.t, exists > 0, "Job %s should exist", jobID)
}

// AssertJobNotExists asserts the job does not exist in Redis
func (ah *AssertionHelpers) AssertJobNotExists(ctx context.Context, redis *TestRedisClient, jobID string) {
	exists, err := redis.Exists(ctx, "orchestrator:job:"+jobID+":status")
	assert.NoError(ah.t, err)
	assert.False(ah.t, exists > 0, "Job %s should not exist", jobID)
}

// AssertStepCompleted asserts a step is completed
func (ah *AssertionHelpers) AssertStepCompleted(ctx context.Context, redis *TestRedisClient, jobID, step string) {
	val, err := redis.HGet(ctx, "orchestrator:job:"+jobID+":steps", step)
	assert.NoError(ah.t, err)
	assert.Equal(ah.t, "completed", val, "Step %s should be completed", step)
}

// AssertContainsText asserts the extracted text contains expected substring
func (ah *AssertionHelpers) AssertContainsText(ctx context.Context, redis *TestRedisClient, jobID, expected string) {
	text, err := redis.Get(ctx, "orchestrator:job:"+jobID+":text")
	assert.NoError(ah.t, err)
	assert.Contains(ah.t, text, expected)
}

// Cleanup provides cleanup utilities
type Cleanup struct {
	t   *testing.T
	fns []func()
}

// NewCleanup creates a new cleanup helper
func NewCleanup(t *testing.T) *Cleanup {
	return &Cleanup{t: t, fns: make([]func(), 0)}
}

// Add adds a cleanup function
func (c *Cleanup) Add(fn func()) {
	c.fns = append(c.fns, fn)
}

// Run runs all cleanup functions
func (c *Cleanup) Run() {
	for _, fn := range c.fns {
		fn()
	}
}

// Deferred runs cleanup when function returns
func (c *Cleanup) Deferred() {
	c.t.Cleanup(func() {
		c.Run()
	})
}

// ContextWithTimeout creates a context with timeout
func ContextWithTimeout(t *testing.T, duration time.Duration) context.Context {
	ctx, cancel := context.WithTimeout(context.Background(), duration)
	t.Cleanup(cancel)
	return ctx
}

// ContextWithCancel creates a context with cancel
func ContextWithCancel(t *testing.T) (context.Context, context.CancelFunc) {
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	return ctx, cancel
}
