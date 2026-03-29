package middleware_test

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"ia-text-orchestrator/internal/middleware"
)

var errTemporary = errors.New("temporary error")
var errPermanent = errors.New("permanent error")

// TestRetry_SucceedsOnFirstTry verifies that WithRetry returns nil immediately
// when fn succeeds on the first call.
func TestRetry_SucceedsOnFirstTry(t *testing.T) {
	calls := 0
	policy := middleware.RetryPolicy{
		MaxRetries:        3,
		InitialDelay:      1 * time.Millisecond,
		BackoffMultiplier: 2.0,
	}

	err := middleware.WithRetry(func() error {
		calls++
		return nil
	}, policy)

	require.NoError(t, err)
	assert.Equal(t, 1, calls, "fn should be called exactly once on success")
}

// TestRetry_RetriesOnFailure verifies that WithRetry retries the function after
// each failure and returns the wrapped error after exhausting all retries.
func TestRetry_RetriesOnFailure(t *testing.T) {
	calls := 0
	policy := middleware.RetryPolicy{
		MaxRetries:        3,
		InitialDelay:      1 * time.Millisecond,
		BackoffMultiplier: 1.0, // no growth — keep delay fixed at 1ms
	}

	err := middleware.WithRetry(func() error {
		calls++
		return errTemporary
	}, policy)

	require.Error(t, err)
	assert.Equal(t, 4, calls, "fn should be called 4 times (1 initial + 3 retries)")
	assert.ErrorIs(t, err, errTemporary, "the wrapped last error should be reachable via errors.Is")
}

// TestRetry_RespectsMaxRetries verifies that WithRetry stops after exactly
// MaxRetries retries, not fewer and not more.
func TestRetry_RespectsMaxRetries(t *testing.T) {
	calls := 0
	maxRetries := 2
	policy := middleware.RetryPolicy{
		MaxRetries:        maxRetries,
		InitialDelay:      1 * time.Millisecond,
		BackoffMultiplier: 1.0,
	}

	_ = middleware.WithRetry(func() error {
		calls++
		return errTemporary
	}, policy)

	assert.Equal(t, maxRetries+1, calls, "total calls should equal MaxRetries + 1")
}

// TestRetry_SucceedsAfterSomeFailures verifies that WithRetry returns nil if
// fn eventually succeeds before exhausting all retries.
func TestRetry_SucceedsAfterSomeFailures(t *testing.T) {
	calls := 0
	policy := middleware.RetryPolicy{
		MaxRetries:        5,
		InitialDelay:      1 * time.Millisecond,
		BackoffMultiplier: 1.0,
	}

	err := middleware.WithRetry(func() error {
		calls++
		if calls < 3 {
			return errTemporary
		}
		return nil
	}, policy)

	require.NoError(t, err)
	assert.Equal(t, 3, calls, "fn should succeed on the 3rd attempt")
}

// TestRetry_ExponentialBackoff verifies that delays grow exponentially between retries.
func TestRetry_ExponentialBackoff(t *testing.T) {
	const initialDelay = 10 * time.Millisecond
	const multiplier = 2.0
	const retries = 3

	calls := 0
	timestamps := make([]time.Time, 0, retries+1)

	policy := middleware.RetryPolicy{
		MaxRetries:        retries,
		InitialDelay:      initialDelay,
		BackoffMultiplier: multiplier,
		MaxDelay:          1 * time.Second,
	}

	_ = middleware.WithRetry(func() error {
		calls++
		timestamps = append(timestamps, time.Now())
		return errTemporary
	}, policy)

	require.Len(t, timestamps, retries+1)

	// Verify delays are roughly exponential (with 50% tolerance for CI jitter).
	expectedDelay := initialDelay
	for i := 1; i < len(timestamps); i++ {
		actual := timestamps[i].Sub(timestamps[i-1])
		// Allow significant tolerance (50%) due to scheduler jitter.
		minExpected := time.Duration(float64(expectedDelay) * 0.5)
		assert.GreaterOrEqual(t, actual, minExpected,
			"delay between attempt %d and %d should be at least %v (got %v)", i-1, i, minExpected, actual)
		expectedDelay = time.Duration(float64(expectedDelay) * multiplier)
	}
}

