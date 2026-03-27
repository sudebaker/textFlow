package middleware

import (
	"context"
	"errors"
	"fmt"
	"time"
)

// RetryPolicy configures exponential backoff retry behavior.
//
// The exponential backoff algorithm calculates delay as:
//
//	delay_n = initial_delay * (multiplier ^ n), capped at max_delay
//
// For example, with InitialDelay=1s and BackoffMultiplier=2.0:
//
//	attempt 0: no delay (first try)
//	attempt 1: 1s delay
//	attempt 2: 2s delay
//	attempt 3: 4s delay
//	attempt 4: 8s delay
//	etc.
//
// Note: MaxRetries determines the number of retries AFTER the initial attempt.
// MaxRetries=3 means 4 total attempts (1 initial + 3 retries).
//
// RetryableErrors controls which error types trigger a retry:
//   - If nil or empty: all errors are retried (except non-retryable signals)
//   - If populated: only errors matching the list via errors.Is() are retried
//
// Use IsRetryableError() to check if an error should be retried regardless of
// this policy's RetryableErrors list.
type RetryPolicy struct {
	// MaxRetries is the number of retries after the initial attempt (default: 3).
	MaxRetries int
	// InitialDelay is the wait time before the first retry (default: 1 second).
	InitialDelay time.Duration
	// MaxDelay caps the exponential backoff at this duration (default: 10 seconds).
	MaxDelay time.Duration
	// BackoffMultiplier is multiplied with delay after each retry (default: 2.0).
	BackoffMultiplier float64
	// RetryableErrors lists specific error types to retry on. If nil, all errors
	// are considered retryable. If populated, only errors matching via errors.Is()
	// are retried.
	RetryableErrors []error
}

// DefaultRetryPolicy returns a sensible default retry configuration.
//
// Defaults:
//   - MaxRetries: 3 (4 total attempts)
//   - InitialDelay: 1 second
//   - MaxDelay: 10 seconds
//   - BackoffMultiplier: 2.0 (exponential)
//   - RetryableErrors: nil (all errors retried)
func DefaultRetryPolicy() RetryPolicy {
	return RetryPolicy{
		MaxRetries:        3,
		InitialDelay:      1 * time.Second,
		MaxDelay:          10 * time.Second,
		BackoffMultiplier: 2.0,
	}
}

// WithRetry executes fn with exponential backoff retry logic, blocking until
// success or max retries exhausted.
//
// Unlike WithRetryContext, WithRetry does not respect context cancellation and
// uses time.Sleep for delays. Use this for non-cancellable operations or when
// you don't have a context available.
//
// Behavior:
//   - Calls fn() up to (MaxRetries + 1) times
//   - On success (nil error), returns nil immediately
//   - On non-retryable error, returns immediately
//   - On retryable error, waits delay then retries
//   - After MaxRetries exhausted, returns "after N retries: <wrapped error>"
//
// Retry determination:
//   - If RetryableErrors is nil/empty: all errors trigger retry
//   - If RetryableErrors populated: only errors.Is() matches trigger retry
//   - Use NewRetryableError() to always mark an error as retryable
//
// Example with defaults (MaxRetries=3, 1s→2s→4s→8s delays):
//
//	policy := middleware.DefaultRetryPolicy()
//	err := middleware.WithRetry(makeAPICall, policy)
//
// To retry only specific errors:
//
//	policy := RetryPolicy{MaxRetries: 2, RetryableErrors: []error{io.EOF}}
//	err := middleware.WithRetry(readFile, policy)
func WithRetry(fn func() error, policy RetryPolicy) error {
	if policy.InitialDelay == 0 {
		policy.InitialDelay = DefaultRetryPolicy().InitialDelay
	}
	if policy.MaxRetries == 0 {
		policy.MaxRetries = DefaultRetryPolicy().MaxRetries
	}
	if policy.BackoffMultiplier == 0 {
		policy.BackoffMultiplier = DefaultRetryPolicy().BackoffMultiplier
	}

	var lastErr error
	delay := policy.InitialDelay

	for attempt := 0; attempt <= policy.MaxRetries; attempt++ {
		err := fn()
		if err == nil {
			return nil
		}

		lastErr = err

		if !isRetryable(err, policy.RetryableErrors) {
			return err
		}

		if attempt < policy.MaxRetries {
			time.Sleep(delay)
			delay = time.Duration(float64(delay) * policy.BackoffMultiplier)
			if delay > policy.MaxDelay {
				delay = policy.MaxDelay
			}
		}
	}

	return fmt.Errorf("after %d retries: %w", policy.MaxRetries, lastErr)
}

