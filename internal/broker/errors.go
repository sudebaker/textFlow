package broker

import (
	"fmt"
	"strings"

	amqp "github.com/rabbitmq/amqp091-go"
)

// QueueOverflowError is returned when a publish is rejected because the queue is full.
// This happens when x-max-length is set with overflow: reject-publish.
type QueueOverflowError struct {
	Queue string
	Err   error
}

func (e *QueueOverflowError) Error() string {
	return fmt.Sprintf("queue %s is full (x-max-length reached)", e.Queue)
}

func (e *QueueOverflowError) Unwrap() error {
	return e.Err
}

// IsQueueOverflowError checks if an error is due to queue overflow (x-max-length with reject-publish).
// When a queue has x-max-length with overflow: reject-publish, RabbitMQ closes the channel
// with a PRECONDITION_FAILED error. The amqp091-go library returns this as a *amqp.Error.
func IsQueueOverflowError(err error) bool {
	if err == nil {
		return false
	}

	// Check for our custom QueueOverflowError
	if _, ok := err.(*QueueOverflowError); ok {
		return true
	}

	// Check for amqp.Error with PRECONDITION_FAILED (406)
	var amqpErr *amqp.Error
	if asAMQP, ok := err.(*amqp.Error); ok {
		amqpErr = asAMQP
	}

	if amqpErr != nil {
		// Code 406 = PRECONDITION_FAILED
		if amqpErr.Code == 406 {
			return true
		}
		// Also check message for queue-related errors
		if strings.Contains(amqpErr.Reason, "max-length") || strings.Contains(amqpErr.Reason, "reject-publish") {
			return true
		}
	}

	// Fallback: check error message string
	errMsg := err.Error()
	return strings.Contains(errMsg, "PRECONDITION_FAILED") ||
		strings.Contains(errMsg, "max-length") ||
		strings.Contains(errMsg, "reject-publish")
}