// TestRetry_MaxDelayCapsBehavior verifies that delays are capped at MaxDelay.
func TestRetry_MaxDelayCapsBehavior(t *testing.T) {
	const maxDelay = 20 * time.Millisecond
	const initialDelay = 10 * time.Millisecond

	calls := 0
	timestamps := make([]time.Time, 0, 4)

	policy := middleware.RetryPolicy{
		MaxRetries:        3,
		InitialDelay:      initialDelay,
		BackoffMultiplier: 100.0, // very high multiplier — should hit cap quickly
		MaxDelay:          maxDelay,
	}

	_ = middleware.WithRetry(func() error {
		calls++
		timestamps = append(timestamps, time.Now())
		return errTemporary
	}, policy)

	require.Len(t, timestamps, 4)

	// After the first retry the delay would be 1000ms (10ms * 100) but cap is 20ms.
	// So all delays after attempt 0 should be ≤ maxDelay * 3 (with tolerance).
	for i := 2; i < len(timestamps); i++ {
		actual := timestamps[i].Sub(timestamps[i-1])
		maxAllowed := maxDelay * 3 // generous tolerance
		assert.LessOrEqual(t, actual, maxAllowed,
			"delay between attempt %d and %d should be capped (got %v, max %v)", i-1, i, actual, maxAllowed)
	}
}

// TestRetry_NonRetryableErrorAbortsImmediately verifies that WithRetry stops
// immediately when an error is NOT in the RetryableErrors list.
func TestRetry_NonRetryableErrorAbortsImmediately(t *testing.T) {
	calls := 0
	policy := middleware.RetryPolicy{
		MaxRetries:      3,
		InitialDelay:    1 * time.Millisecond,
		RetryableErrors: []error{errTemporary}, // only errTemporary is retryable
	}

	err := middleware.WithRetry(func() error {
		calls++
		return errPermanent // not in RetryableErrors
	}, policy)

	require.Error(t, err)
	assert.Equal(t, 1, calls, "non-retryable error should abort immediately after first attempt")
	assert.ErrorIs(t, err, errPermanent)
}

// TestRetry_RetryableErrorFilterRetriesMatchingErrors verifies that WithRetry
// retries only errors matching the RetryableErrors list.
func TestRetry_RetryableErrorFilterRetriesMatchingErrors(t *testing.T) {
	calls := 0
	policy := middleware.RetryPolicy{
		MaxRetries:      3,
		InitialDelay:    1 * time.Millisecond,
		RetryableErrors: []error{errTemporary},
	}

	_ = middleware.WithRetry(func() error {
		calls++
		return errTemporary // is in RetryableErrors — should retry
	}, policy)

	assert.Equal(t, 4, calls, "retryable error should cause 4 total calls (1+3)")
}

// TestRetry_NilRetryableErrorsRetriesAll verifies that when RetryableErrors is nil,
// all errors trigger a retry.
func TestRetry_NilRetryableErrorsRetriesAll(t *testing.T) {
	calls := 0
	policy := middleware.RetryPolicy{
		MaxRetries:      2,
		InitialDelay:    1 * time.Millisecond,
		RetryableErrors: nil, // nil means all errors retried
	}

	_ = middleware.WithRetry(func() error {
		calls++
		return errPermanent
	}, policy)

	assert.Equal(t, 3, calls, "nil RetryableErrors should retry all errors")
}