// WithRetryContext executes fn with exponential backoff retry logic, respecting
// context cancellation via ctx.Done().
//
// Unlike WithRetry, WithRetryContext respects context deadlines and cancellation.
// If ctx is cancelled during fn() or during backoff delay, returns ctx.Err()
// immediately without further retries. Use this for all external I/O operations,
// HTTP requests, or when cancellation is required.
//
// Behavior:
//   - Calls fn() up to (MaxRetries + 1) times
//   - On success (nil error), returns nil immediately
//   - On non-retryable error, returns immediately
//   - On retryable error, waits delay then retries (respects ctx cancellation)
//   - If ctx cancelled at any point, returns ctx.Err() immediately
//   - After MaxRetries exhausted, returns "after N retries: <wrapped error>"
//
// Context cancellation handling:
//   - Checked at start of each attempt loop
//   - Checked during backoff delay with context-aware timer
//   - Stops timer and returns immediately on cancellation
//
// Example with HTTP request and timeout:
//
//	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
//	defer cancel()
//	policy := middleware.DefaultRetryPolicy()
//	err := middleware.WithRetryContext(ctx, makeHTTPRequest, policy)
//
// Comparison with WithRetry:
//
//	WithRetry:        Simple, blocking, no cancellation — use for background tasks
//	WithRetryContext: Cancellable, respects deadlines — use for I/O operations
func WithRetryContext(ctx context.Context, fn func() error, policy RetryPolicy) error {
	if policy.InitialDelay == 0 {
		policy.InitialDelay = DefaultRetryPolicy().InitialDelay
	}
	if policy.MaxRetries == 0 {
		policy.MaxRetries = DefaultRetryPolicy().MaxRetries
	}
	if policy.BackoffMultiplier == 0 {
		policy.BackoffMultiplier = DefaultRetryPolicy().BackoffMultiplier
	}

	var lastErr error
	delay := policy.InitialDelay

	for attempt := 0; attempt <= policy.MaxRetries; attempt++ {
		select {
		case <-ctx.Done():
			return ctx.Err()
		default:
		}

		err := fn()
		if err == nil {
			return nil
		}

		lastErr = err

		if !isRetryable(err, policy.RetryableErrors) {
			return err
		}

		if attempt < policy.MaxRetries {
			timer := time.NewTimer(delay)
			select {
			case <-ctx.Done():
				timer.Stop()
				return ctx.Err()
			case <-timer.C:
			}
			delay = time.Duration(float64(delay) * policy.BackoffMultiplier)
			if delay > policy.MaxDelay {
				delay = policy.MaxDelay
			}
		}
	}

	return fmt.Errorf("after %d retries: %w", policy.MaxRetries, lastErr)
}

// isRetryable checks whether err should be retried based on retryableErrors list.
// Returns true if retryableErrors is nil (all errors retryable) or if err
// matches one of the error types via errors.Is().
func isRetryable(err error, retryableErrors []error) bool {
	if retryableErrors == nil {
		return true
	}

	for _, retryable := range retryableErrors {
		if errors.Is(err, retryable) {
			return true
		}
	}

	return false
}

// RetryableError wraps an error to explicitly mark it as retryable, regardless
// of the RetryPolicy.RetryableErrors configuration.
//
// Use RetryableError when you want to force retry behavior for a specific error,
// bypassing the policy's RetryableErrors whitelist. This is useful for marking
// custom error types as always retryable.
//
// Example:
//
//	if isTemporaryFailure(err) {
//	    return middleware.NewRetryableError(err)
//	}
//
// IsRetryableError() can be used to detect this wrapper at any nesting level
// via errors.As().
type RetryableError struct {
	// Err is the wrapped error.
	Err error
}

// Error returns the error message of the wrapped error, implementing the error interface.
func (e *RetryableError) Error() string {
	return e.Err.Error()
}

// Unwrap returns the wrapped error, enabling error unwrapping via errors.As()
// and errors.Is().
func (e *RetryableError) Unwrap() error {
	return e.Err
}

// NewRetryableError wraps err in a RetryableError, marking it as explicitly
// retryable. The wrapped error can be retrieved via IsRetryableError() or
// standard errors.As() / errors.Unwrap().
//
// Use this to bypass RetryPolicy.RetryableErrors filtering when you want to
// force retry behavior for specific errors.
//
// Example:
//
//	err := someOperation()
//	if isSomeTransientError(err) {
//	    err = middleware.NewRetryableError(err)
//	}
//	return middleware.WithRetry(fn, policy)
func NewRetryableError(err error) error {
	return &RetryableError{Err: err}
}

// IsRetryableError reports whether err is a RetryableError, at any depth in
// the error chain. It uses errors.As() to find wrapped instances.
//
// Use this to check if an error was explicitly marked as retryable via
// NewRetryableError(), bypassing the RetryPolicy.RetryableErrors list.
//
// Example:
//
//	if err := operation(); err != nil {
//	    if middleware.IsRetryableError(err) {
//	        // This error was explicitly marked for retry
//	    }
//	}
func IsRetryableError(err error) bool {
	var retryable *RetryableError
	return errors.As(err, &retryable)
}
