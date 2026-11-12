package broker

import (
	"context"
	"fmt"
	"time"
)

// ConsumeWithContext consumes a queue until the context is cancelled.
//
// A cancelled context is a clean shutdown, not a failure: it returns nil and
// logs at debug level. Any other error means the connection or the channel
// broke; the broker reconnects with a growing backoff and keeps consuming.
func (b *RabbitMQBroker) ConsumeWithContext(ctx context.Context, queue string, handler func([]byte) error) error {
	for {
		err := b.consumeOnce(ctx, queue, handler)
		if ctx.Err() != nil {
			b.logger.Debug().Str("queue", queue).Msg("consumer stopped by context")
			return nil
		}
		if err != nil {
			b.logger.Error().Err(err).Str("queue", queue).Msg("consumer stopped, attempting reconnect")
			b.reconnect()
			select {
			case <-ctx.Done():
				return nil
			case <-time.After(5 * time.Second):
			}
			continue
		}
	}
}

func (b *RabbitMQBroker) consumeOnce(ctx context.Context, queue string, handler func([]byte) error) error {
	b.mu.RLock()
	ch := b.channel
	b.mu.RUnlock()
	if ch == nil {
		return fmt.Errorf("channel is nil")
	}

	msgs, err := ch.Consume(
		queue,
		"",
		false,
		false,
		false,
		false,
		nil,
	)
	if err != nil {
		return fmt.Errorf("consume queue %s: %w", queue, err)
	}

	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case msg, ok := <-msgs:
			if !ok {
				return fmt.Errorf("message channel closed")
			}
			if err := handler(msg.Body); err != nil {
				b.logger.Error().Err(err).Str("queue", queue).Msg("handler error")
				_ = msg.Nack(false, false)
			} else {
				_ = msg.Ack(false)
			}
		}
	}
}
