package broker

import (
	"context"
	"fmt"
	"time"
)

func (b *RabbitMQBroker) ConsumeWithContext(ctx context.Context, queue string, handler func([]byte) error) error {
	for {
		if err := b.consumeOnce(ctx, queue, handler); err != nil {
			b.logger.Error().Err(err).Str("queue", queue).Msg("consumer stopped, attempting reconnect")
			select {
			case <-ctx.Done():
				return ctx.Err()
			default:
			}
			b.reconnect()
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(5 * time.Second):
			}
			continue
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		default:
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
