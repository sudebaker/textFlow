package handlers

import (
	"context"
	"fmt"
	"net/http"
	"testing"

	"textflow/internal/broker"
	"textflow/internal/config"
)

// mockRedisClient implements RedisClientInterface for testing.
type mockRedisClient struct {
	activeJobs int64
	err        error
}

func (m *mockRedisClient) GetActiveJobCount(ctx context.Context) (int64, error) {
	return m.activeJobs, m.err
}

// mockBroker implements a minimal broker mock for queue depth testing.
type mockBroker struct {
	queueDepth int
	err        error
}

func (m *mockBroker) GetQueueInfo(queue string) (*broker.QueueInfo, error) {
	if m.err != nil {
		return nil, m.err
	}
	return &broker.QueueInfo{
		Name:     queue,
		Messages: m.queueDepth,
	}, nil
}

func TestAdmissionController_AcceptWhenHealthy(t *testing.T) {
	cfg := &config.Config{
		IngestionRateLimit:        10,
		IngestionRateBurst:        20,
		MaxConcurrentJobs:         30,
		QueueDepthRejectThreshold: 500,
		ExtractQueue:              "extract_text",
	}

	redis := &mockRedisClient{activeJobs: 5}
	ac := NewAdmissionController(cfg, redis, nil)

	accepted, reason, status := ac.CanAcceptJob(context.Background())
	if !accepted {
		t.Errorf("Expected job to be accepted, got rejected: %s (status %d)", reason, status)
	}
	if status != 200 {
		t.Errorf("Expected status 200, got %d", status)
	}
}

func TestAdmissionController_RejectWhenRateLimited(t *testing.T) {
	cfg := &config.Config{
		IngestionRateLimit:        100,
		IngestionRateBurst:        0,
		MaxConcurrentJobs:         30,
		QueueDepthRejectThreshold: 500,
		ExtractQueue:              "extract_text",
	}

	redis := &mockRedisClient{activeJobs: 0}
	ac := NewAdmissionController(cfg, redis, nil)

	// Exhaust the rate limiter
	ac.rateLimiter.Allow()

	accepted, reason, status := ac.CanAcceptJob(context.Background())
	if accepted {
		t.Error("Expected job to be rejected due to rate limit")
	}
	if status != 429 {
		t.Errorf("Expected status 429, got %d", status)
	}
	if reason != "rate limit exceeded" {
		t.Errorf("Expected reason 'rate limit exceeded', got '%s'", reason)
	}
}

func TestAdmissionController_RejectWhenConcurrentJobsExceeded(t *testing.T) {
	cfg := &config.Config{
		IngestionRateLimit:        10,
		IngestionRateBurst:        20,
		MaxConcurrentJobs:         5,
		QueueDepthRejectThreshold: 500,
		ExtractQueue:              "extract_text",
	}

	redis := &mockRedisClient{activeJobs: 5}
	ac := NewAdmissionController(cfg, redis, nil)

	accepted, reason, status := ac.CanAcceptJob(context.Background())
	if accepted {
		t.Error("Expected job to be rejected due to concurrent jobs limit")
	}
	if status != 503 {
		t.Errorf("Expected status 503, got %d", status)
	}
	if reason != "too many jobs in progress (5/5)" {
		t.Errorf("Unexpected reason: %s", reason)
	}
}

func TestAdmissionController_AcceptWhenBelowLimits(t *testing.T) {
	cfg := &config.Config{
		IngestionRateLimit:        10,
		IngestionRateBurst:        20,
		MaxConcurrentJobs:         30,
		QueueDepthRejectThreshold: 500,
		ExtractQueue:              "extract_text",
	}

	redis := &mockRedisClient{activeJobs: 2}
	ac := NewAdmissionController(cfg, redis, nil)

	accepted, reason, status := ac.CanAcceptJob(context.Background())
	if !accepted {
		t.Errorf("Expected job to be accepted, got rejected: %s (status %d)", reason, status)
	}
	if status != 200 {
		t.Errorf("Expected status 200, got %d", status)
	}
}

func TestAdmissionController_NilRedisSkipsConcurrentCheck(t *testing.T) {
	cfg := &config.Config{
		IngestionRateLimit:        10,
		IngestionRateBurst:        20,
		MaxConcurrentJobs:         30,
		QueueDepthRejectThreshold: 500,
		ExtractQueue:              "extract_text",
	}

	ac := NewAdmissionController(cfg, nil, nil)

	accepted, reason, status := ac.CanAcceptJob(context.Background())
	if !accepted {
		t.Errorf("Expected job to be accepted when redis is nil, got rejected: %s (status %d)", reason, status)
	}
	if status != 200 {
		t.Errorf("Expected status 200, got %d", status)
	}
}

func TestAdmissionController_RedisErrorFailsOpen(t *testing.T) {
	cfg := &config.Config{
		IngestionRateLimit:        10,
		IngestionRateBurst:        20,
		MaxConcurrentJobs:         30,
		QueueDepthRejectThreshold: 500,
		ExtractQueue:              "extract_text",
	}

	redis := &mockRedisClient{activeJobs: 0, err: fmt.Errorf("redis connection failed")}
	ac := NewAdmissionController(cfg, redis, nil)

	accepted, reason, status := ac.CanAcceptJob(context.Background())
	if !accepted {
		t.Errorf("Expected job to be accepted when redis has error (fail open), got rejected: %s (status %d)", reason, status)
	}
	if status != 200 {
		t.Errorf("Expected status 200, got %d", status)
	}
}

// mockBrokerByQueue returns a high depth only for a specific queue, so we can
// test downstream backpressure independently of the extraction queue.
type mockBrokerByQueue struct {
	depths map[string]int
}

func (m *mockBrokerByQueue) GetQueueInfo(queue string) (*broker.QueueInfo, error) {
	return &broker.QueueInfo{Name: queue, Messages: m.depths[queue]}, nil
}

func TestAdmissionController_RejectWhenDownstreamQueueSaturated(t *testing.T) {
	cfg := &config.Config{
		IngestionRateLimit:        10,
		IngestionRateBurst:        20,
		MaxConcurrentJobs:         30,
		QueueDepthRejectThreshold: 500,
		ExtractQueue:              "extract_text",
		EmbeddingsQueue:           "embeddings",
		EntitiesQueue:             "entities",
		MetadataQueue:             "metadata",
		InferencesQueue:           "inferences",
	}

	// Extraction queue is fine, but the embeddings queue is saturated.
	broker := &mockBrokerByQueue{depths: map[string]int{
		"extract_text": 10,
		"embeddings":   600,
		"entities":     5,
		"metadata":     5,
		"inferences":   5,
	}}
	ac := NewAdmissionController(cfg, &mockRedisClient{activeJobs: 5}, broker)

	accepted, reason, status := ac.CanAcceptJob(context.Background())
	if accepted {
		t.Errorf("Expected job to be rejected when downstream queue saturated, got accepted")
	}
	if status != http.StatusServiceUnavailable {
		t.Errorf("Expected status 503, got %d", status)
	}
	if reason == "" {
		t.Errorf("Expected a reason mentioning the saturated queue, got empty")
	}
}
