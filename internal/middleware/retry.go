package middleware

import (
	"context"
	"errors"
	"fmt"
	"time"
)

type RetryPolicy struct {
	MaxRetries        int
	InitialDelay      time.Duration
	MaxDelay          time.Duration
	BackoffMultiplier float64
	RetryableErrors   []error
}

func DefaultRetryPolicy() RetryPolicy {
	return RetryPolicy{
		MaxRetries:        3,
		InitialDelay:      1 * time.Second,
		MaxDelay:          10 * time.Second,
		BackoffMultiplier: 2.0,
	}
}

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

type RetryableError struct {
	Err error
}

func (e *RetryableError) Error() string {
	return e.Err.Error()
}

func (e *RetryableError) Unwrap() error {
	return e.Err
}

func NewRetryableError(err error) error {
	return &RetryableError{Err: err}
}

func IsRetryableError(err error) bool {
	var retryable *RetryableError
	return errors.As(err, &retryable)
}
