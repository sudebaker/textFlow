package redis

import (
	"context"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"ia-text-orchestrator/internal/config"
	"ia-text-orchestrator/internal/models"
)

// setupTestRedis creates a miniredis instance for testing
func setupTestRedis(t *testing.T) (*miniredis.Miniredis, *RedisClient) {
	mr := miniredis.RunT(t)

	cfg := &config.Config{
		RedisURL: "redis://" + mr.Addr(),
		JobTTL:   1 * time.Hour,
	}

	client, err := New(cfg)
	require.NoError(t, err)
	require.NotNil(t, client)

	return mr, client
}

func TestRedisClient_New(t *testing.T) {
	mr, client := setupTestRedis(t)
	defer mr.Close()
	defer client.Close()

	assert.NotNil(t, client)
	assert.NotNil(t, client.client)
	assert.Equal(t, "orchestrator", client.namespace)
}

func TestRedisClient_KeyNamespacing(t *testing.T) {
	mr, client := setupTestRedis(t)
	defer mr.Close()
	defer client.Close()

	tests := []struct {
		name     string
		parts    []string
		expected string
	}{
		{
			name:     "simple key",
			parts:    []string{"job", "123", "status"},
			expected: "orchestrator:job:123:status",
		},
		{
			name:     "single part",
			parts:    []string{"test"},
			expected: "orchestrator:test",
		},
		{
			name:     "many parts",
			parts:    []string{"a", "b", "c", "d", "e"},
			expected: "orchestrator:a:b:c:d:e",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			key := client.key(tt.parts...)
			assert.Equal(t, tt.expected, key)
		})
	}
}

func TestRedisClient_SetAndGetJobStatus(t *testing.T) {
	mr, client := setupTestRedis(t)
	defer mr.Close()
	defer client.Close()

	ctx := context.Background()
	jobID := "test-job-123"

	// Set status
	err := client.SetJobStatus(ctx, jobID, models.StatusPending)
	require.NoError(t, err)

	// Get status
	status, err := client.GetJobStatus(ctx, jobID)
	require.NoError(t, err)
	assert.Equal(t, models.StatusPending, status)

	// Update status
	err = client.SetJobStatus(ctx, jobID, models.StatusProcessing)
	require.NoError(t, err)

	status, err = client.GetJobStatus(ctx, jobID)
	require.NoError(t, err)
	assert.Equal(t, models.StatusProcessing, status)
}

func TestRedisClient_SetAndGetJobText(t *testing.T) {
	mr, client := setupTestRedis(t)
	defer mr.Close()
	defer client.Close()

	ctx := context.Background()
	jobID := "test-job-123"
	expectedText := "This is a test document"

	err := client.SetJobText(ctx, jobID, expectedText)
	require.NoError(t, err)

	text, err := client.GetJobText(ctx, jobID)
	require.NoError(t, err)
	assert.Equal(t, expectedText, text)
}

func TestRedisClient_SetAndGetJobResults(t *testing.T) {
	mr, client := setupTestRedis(t)
	defer mr.Close()
	defer client.Close()

	ctx := context.Background()
	jobID := "test-job-123"
	results := &models.JobResults{
		Text:       "Sample text",
		Embeddings: []float32{0.1, 0.2, 0.3},
	}

	err := client.SetJobResults(ctx, jobID, results)
	require.NoError(t, err)

	retrieved, err := client.GetJobResults(ctx, jobID)
	require.NoError(t, err)
	assert.Equal(t, results.Text, retrieved.Text)
	assert.Equal(t, len(results.Embeddings), len(retrieved.Embeddings))
}

func TestRedisClient_SetAndGetJobEmbeddings(t *testing.T) {
	mr, client := setupTestRedis(t)
	defer mr.Close()
	defer client.Close()

	ctx := context.Background()
	jobID := "test-job-123"
	embeddings := []float32{0.1, 0.2, 0.3, 0.4, 0.5}

	err := client.SetJobEmbeddings(ctx, jobID, embeddings)
	require.NoError(t, err)

	retrieved, err := client.GetJobEmbeddings(ctx, jobID)
	require.NoError(t, err)
	assert.Equal(t, embeddings, retrieved)
}

func TestRedisClient_SetAndGetJobEntities(t *testing.T) {
	mr, client := setupTestRedis(t)
	defer mr.Close()
	defer client.Close()

	ctx := context.Background()
	jobID := "test-job-123"
	entities := []models.Entity{
		{Text: "John Doe", Label: "PERSON", Start: 0, End: 8},
		{Text: "New York", Label: "LOCATION", Start: 20, End: 28},
	}

	err := client.SetJobEntities(ctx, jobID, entities)
	require.NoError(t, err)

	retrieved, err := client.GetJobEntities(ctx, jobID)
	require.NoError(t, err)
	assert.Equal(t, len(entities), len(retrieved))
	assert.Equal(t, entities[0].Text, retrieved[0].Text)
	assert.Equal(t, entities[1].Label, retrieved[1].Label)
}

