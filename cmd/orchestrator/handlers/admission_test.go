package handlers

import (
	"context"
	"fmt"
	"testing"

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

func (m *mockBroker) GetQueueInfo(queue string) (*QueueInfo, error) {
	if m.err != nil {
		return nil, m.err
	}
	return &QueueInfo{
		Name:     queue,
		Messages: m.queueDepth,
	}, nil
}

// QueueInfo is exported for testing.
type QueueInfo struct {
	Name      string `json:"name"`
	Messages  int    `json:"messages"`
	Consumers int    `json:"consumers"`
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
