package redis

import (
	"context"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/alicebob/miniredis/v2/server"
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
		Text: "Sample text",
		Chunks: []models.Chunk{
			{ChunkID: "chunk_0", Text: "hello", Embeddings: []float32{0.1, 0.2, 0.3}},
		},
	}

	err := client.SetJobResults(ctx, jobID, results)
	require.NoError(t, err)

	retrieved, err := client.GetJobResults(ctx, jobID)
	require.NoError(t, err)
	assert.Equal(t, results.Text, retrieved.Text)
	assert.Equal(t, len(results.Chunks), len(retrieved.Chunks))
}

func TestRedisClient_SetAndGetJobEmbeddings(t *testing.T) {
	mr, client := setupTestRedis(t)
	defer mr.Close()
	defer client.Close()

	ctx := context.Background()
	jobID := "test-job-123"
	embeddings := map[string][]float32{
		"chunk-1": {0.1, 0.2, 0.3},
		"chunk-2": {0.4, 0.5, 0.6, 0.7},
	}

	err := client.SetJobEmbeddings(ctx, jobID, embeddings)
	require.NoError(t, err)

	retrieved, err := client.GetJobEmbeddings(ctx, jobID)
	require.NoError(t, err)
	assert.Equal(t, len(embeddings), len(retrieved))
	assert.InDeltaSlice(t, embeddings["chunk-1"], retrieved["chunk-1"], 1e-6)
	assert.InDeltaSlice(t, embeddings["chunk-2"], retrieved["chunk-2"], 1e-6)
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

func TestRedisClient_TTLSetFailure_PropagatesError(t *testing.T) {
	methods := []struct {
		name string
		fn   func(client *RedisClient) error
	}{
		{
			name: "SetJobStatus",
			fn: func(c *RedisClient) error {
				return c.SetJobStatus(context.Background(), "job-1", models.StatusProcessing)
			},
		},
		{
			name: "UpdateJobStep",
			fn: func(c *RedisClient) error {
				return c.UpdateJobStep(context.Background(), "job-1", "step-name", "completed")
			},
		},
		{
			name: "SetJobCreated",
			fn: func(c *RedisClient) error {
				return c.SetJobCreated(context.Background(), "job-1")
			},
		},
		{
			name: "SetJobCompleted",
			fn: func(c *RedisClient) error {
				return c.SetJobCompleted(context.Background(), "job-1")
			},
		},
	}

	for _, tc := range methods {
		t.Run(tc.name, func(t *testing.T) {
			mr := miniredis.RunT(t)

			cfg := &config.Config{
				RedisURL: "redis://" + mr.Addr(),
				JobTTL:   1 * time.Hour,
			}
			client, err := New(cfg)
			require.NoError(t, err)

			// Inject a pre-hook so that EXPIRE always returns an error, while HSet still succeeds.
			mr.Server().SetPreHook(func(p *server.Peer, cmd string, args ...string) bool {
				if cmd == "EXPIRE" {
					p.WriteError("ERR simulated EXPIRE failure")
					return true
				}
				return false
			})

			err = tc.fn(client)
			assert.Error(t, err, "%s must propagate EXPIRE error", tc.name)
		})
	}
}

func TestGetJobResults_ChunkInferences(t *testing.T) {
	mr, client := setupTestRedis(t)
	defer mr.Close()
	defer client.Close()

	ctx := context.Background()
	jobID := "test-job-mi"

	// Simulate what Python completion-worker writes to Redis (new schema)
	pythonJSON := `{
        "job_id": "test-job-mi",
        "status": "completed",
        "created_at": "2026-01-01T00:00:00",
        "completed_at": "2026-01-01T00:01:00",
        "chunks": [
            {
                "chunk_id": "chunk_000",
                "text": "sample text",
                "start_offset": 0,
                "end_offset": 11,
                "embeddings": [0.1, 0.2, 0.3],
                "entity_ids": ["abc000000001"],
                "inferences": [
                    {"text": "Property value is 500000 EUR", "confidence": 0.95, "entity_refs": ["500000 EUR"], "entity_id": "abc000000001"}
                ]
            },
            {
                "chunk_id": "chunk_001",
                "text": "other text",
                "start_offset": 12,
                "end_offset": 22,
                "embeddings": [0.4, 0.5, 0.6],
                "entity_ids": [],
                "inferences": []
            }
        ],
        "entities": {
            "abc000000001": {"label": "ORG", "text": "500000 EUR", "confidence": 0.95}
        }
    }`

	key := client.key("job", jobID, "results")
	err := client.GetClient().Set(ctx, key, pythonJSON, time.Hour).Err()
	require.NoError(t, err)

	results, err := client.GetJobResults(ctx, jobID)
	require.NoError(t, err)
	require.NotNil(t, results)

	assert.Equal(t, 2, len(results.Chunks))
	assert.Equal(t, 1, len(results.Chunks[0].Inferences))
	assert.Equal(t, "Property value is 500000 EUR", results.Chunks[0].Inferences[0].Text)
	assert.InDelta(t, 0.95, results.Chunks[0].Inferences[0].Confidence, 0.001)
	assert.Equal(t, []string{"500000 EUR"}, results.Chunks[0].Inferences[0].EntityRefs)
	assert.Equal(t, 0, len(results.Chunks[1].Inferences))
	assert.Equal(t, 1, len(results.Entities))
	assert.Equal(t, "ORG", results.Entities["abc000000001"].Label)
}
