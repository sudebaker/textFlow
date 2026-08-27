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
	broker      BrokerInterface
	config      *config.Config
	rateLimiter *rate.Limiter
}

// RedisClientInterface is a minimal interface for checking active job count.
type RedisClientInterface interface {
	GetActiveJobCount(ctx context.Context) (int64, error)
}

// BrokerInterface is a minimal interface for inspecting queue depth.
// It lets admission control check backpressure on extraction and downstream
// queues without depending on the concrete RabbitMQBroker.
type BrokerInterface interface {
	GetQueueInfo(queue string) (*broker.QueueInfo, error)
}

// NewAdmissionController creates a new AdmissionController with the given config and dependencies.
func NewAdmissionController(cfg *config.Config, redisInst RedisClientInterface, brokerInst BrokerInterface) *AdmissionController {
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

	// 3. Queue depth check (extraction + downstream queues)
	if ac.broker != nil {
		// Check the extraction queue first (primary ingestion bottleneck).
		info, err := ac.broker.GetQueueInfo(ac.config.ExtractQueue)
		if err == nil && info.Messages >= ac.config.QueueDepthRejectThreshold {
			metrics.AdmissionRejected.WithLabelValues("queue_depth").Inc()
			return false, fmt.Sprintf("extraction queue saturated (%d messages)", info.Messages), http.StatusServiceUnavailable
		}

		// Backpressure on downstream queues (spec 3.7): if any downstream queue
		// is saturated, reject new jobs so workers can drain before more work
		// is admitted. This prevents unbounded queue growth behind a slow stage.
		for _, q := range ac.downstreamQueues() {
			info, err := ac.broker.GetQueueInfo(q)
			if err == nil && info.Messages >= ac.config.QueueDepthRejectThreshold {
				metrics.AdmissionRejected.WithLabelValues("queue_depth").Inc()
				return false, fmt.Sprintf("queue %s saturated (%d messages)", q, info.Messages), http.StatusServiceUnavailable
			}
		}
	}

	metrics.AdmissionAccepted.Inc()
	return true, "", http.StatusOK
}

// downstreamQueues returns the queues that admission control should monitor
// for backpressure, in addition to the extraction queue. These are the queues
// that downstream workers consume from; if any is saturated, the pipeline is
// bottlenecked behind that stage.
func (ac *AdmissionController) downstreamQueues() []string {
	return []string{
		ac.config.EmbeddingsQueue,
		ac.config.EntitiesQueue,
		ac.config.MetadataQueue,
		ac.config.InferencesQueue,
	}
}

// GetRateLimiter returns the rate limiter for use in middleware.
func (ac *AdmissionController) GetRateLimiter() *rate.Limiter {
	return ac.rateLimiter
}
