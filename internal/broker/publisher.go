package broker

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	amqp "github.com/rabbitmq/amqp091-go"
	"github.com/rs/zerolog"
	"textflow/pkg/metrics"
)

type publisher struct {
	pool   *ChannelPool
	logger zerolog.Logger
}

func newPublisher(pool *ChannelPool, logger zerolog.Logger) *publisher {
	return &publisher{pool: pool, logger: logger}
}

func (p *publisher) publish(ctx context.Context, queue string, body []byte) error {
	select {
	case <-ctx.Done():
		return fmt.Errorf("context cancelled before publish: %w", ctx.Err())
	default:
	}

	pc, err := p.pool.Checkout(CheckoutTimeout)
	if err != nil {
		return fmt.Errorf("checkout channel for %s: %w", queue, err)
	}
	defer p.pool.Return(pc)

	ack, err := pc.PublishWithConfirm(
		"",    // exchange
		queue, // routing key
		false, // mandatory
		false, // immediate
		amqp.Publishing{
			ContentType:  "application/json",
			Body:         body,
			DeliveryMode: amqp.Persistent,
			Timestamp:    time.Now(),
		},
	)
	if err != nil {
		metrics.RabbitMQErrors.Inc()
		// Check if this is a queue overflow error
		if IsQueueOverflowError(err) {
			metrics.QueueOverflowTotal.WithLabelValues(queue).Inc()
			return &QueueOverflowError{Queue: queue, Err: err}
		}
		return fmt.Errorf("publish to %s: %w", queue, err)
	}
	if !ack {
		metrics.RabbitMQErrors.Inc()
		return fmt.Errorf("publish to %s nacked by broker", queue)
	}

	metrics.QueuePublishTotal.WithLabelValues(queue).Inc()
	p.logger.Debug().Str("queue", queue).Msg("message published with confirm ack")
	return nil
}

func (p *publisher) publishJSON(ctx context.Context, queue string, message interface{}) error {
	body, err := json.Marshal(message)
	if err != nil {
		return fmt.Errorf("marshal message for %s: %w", queue, err)
	}
	return p.publish(ctx, queue, body)
}
