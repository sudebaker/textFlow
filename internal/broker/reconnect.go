package broker

import (
	"fmt"
	"time"

	amqp "github.com/rabbitmq/amqp091-go"
)

func (b *RabbitMQBroker) tryReconnect() error {
	b.mu.Lock()
	oldConn := b.conn
	oldPool := b.pool
	b.mu.Unlock()

	if oldPool != nil {
		_ = oldPool.Close()
	}
	if oldConn != nil {
		_ = oldConn.Close()
	}

	conn, err := amqp.Dial(b.config.RabbitMQURL)
	if err != nil {
		return fmt.Errorf("dial: %w", err)
	}

	ch, err := conn.Channel()
	if err != nil {
		conn.Close()
		return fmt.Errorf("open channel: %w", err)
	}

	b.mu.Lock()
	b.conn = conn
	b.channel = ch
	b.mu.Unlock()

	if err := b.declareTopology(); err != nil {
		return fmt.Errorf("declare topology: %w", err)
	}

	newPool, err := NewChannelPool(conn, b.config.RabbitMQPoolSize, b.logger)
	if err != nil {
		return fmt.Errorf("create pool: %w", err)
	}

	b.mu.Lock()
	b.pool = newPool
	if b.pub != nil {
		b.pub.pool = newPool
	}
	b.closedChan = conn.NotifyClose(make(chan *amqp.Error, 1))
	b.mu.Unlock()

	return nil
}

func (b *RabbitMQBroker) reconnect() {
	if !b.isReconnecting.CompareAndSwap(false, true) {
		b.logger.Info().Msg("reconnect already in progress")
		return
	}
	defer b.isReconnecting.Store(false)

	b.logger.Warn().Msg("RabbitMQ reconnect initiated")

	backoff := InitialBackoff
	for attempt := 1; attempt <= MaxReconnectAttempts; attempt++ {
		if err := b.tryReconnect(); err == nil {
			b.logger.Info().Int("attempt", attempt).Msg("RabbitMQ reconnected")
			return
		} else {
			b.logger.Warn().Err(err).Int("attempt", attempt).Msg("reconnect attempt failed")
		}
		select {
		case <-b.stopChan:
			return
		case <-time.After(backoff):
		}
		if backoff < MaxBackoff {
			backoff *= 2
		}
	}
}

func (b *RabbitMQBroker) declareTopology() error {
	if err := b.declareDLX(); err != nil {
		return fmt.Errorf("declare DLX: %w", err)
	}
	if err := b.declareDelayedExchange(); err != nil {
		b.logger.Warn().Err(err).Msg(
			"delayed exchange declaration failed after reconnect — plugin may not be enabled; " +
				"workers will fall back to blocking retry",
		)
		b.mu.Lock()
		conn := b.conn
		b.mu.Unlock()
		newCh, err := conn.Channel()
		if err != nil {
			return fmt.Errorf("reopen channel after delayed exchange error: %w", err)
		}
		b.mu.Lock()
		b.channel = newCh
		b.mu.Unlock()
	}
	if err := b.declareQueues(); err != nil {
		return fmt.Errorf("declare queues: %w", err)
	}
	return nil
}
