package middleware

import (
	"context"
	"sync"
	"time"

	"ia-text-orchestrator/pkg/metrics"
)

type CircuitBreakerManager struct {
	breakers map[string]*CircuitBreaker
	mu       sync.RWMutex
}

var (
	circuitBreakerManager *CircuitBreakerManager
	once                  sync.Once
)

// GetCircuitBreakerManager returns the singleton instance
func GetCircuitBreakerManager() *CircuitBreakerManager {
	once.Do(func() {
		circuitBreakerManager = &CircuitBreakerManager{
			breakers: make(map[string]*CircuitBreaker),
		}
	})
	return circuitBreakerManager
}

// GetOrCreate returns an existing circuit breaker or creates a new one
func (m *CircuitBreakerManager) GetOrCreate(name string, settings Settings) *CircuitBreaker {
	m.mu.Lock()
	defer m.mu.Unlock()

	if cb, ok := m.breakers[name]; ok {
		return cb
	}

	cb := NewCircuitBreaker(settings)
	m.breakers[name] = cb
	return cb
}

// Get returns an existing circuit breaker (returns nil if not exists)
func (m *CircuitBreakerManager) Get(name string) *CircuitBreaker {
	m.mu.RLock()
	defer m.mu.RUnlock()

	return m.breakers[name]
}

// Remove removes a circuit breaker
func (m *CircuitBreakerManager) Remove(name string) {
	m.mu.Lock()
	defer m.mu.Unlock()

	delete(m.breakers, name)
}

// State returns the current state of all circuit breakers
func (m *CircuitBreakerManager) State() map[string]CircuitState {
	m.mu.RLock()
	defer m.mu.RUnlock()

	state := make(map[string]CircuitState)
	for name, cb := range m.breakers {
		state[name] = cb.State()
	}
	return state
}

// Counts returns the current counts of all circuit breakers
func (m *CircuitBreakerManager) Counts() map[string]Counts {
	m.mu.RLock()
	defer m.mu.RUnlock()

	counts := make(map[string]Counts)
	for name, cb := range m.breakers {
		counts[name] = cb.Counts()
	}
	return counts
}

// Execute wraps a function with circuit breaker logic and metrics
func (m *CircuitBreakerManager) Execute(ctx context.Context, name string, settings Settings, fn func() error) error {
	cb := m.GetOrCreate(name, settings)

	// Record circuit breaker state metric
	state := cb.State()
	metrics.CircuitBreakerState.WithLabelValues(name).Set(float64(state))

	// Execute with circuit breaker
	err := cb.Execute(ctx, fn)

	// Record metrics
	if err != nil {
		switch err {
		case ErrCircuitOpen:
			metrics.CircuitBreakerOpen.WithLabelValues(name).Inc()
		case ErrCircuitTooMany:
			metrics.CircuitBreakerTooMany.WithLabelValues(name).Inc()
		default:
			metrics.CircuitBreakerErrors.WithLabelValues(name).Inc()
		}
	}

	return err
}

// Common circuit breaker configurations
var (
	// For external API calls (Docling, Resource Manager)
	ExternalAPIConfig = Settings{
		MaxRequests: 5,
		Interval:    30 * time.Second,
		Timeout:     30 * time.Second,
		ReadyToTrip: func(counts Counts) bool {
			// Open if more than 50% failures in window
			if counts.Requests < 3 {
				return false
			}
			failureRatio := float64(counts.Failures) / float64(counts.Requests)
			return failureRatio >= 0.5
		},
	}

	// For database operations (Redis)
	DatabaseConfig = Settings{
		MaxRequests: 10,
		Interval:    10 * time.Second,
		Timeout:     5 * time.Second,
		ReadyToTrip: func(counts Counts) bool {
			// More aggressive for databases
			if counts.Requests < 5 {
				return false
			}
			failureRatio := float64(counts.Failures) / float64(counts.Requests)
			return failureRatio >= 0.3
		},
	}

	// For message queue operations (RabbitMQ)
	MessageQueueConfig = Settings{
		MaxRequests: 10,
		Interval:    15 * time.Second,
		Timeout:     15 * time.Second,
		ReadyToTrip: func(counts Counts) bool {
			if counts.Requests < 5 {
				return false
			}
			failureRatio := float64(counts.Failures) / float64(counts.Requests)
			return failureRatio >= 0.4
		},
	}
)
