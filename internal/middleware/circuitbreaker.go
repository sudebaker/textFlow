// Package middleware provides HTTP middleware components for the orchestrator.
package middleware

import (
	"context"
	"errors"
	"sync"
	"time"
)

// CircuitState represents the state of the circuit breaker in the state machine:
// Closed → Open → HalfOpen → Closed.
type CircuitState int

const (
	// StateClosed is the initial state where requests proceed normally.
	// When too many failures occur (determined by ReadyToTrip), the circuit transitions to StateOpen.
	StateClosed CircuitState = iota
	// StateHalfOpen is an intermediate state that allows a limited number of test requests
	// to verify if the service has recovered. If all test requests succeed, the circuit
	// returns to StateClosed. If any fail, it returns to StateOpen.
	StateHalfOpen
	// StateOpen blocks all requests immediately, returning ErrCircuitOpen to fail fast
	// and prevent cascade failures. After Timeout duration elapses, the circuit transitions
	// to StateHalfOpen to test recovery.
	StateOpen
)

// String returns a human-readable representation of the circuit state.
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
	// ErrCircuitOpen is returned when a request is made while the circuit breaker
	// is in the Open state. This prevents cascading failures by failing fast and
	// allowing the remote service time to recover.
	ErrCircuitOpen = errors.New("circuit breaker is open")
	// ErrCircuitTooMany is returned when more than MaxRequests probe requests
	// are attempted while the circuit is in the HalfOpen state.
	ErrCircuitTooMany = errors.New("too many requests while circuit is half-open")
)

// Counts holds metrics collected within the current time interval.
// These counts are used by ReadyToTrip to determine if the circuit should open.
type Counts struct {
	// Requests is the total number of requests in the current interval.
	Requests int
	// Successes is the number of successful requests in the current interval.
	Successes int
	// Failures is the number of failed requests in the current interval.
	Failures int
	// Timeouts is the number of requests that exceeded the Timeout duration.
	Timeouts int
	// ContextCancelled is the number of requests cancelled via context cancellation.
	ContextCancelled int
	// ConcurrencyInFlight is the number of requests currently in flight.
	ConcurrencyInFlight int
}

// Settings configures the behavior of the CircuitBreaker.
type Settings struct {
	// Name is a human-readable identifier for this circuit breaker (used in logging/metrics).
	Name string
	// MaxRequests is the maximum number of probe requests allowed while the circuit
	// is in HalfOpen state (default: 3).
	MaxRequests uint32
	// Interval is the duration of the rolling window for counting successes and failures.
	// The window resets after this duration elapses (default: 30 seconds).
	Interval time.Duration
	// Timeout is the duration to wait in the Open state before attempting recovery
	// by transitioning to HalfOpen (default: 10 seconds).
	Timeout time.Duration
	// ReadyToTrip returns true if the circuit should transition from Closed to Open
	// based on the current window counts. If nil, defaultReadyToTrip is used (requires
	// ≥3 requests with ≥60% failure rate).
	ReadyToTrip func(counts Counts) bool
	// IsSuccessful returns true if the error indicates a successful request.
	// If nil, defaultIsSuccessful is used (returns true only if err == nil).
	IsSuccessful func(err error) bool
}

// CircuitBreaker implements the circuit breaker resilience pattern to prevent cascade
// failures when calling potentially failing services. It transitions between three states:
//   - Closed: Requests proceed normally; failures are counted
//   - Open: Requests fail immediately with ErrCircuitOpen; allows service recovery time
//   - HalfOpen: A limited number of test requests are allowed to verify recovery
//
// The circuit breaker protects against thundering herd problems by fast-failing when
// a service is degraded, then gradually allowing traffic to resume.
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

// NewCircuitBreaker creates a new CircuitBreaker with the provided settings.
// Unset fields are populated with sensible defaults:
//   - MaxRequests: 3 probe requests in HalfOpen state
//   - Interval: 30 seconds for the measurement window
//   - Timeout: 10 seconds before attempting recovery from Open state
//   - ReadyToTrip: fails if ≥3 requests with ≥60% failure rate
//   - IsSuccessful: only nil errors are considered successful
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

// defaultReadyToTrip opens the circuit if at least 3 requests have been made
// and at least 60% of them have failed.
func defaultReadyToTrip(counts Counts) bool {
	failureRatio := float64(counts.Failures) / float64(counts.Requests)
	return counts.Requests >= 3 && failureRatio >= 0.6
}

// defaultIsSuccessful considers only nil errors as successful.
func defaultIsSuccessful(err error) bool {
	return err == nil
}

