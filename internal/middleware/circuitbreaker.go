package middleware

import (
	"context"
	"errors"
	"sync"
	"time"
)

type CircuitState int

const (
	StateClosed CircuitState = iota
	StateHalfOpen
	StateOpen
)

func (s CircuitState) String() string {
	switch s {
	case StateClosed:
		return "closed"
	case StateHalfOpen:
		return "half-open"
	case StateOpen:
		return "open"
	default:
		return "unknown"
	}
}

var (
	ErrCircuitOpen    = errors.New("circuit breaker is open")
	ErrCircuitTooMany = errors.New("too many requests while circuit is half-open")
)

type Counts struct {
	Requests            int
	Successes           int
	Failures            int
	Timeouts            int
	ContextCancelled    int
	ConcurrencyInFlight int
}

type Settings struct {
	Name         string
	MaxRequests  uint32
	Interval     time.Duration
	Timeout      time.Duration
	ReadyToTrip  func(counts Counts) bool
	IsSuccessful func(err error) bool
}

type CircuitBreaker struct {
	name              string
	state             CircuitState
	failures          int
	successes         int
	requests          int
	lastStateChange   time.Time
	lastIntervalStart time.Time
	windowCounts      Counts
	mu                sync.RWMutex
	settings          Settings
}

func NewCircuitBreaker(settings Settings) *CircuitBreaker {
	cb := &CircuitBreaker{
		name:     settings.Name,
		state:    StateClosed,
		settings: settings,
	}
	if cb.settings.MaxRequests == 0 {
		cb.settings.MaxRequests = 3
	}
	if cb.settings.Interval == 0 {
		cb.settings.Interval = 30 * time.Second
	}
	if cb.settings.Timeout == 0 {
		cb.settings.Timeout = 10 * time.Second
	}
	if cb.settings.ReadyToTrip == nil {
		cb.settings.ReadyToTrip = defaultReadyToTrip
	}
	if cb.settings.IsSuccessful == nil {
		cb.settings.IsSuccessful = defaultIsSuccessful
	}
	return cb
}

func defaultReadyToTrip(counts Counts) bool {
	failureRatio := float64(counts.Failures) / float64(counts.Requests)
	return counts.Requests >= 3 && failureRatio >= 0.6
}

func defaultIsSuccessful(err error) bool {
	return err == nil
}

func (cb *CircuitBreaker) Execute(ctx context.Context, fn func() error) error {
	if err := cb.beforeRequest(); err != nil {
		return err
	}

	result := cb.executeRequest(ctx, fn)
	cb.afterRequest(result)
	return result
}

func (cb *CircuitBreaker) beforeRequest() error {
	cb.mu.Lock()
	state := cb.state
	cb.mu.Unlock()

	switch state {
	case StateOpen:
		return ErrCircuitOpen
	case StateHalfOpen:
		cb.mu.Lock()
		cb.requests++
		if cb.requests > int(cb.settings.MaxRequests) {
			cb.mu.Unlock()
			return ErrCircuitTooMany
		}
		cb.mu.Unlock()
	case StateClosed:
		cb.mu.Lock()
		cb.requests++
		cb.windowCounts.Requests++
		cb.mu.Unlock()
	}

	return nil
}

func (cb *CircuitBreaker) executeRequest(ctx context.Context, fn func() error) (err error) {
	defer func() {
		if r := recover(); r != nil {
			err = errors.New("panic recovered")
		}
	}()

	done := make(chan error, 1)
	go func() {
		done <- fn()
	}()

	select {
	case <-ctx.Done():
		cb.mu.Lock()
		cb.windowCounts.ContextCancelled++
		cb.mu.Unlock()
		return ctx.Err()
	case err := <-done:
		return err
	}
}

func (cb *CircuitBreaker) afterRequest(result error) {
	cb.mu.Lock()
	defer cb.mu.Unlock()

	now := time.Now()

	if cb.windowCounts.ConcurrencyInFlight > 0 {
		cb.windowCounts.ConcurrencyInFlight--
	}

	if now.Sub(cb.lastIntervalStart) >= cb.settings.Interval {
		cb.windowCounts = Counts{}
		cb.lastIntervalStart = now
		cb.failures = 0
		cb.successes = 0
		cb.requests = 0
	}

	if cb.settings.IsSuccessful(result) {
		cb.successes++
		cb.windowCounts.Successes++
	} else {
		cb.failures++
		cb.windowCounts.Failures++
	}

	cb.evaluateState()
}

func (cb *CircuitBreaker) evaluateState() {
	switch cb.state {
	case StateClosed:
		if cb.settings.ReadyToTrip(cb.windowCounts) {
			cb.setState(StateOpen)
		}
	case StateOpen:
		if time.Since(cb.lastStateChange) >= cb.settings.Timeout {
			cb.setState(StateHalfOpen)
		}
	case StateHalfOpen:
		if cb.requests >= int(cb.settings.MaxRequests) {
			if cb.failures > 0 {
				cb.setState(StateOpen)
			} else {
				cb.setState(StateClosed)
			}
		}
	}
}

func (cb *CircuitBreaker) setState(state CircuitState) {
	if cb.state == state {
		return
	}

	cb.lastStateChange = time.Now()
	cb.state = state

	switch state {
	case StateClosed:
		cb.failures = 0
		cb.successes = 0
		cb.requests = 0
		cb.windowCounts = Counts{}
	case StateOpen:
		cb.requests = 0
	case StateHalfOpen:
		cb.requests = 0
	}
}

func (cb *CircuitBreaker) State() CircuitState {
	cb.mu.RLock()
	defer cb.mu.RUnlock()
	return cb.state
}

func (cb *CircuitBreaker) Counts() Counts {
	cb.mu.RLock()
	defer cb.mu.RUnlock()
	return Counts{
		Requests:  cb.requests,
		Successes: cb.successes,
		Failures:  cb.failures,
	}
}
