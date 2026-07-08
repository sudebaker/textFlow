package handlers

import (
	"context"
	"fmt"
	"net/http"

	"golang.org/x/time/rate"
	"textflow/internal/broker"
	"textflow/internal/config"
	"textflow/pkg/metrics"
)

// AdmissionController enforces system capacity limits before accepting new jobs.
// It checks three conditions:
//  1. Global rate limit (token bucket) — prevents bursts from overwhelming workers
//  2. Concurrent jobs limit (Redis counter) — prevents too many jobs in flight
//  3. Queue depth (RabbitMQ inspect) — rejects when extraction queue is saturated
//
// Returns (accepted bool, reason string, HTTP status code).
// When rejected, the client receives Retry-After header to know when to retry.
type AdmissionController struct {
	redis       RedisClientInterface
	broker      *broker.RabbitMQBroker
	config      *config.Config
	rateLimiter *rate.Limiter
}

// RedisClientInterface is a minimal interface for checking active job count.
type RedisClientInterface interface {
	GetActiveJobCount(ctx context.Context) (int64, error)
}

// NewAdmissionController creates a new AdmissionController with the given config and dependencies.
func NewAdmissionController(cfg *config.Config, redisInst RedisClientInterface, brokerInst *broker.RabbitMQBroker) *AdmissionController {
	return &AdmissionController{
		redis:       redisInst,
		broker:      brokerInst,
		config:      cfg,
		rateLimiter: rate.NewLimiter(rate.Limit(cfg.IngestionRateLimit), cfg.IngestionRateBurst),
	}
}

// CanAcceptJob checks if the system has capacity to accept a new job.
// Returns (accepted, reason, statusCode).
// Status codes: 200=accepted, 429=rate limited, 503=capacity full.
func (ac *AdmissionController) CanAcceptJob(ctx context.Context) (bool, string, int) {
	// 1. Rate limit (token bucket)
	if !ac.rateLimiter.Allow() {
		metrics.AdmissionRejected.WithLabelValues("rate_limit").Inc()
		return false, "rate limit exceeded", http.StatusTooManyRequests
	}

	// 2. Concurrent jobs limit (Redis counter)
	if ac.redis != nil {
		active, err := ac.redis.GetActiveJobCount(ctx)
		if err == nil && int(active) >= ac.config.MaxConcurrentJobs {
			metrics.AdmissionRejected.WithLabelValues("concurrent_jobs").Inc()
			return false, fmt.Sprintf("too many jobs in progress (%d/%d)", active, ac.config.MaxConcurrentJobs), http.StatusServiceUnavailable
		}
	}

	// 3. Queue depth check
	if ac.broker != nil {
		info, err := ac.broker.GetQueueInfo(ac.config.ExtractQueue)
		if err == nil && info.Messages >= ac.config.QueueDepthRejectThreshold {
			metrics.AdmissionRejected.WithLabelValues("queue_depth").Inc()
			return false, fmt.Sprintf("extraction queue saturated (%d messages)", info.Messages), http.StatusServiceUnavailable
		}
	}

	metrics.AdmissionAccepted.Inc()
	return true, "", http.StatusOK
}

// GetRateLimiter returns the rate limiter for use in middleware.
func (ac *AdmissionController) GetRateLimiter() *rate.Limiter {
	return ac.rateLimiter
}