// Execute runs the provided function through the circuit breaker.
// It enforces the circuit breaker state machine:
//
// When the circuit is Closed: the function executes normally; failures are counted.
// When the circuit is Open: returns ErrCircuitOpen immediately without executing fn.
// When the circuit is HalfOpen: allows up to MaxRequests test executions; resets to
// Closed if all succeed, or back to Open if any fail.
//
// Execute returns:
//   - nil if fn() returned nil and IsSuccessful(nil) == true
//   - fn's error if fn executed (or any other error from fn)
//   - ErrCircuitOpen if the circuit is in the Open state
//   - ErrCircuitTooMany if HalfOpen and more than MaxRequests probes attempted
//   - ctx.Err() if the context is cancelled during execution
func (cb *CircuitBreaker) Execute(ctx context.Context, fn func() error) error {
	if err := cb.beforeRequest(); err != nil {
		return err
	}

	result := cb.executeRequest(ctx, fn)
	cb.afterRequest(result)
	return result
}

// beforeRequest checks if the circuit allows a new request and updates internal state.
// It returns ErrCircuitOpen if the circuit is open, ErrCircuitTooMany if too many
// probes are in flight during HalfOpen state, or nil if the request should proceed.
func (cb *CircuitBreaker) beforeRequest() error {
	cb.mu.Lock()
	defer cb.mu.Unlock()

	switch cb.state {
	case StateOpen:
		return ErrCircuitOpen
	case StateHalfOpen:
		cb.requests++
		if cb.requests > int(cb.settings.MaxRequests) {
			return ErrCircuitTooMany
		}
	case StateClosed:
		cb.requests++
		cb.windowCounts.Requests++
	}

	return nil
}

// executeRequest runs fn with timeout and panic recovery.
// The function executes in a goroutine; if ctx is cancelled before fn completes,
// the context error is returned. Panics are recovered and converted to an error.
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

// afterRequest processes the result of an executed request and updates circuit state.
// It tracks failure/success counts, resets the window when Interval elapses, and
// calls evaluateState to potentially transition the circuit to a new state.
func (cb *CircuitBreaker) afterRequest(result error) {
	cb.mu.Lock()
	defer cb.mu.Unlock()

	now := time.Now()

	if cb.windowCounts.ConcurrencyInFlight > 0 {
		cb.windowCounts.ConcurrencyInFlight--
	}

	// Reset counts if interval window has expired
	if now.Sub(cb.lastIntervalStart) >= cb.settings.Interval {
		cb.windowCounts = Counts{}
		cb.lastIntervalStart = now
		cb.failures = 0
		cb.successes = 0
		cb.requests = 0
	}

	// Classify result as success or failure
	if cb.settings.IsSuccessful(result) {
		cb.successes++
		cb.windowCounts.Successes++
	} else {
		cb.failures++
		cb.windowCounts.Failures++
	}

	cb.evaluateState()
}

// evaluateState determines if a state transition should occur based on current metrics.
//
// State transitions:
//   - Closed → Open: when ReadyToTrip(windowCounts) returns true
//   - Open → HalfOpen: when Timeout duration has elapsed since the last state change
//   - HalfOpen → Closed: when MaxRequests probes complete with zero failures
//   - HalfOpen → Open: when MaxRequests probes complete with at least one failure
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

// setState transitions the circuit to a new state and resets relevant counters.
// It records the time of the state change for Timeout calculation during Open state.
func (cb *CircuitBreaker) setState(state CircuitState) {
	if cb.state == state {
		return
	}

	cb.lastStateChange = time.Now()
	cb.state = state

	switch state {
	case StateClosed:
		// Full reset on successful recovery
		cb.failures = 0
		cb.successes = 0
		cb.requests = 0
		cb.windowCounts = Counts{}
	case StateOpen:
		// Clear probe counter on opening
		cb.requests = 0
	case StateHalfOpen:
		// Reset probe counter for new probe phase
		cb.requests = 0
	}
}

// State returns the current state of the circuit breaker.
// This is safe to call concurrently.
func (cb *CircuitBreaker) State() CircuitState {
	cb.mu.RLock()
	defer cb.mu.RUnlock()
	return cb.state
}

// Counts returns a snapshot of the current request metrics.
// This is safe to call concurrently.
func (cb *CircuitBreaker) Counts() Counts {
	cb.mu.RLock()
	defer cb.mu.RUnlock()
	return Counts{
		Requests:  cb.requests,
		Successes: cb.successes,
		Failures:  cb.failures,
	}
}