// TestRetryContext_SucceedsOnFirstTry verifies WithRetryContext returns nil immediately
// when fn succeeds on the first call.
func TestRetryContext_SucceedsOnFirstTry(t *testing.T) {
	calls := 0
	policy := middleware.RetryPolicy{
		MaxRetries:        3,
		InitialDelay:      1 * time.Millisecond,
		BackoffMultiplier: 1.0,
	}

	err := middleware.WithRetryContext(context.Background(), func() error {
		calls++
		return nil
	}, policy)

	require.NoError(t, err)
	assert.Equal(t, 1, calls)
}

// TestRetryContext_RetriesOnFailure verifies WithRetryContext retries and wraps error.
func TestRetryContext_RetriesOnFailure(t *testing.T) {
	calls := 0
	policy := middleware.RetryPolicy{
		MaxRetries:        2,
		InitialDelay:      1 * time.Millisecond,
		BackoffMultiplier: 1.0,
	}

	err := middleware.WithRetryContext(context.Background(), func() error {
		calls++
		return errTemporary
	}, policy)

	require.Error(t, err)
	assert.Equal(t, 3, calls)
	assert.ErrorIs(t, err, errTemporary)
}

// TestRetryContext_CancelsDuringBackoff verifies WithRetryContext returns
// context.Canceled when context is cancelled during the backoff delay.
func TestRetryContext_CancelsDuringBackoff(t *testing.T) {
	calls := 0
	policy := middleware.RetryPolicy{
		MaxRetries:        10,
		InitialDelay:      500 * time.Millisecond, // long delay so we can cancel it
		BackoffMultiplier: 1.0,
	}

	ctx, cancel := context.WithCancel(context.Background())

	// Cancel after the first retry starts waiting.
	go func() {
		time.Sleep(50 * time.Millisecond)
		cancel()
	}()

	err := middleware.WithRetryContext(ctx, func() error {
		calls++
		return errTemporary
	}, policy)

	assert.ErrorIs(t, err, context.Canceled)
	assert.Equal(t, 1, calls, "should call fn once, then cancel during backoff")
}

// TestRetryContext_CancelledBeforeStart verifies WithRetryContext returns
// context.Canceled immediately if context is already cancelled.
func TestRetryContext_CancelledBeforeStart(t *testing.T) {
	calls := 0
	policy := middleware.RetryPolicy{
		MaxRetries:   3,
		InitialDelay: 1 * time.Millisecond,
	}

	ctx, cancel := context.WithCancel(context.Background())
	cancel() // cancel immediately before calling WithRetryContext

	err := middleware.WithRetryContext(ctx, func() error {
		calls++
		return errTemporary
	}, policy)

	assert.ErrorIs(t, err, context.Canceled)
	assert.Equal(t, 0, calls, "fn should not be called when context is already cancelled")
}

// TestRetryableError_IsRetryableError verifies NewRetryableError and IsRetryableError.
func TestRetryableError_IsRetryableError(t *testing.T) {
	baseErr := errors.New("base error")
	retryable := middleware.NewRetryableError(baseErr)

	assert.True(t, middleware.IsRetryableError(retryable))
	assert.False(t, middleware.IsRetryableError(baseErr))
	assert.ErrorIs(t, retryable, baseErr, "retryable error should unwrap to base error")
}

// TestRetryableError_RetryableErrorMessage verifies that RetryableError preserves
// the original error message.
func TestRetryableError_RetryableErrorMessage(t *testing.T) {
	baseErr := errors.New("something went wrong")
	retryable := middleware.NewRetryableError(baseErr)

	assert.Equal(t, "something went wrong", retryable.Error())
}

// TestRetry_DefaultPolicyValues verifies DefaultRetryPolicy returns sensible values.
func TestRetry_DefaultPolicyValues(t *testing.T) {
	policy := middleware.DefaultRetryPolicy()

	assert.Equal(t, 3, policy.MaxRetries)
	assert.Equal(t, 1*time.Second, policy.InitialDelay)
	assert.Equal(t, 10*time.Second, policy.MaxDelay)
	assert.Equal(t, 2.0, policy.BackoffMultiplier)
	assert.Nil(t, policy.RetryableErrors)
}
