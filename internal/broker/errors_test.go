package broker

import (
	"errors"
	"testing"

	amqp "github.com/rabbitmq/amqp091-go"
)

func TestIsQueueOverflowError_QueueOverflowType(t *testing.T) {
	err := &QueueOverflowError{Queue: "extract_text", Err: errors.New("queue full")}
	if !IsQueueOverflowError(err) {
		t.Error("Expected IsQueueOverflowError to return true for QueueOverflowError")
	}
}

func TestIsQueueOverflowError_AMQPPreconditionFailed(t *testing.T) {
	err := &amqp.Error{
		Code:   406,
		Reason: "PRECONDITION_FAILED",
	}
	if !IsQueueOverflowError(err) {
		t.Error("Expected IsQueueOverflowError to return true for AMQP 406 PRECONDITION_FAILED")
	}
}

func TestIsQueueOverflowError_AMQPMaxLength(t *testing.T) {
	err := &amqp.Error{
		Code:   406,
		Reason: "max-length exceeded",
	}
	if !IsQueueOverflowError(err) {
		t.Error("Expected IsQueueOverflowError to return true for AMQP max-length error")
	}
}

func TestIsQueueOverflowError_AMQPRejectPublish(t *testing.T) {
	err := &amqp.Error{
		Code:   406,
		Reason: "reject-publish",
	}
	if !IsQueueOverflowError(err) {
		t.Error("Expected IsQueueOverflowError to return true for AMQP reject-publish error")
	}
}

func TestIsQueueOverflowError_NilError(t *testing.T) {
	if IsQueueOverflowError(nil) {
		t.Error("Expected IsQueueOverflowError to return false for nil error")
	}
}

func TestIsQueueOverflowError_UnrelatedError(t *testing.T) {
	err := errors.New("some other error")
	if IsQueueOverflowError(err) {
		t.Error("Expected IsQueueOverflowError to return false for unrelated error")
	}
}

func TestIsQueueOverflowError_ErrorMessageFallback(t *testing.T) {
	err := errors.New("channel closed: PRECONDITION_FAILED - max-length exceeded")
	if !IsQueueOverflowError(err) {
		t.Error("Expected IsQueueOverflowError to return true for error message containing PRECONDITION_FAILED and max-length")
	}
}

func TestQueueOverflowError_ErrorString(t *testing.T) {
	err := &QueueOverflowError{Queue: "extract_text", Err: errors.New("queue full")}
	expected := "queue extract_text is full (x-max-length reached)"
	if err.Error() != expected {
		t.Errorf("Expected error string %q, got %q", expected, err.Error())
	}
}

func TestQueueOverflowError_Unwrap(t *testing.T) {
	inner := errors.New("queue full")
	err := &QueueOverflowError{Queue: "extract_text", Err: inner}
	if !errors.Is(err, inner) {
		t.Error("Expected QueueOverflowError to unwrap to inner error")
	}
}
