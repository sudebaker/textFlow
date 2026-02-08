package health

import (
	"context"
	"time"

	"ia-text-orchestrator/internal/broker"
	"ia-text-orchestrator/internal/middleware"
	redisclient "ia-text-orchestrator/internal/redis"
)

// CheckResult represents the result of a single health check
type CheckResult struct {
	Status  string                 `json:"status"`
	Message string                 `json:"message,omitempty"`
	Latency int64                  `json:"latency_ms"`
	Details map[string]interface{} `json:"details,omitempty"`
}

// HealthStatus represents the overall health status
type HealthStatus struct {
	Status       string                 `json:"status"`
	Timestamp    time.Time              `json:"timestamp"`
	Service      string                 `json:"service"`
	Version      string                 `json:"version"`
	Checks       map[string]CheckResult `json:"checks"`
	Uptime       string                 `json:"uptime"`
	MemoryUsage  string                 `json:"memory_usage"`
	CircuitState map[string]string      `json:"circuit_state,omitempty"`
}

// HealthChecker performs comprehensive health checks
type HealthChecker struct {
	redis     *redisclient.RedisClient
	broker    *broker.RabbitMQBroker
	startTime time.Time
}

// NewHealthChecker creates a new health checker
func NewHealthChecker(redis *redisclient.RedisClient, broker *broker.RabbitMQBroker) *HealthChecker {
	return &HealthChecker{
		redis:     redis,
		broker:    broker,
		startTime: time.Now(),
	}
}

// Check performs all health checks and returns the overall status
func (hc *HealthChecker) Check(ctx context.Context) *HealthStatus {
	status := &HealthStatus{
		Timestamp:    time.Now(),
		Service:      "orchestrator",
		Version:      "1.0.0",
		Checks:       make(map[string]CheckResult),
		CircuitState: make(map[string]string),
	}

	// Redis check
	status.Checks["redis"] = hc.checkRedis(ctx)

	// RabbitMQ check
	status.Checks["rabbitmq"] = hc.checkRabbitMQ(ctx)

	// Circuit breakers check
	status.Checks["circuit_breakers"] = hc.checkCircuitBreakers()

	// Determine overall status
	allHealthy := true
	for _, check := range status.Checks {
		if check.Status != "healthy" && check.Status != "degraded" {
			allHealthy = false
			break
		}
	}

	if allHealthy {
		status.Status = "healthy"
	} else {
		status.Status = "degraded"
	}

	// Add uptime and memory info
	status.Uptime = time.Since(hc.startTime).String()
	status.MemoryUsage = hc.getMemoryInfo()

	return status
}

// checkRedis performs Redis health check
func (hc *HealthChecker) checkRedis(ctx context.Context) CheckResult {
	start := time.Now()

	// Ping Redis
	err := hc.redis.HealthCheck()
	latency := time.Since(start).Milliseconds()

	if err != nil {
		return CheckResult{
			Status:  "unhealthy",
			Message: err.Error(),
			Latency: latency,
		}
	}

	return CheckResult{
		Status:  "healthy",
		Latency: latency,
	}
}

// checkRabbitMQ performs RabbitMQ health check
func (hc *HealthChecker) checkRabbitMQ(ctx context.Context) CheckResult {
	start := time.Now()

	// Check RabbitMQ connection
	err := hc.broker.HealthCheck()
	latency := time.Since(start).Milliseconds()

	if err != nil {
		return CheckResult{
			Status:  "unhealthy",
			Message: err.Error(),
			Latency: latency,
		}
	}

	// Get queue info for all queues
	queues := []string{"embeddings", "entities", "metadata", "extract_text"}
	queueDetails := make(map[string]interface{})

	for _, queue := range queues {
		info, err := hc.broker.GetQueueInfo(queue)
		if err != nil {
			queueDetails[queue] = map[string]interface{}{
				"error": err.Error(),
			}
		} else {
			queueDetails[queue] = map[string]interface{}{
				"messages":  info.Messages,
				"consumers": info.Consumers,
			}
		}
	}

	return CheckResult{
		Status:  "healthy",
		Latency: latency,
		Details: map[string]interface{}{
			"queues": queueDetails,
		},
	}
}

// checkCircuitBreakers performs circuit breaker health check
func (hc *HealthChecker) checkCircuitBreakers() CheckResult {
	start := time.Now()

	manager := middleware.GetCircuitBreakerManager()
	states := manager.State()

	if len(states) == 0 {
		return CheckResult{
			Status:  "healthy",
			Message: "No circuit breakers configured",
			Latency: time.Since(start).Milliseconds(),
		}
	}

	circuitDetails := make(map[string]interface{})
	hasOpen := false

	for name, state := range states {
		stateStr := state.String()
		circuitDetails[name] = map[string]interface{}{
			"state": stateStr,
		}

		if state == middleware.StateOpen {
			hasOpen = true
		}
	}

	status := "healthy"
	if hasOpen {
		status = "degraded"
	}

	return CheckResult{
		Status:  status,
		Latency: time.Since(start).Milliseconds(),
		Details: map[string]interface{}{
			"breakers": circuitDetails,
		},
	}
}

// getMemoryInfo returns memory usage information
func (hc *HealthChecker) getMemoryInfo() string {
	// This would typically call runtime.MemStats
	return "Check /metrics for detailed memory metrics"
}