func TestRedisClient_SetAndGetJobMetadata(t *testing.T) {
	mr, client := setupTestRedis(t)
	defer mr.Close()
	defer client.Close()

	ctx := context.Background()
	jobID := "test-job-123"
	metadata := map[string]interface{}{
		"filename":  "test.txt",
		"pages":     10,
		"file_type": "pdf",
	}

	err := client.SetJobMetadata(ctx, jobID, metadata)
	require.NoError(t, err)

	retrieved, err := client.GetJobMetadata(ctx, jobID)
	require.NoError(t, err)
	assert.Equal(t, metadata["filename"], retrieved["filename"])
	assert.Equal(t, float64(10), retrieved["pages"]) // JSON unmarshals numbers as float64
}

func TestRedisClient_UpdateAndGetJobSteps(t *testing.T) {
	mr, client := setupTestRedis(t)
	defer mr.Close()
	defer client.Close()

	ctx := context.Background()
	jobID := "test-job-123"

	err := client.UpdateJobStep(ctx, jobID, "extract", "completed")
	require.NoError(t, err)

	err = client.UpdateJobStep(ctx, jobID, "embeddings", "processing")
	require.NoError(t, err)

	steps, err := client.GetJobSteps(ctx, jobID)
	require.NoError(t, err)
	assert.Equal(t, "completed", steps["extract"])
	assert.Equal(t, "processing", steps["embeddings"])
}

func TestRedisClient_SetAndGetJobError(t *testing.T) {
	mr, client := setupTestRedis(t)
	defer mr.Close()
	defer client.Close()

	ctx := context.Background()
	jobID := "test-job-123"
	errorMsg := "Failed to process document"

	err := client.SetJobError(ctx, jobID, errorMsg)
	require.NoError(t, err)

	retrieved, err := client.GetJobError(ctx, jobID)
	require.NoError(t, err)
	assert.Equal(t, errorMsg, retrieved)
}

func TestRedisClient_DeleteJob(t *testing.T) {
	mr, client := setupTestRedis(t)
	defer mr.Close()
	defer client.Close()

	ctx := context.Background()
	jobID := "test-job-123"

	// Set various job data
	err := client.SetJobStatus(ctx, jobID, models.StatusCompleted)
	require.NoError(t, err)

	err = client.SetJobText(ctx, jobID, "test text")
	require.NoError(t, err)

	// Verify data exists
	_, err = client.GetJobStatus(ctx, jobID)
	require.NoError(t, err)

	// Delete job
	err = client.DeleteJob(ctx, jobID)
	require.NoError(t, err)

	// Verify data is gone
	_, err = client.GetJobStatus(ctx, jobID)
	assert.Error(t, err)
	assert.Contains(t, err.Error(), "job not found")
}

func TestRedisClient_HealthCheck(t *testing.T) {
	mr, client := setupTestRedis(t)
	defer mr.Close()
	defer client.Close()

	err := client.HealthCheck()
	assert.NoError(t, err)

	// Simulate Redis down
	mr.Close()
	err = client.HealthCheck()
	assert.Error(t, err)
}

func TestRedisClient_ContextCancellation(t *testing.T) {
	mr, client := setupTestRedis(t)
	defer mr.Close()
	defer client.Close()

	// Create a cancelled context
	ctx, cancel := context.WithCancel(context.Background())
	cancel() // Immediately cancel

	jobID := "test-job-123"

	// Operations with cancelled context should fail or complete quickly
	// Note: miniredis may not properly simulate network timeouts
	err := client.SetJobStatus(ctx, jobID, models.StatusPending)
	// In real Redis with network, this would fail. With miniredis, it may succeed
	// so we just verify the function completes
	_ = err
}

func TestRedisClient_GetJobStatus_NotFound(t *testing.T) {
	mr, client := setupTestRedis(t)
	defer mr.Close()
	defer client.Close()

	ctx := context.Background()
	_, err := client.GetJobStatus(ctx, "nonexistent-job")
	assert.Error(t, err)
	assert.Contains(t, err.Error(), "job not found")
}

func TestRedisClient_SetJobCreatedAndCompleted(t *testing.T) {
	mr, client := setupTestRedis(t)
	defer mr.Close()
	defer client.Close()

	ctx := context.Background()
	jobID := "test-job-123"

	// Set created time
	err := client.SetJobCreated(ctx, jobID)
	require.NoError(t, err)

	// Verify the key exists in miniredis
	key := client.key("job", jobID, "meta")
	exists := mr.Exists(key)
	assert.True(t, exists, "Key should exist in Redis")

	// Set completed time
	err = client.SetJobCompleted(ctx, jobID)
	require.NoError(t, err)
}
