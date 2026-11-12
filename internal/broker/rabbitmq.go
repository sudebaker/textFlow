package broker

import (
	"context"
	"fmt"
	"sync"
	"sync/atomic"
	"time"

	amqp "github.com/rabbitmq/amqp091-go"
	"github.com/rs/zerolog"
	"textflow/internal/config"
	"textflow/internal/models"
	"textflow/pkg/logging"
	"textflow/pkg/metrics"
)

const (
	MaxReconnectAttempts   = 10
	InitialBackoff         = 2 * time.Second
	MaxBackoff             = 60 * time.Second
	ChannelMonitorInterval = 5 * time.Second

	DelayedExchangeName = "document_processor_delayed"
)

type RabbitMQBroker struct {
	conn           *amqp.Connection
	channel        *amqp.Channel
	pool           *ChannelPool
	pub            *publisher
	config         *config.Config
	logger         zerolog.Logger
	mu             sync.RWMutex
	closedChan     <-chan *amqp.Error
	stopChan       chan struct{}
	closeOnce      sync.Once
	isReconnecting atomic.Bool
}

func New(cfg *config.Config) (*RabbitMQBroker, error) {
	logger := logging.GetLogger()

	var err error
	var conn *amqp.Connection
	var channel *amqp.Channel

	maxRetries := 3
	for i := 0; i < maxRetries; i++ {
		conn, err = amqp.Dial(cfg.RabbitMQURL)
		if err != nil {
			logger.Warn().Msgf("Failed to connect to RabbitMQ (attempt %d/%d): %v", i+1, maxRetries, err)
			time.Sleep(time.Duration(i+1) * time.Second)
			continue
		}
		break
	}

	if err != nil {
		return nil, fmt.Errorf("failed to connect to RabbitMQ after %d retries: %w", maxRetries, err)
	}

	channel, err = conn.Channel()
	if err != nil {
		conn.Close()
		return nil, fmt.Errorf("failed to open channel: %w", err)
	}

	broker := &RabbitMQBroker{
		conn:     conn,
		channel:  channel,
		config:   cfg,
		logger:   logger,
		stopChan: make(chan struct{}),
	}

	broker.startMonitoring()

	if err := broker.declareDLX(); err != nil {
		broker.Close()
		return nil, fmt.Errorf("failed to declare DLX: %w", err)
	}

	if err := broker.declareDelayedExchange(); err != nil {
		broker.logger.Warn().Err(err).Msg(
			"Failed to declare delayed exchange — plugin may not be enabled; " +
				"workers will fall back to blocking retry",
		)
		newCh, chErr := conn.Channel()
		if chErr != nil {
			broker.Close()
			return nil, fmt.Errorf("failed to reopen channel after delayed exchange error: %w", chErr)
		}
		broker.mu.Lock()
		broker.channel = newCh
		broker.mu.Unlock()
	}

	if err := broker.declareQueues(); err != nil {
		broker.Close()
		return nil, err
	}

	poolSize := cfg.RabbitMQPoolSize
	if poolSize < 1 {
		poolSize = DefaultPoolSize
	}

	pool, err := NewChannelPool(conn, poolSize, logger)
	if err != nil {
		broker.Close()
		return nil, fmt.Errorf("failed to create channel pool: %w", err)
	}
	broker.pool = pool
	broker.pub = newPublisher(pool, logger)

	return broker, nil
}

func (b *RabbitMQBroker) declareDLX() error {
	err := b.channel.ExchangeDeclare(
		"document_processor_dlx",
		"topic",
		true,
		false,
		false,
		false,
		nil,
	)
	if err != nil {
		return fmt.Errorf("failed to declare DLX exchange: %w", err)
	}
	b.logger.Info().Msg("Dead Letter Exchange declared: document_processor_dlx")

	_, err = b.channel.QueueDeclare(
		"dead_letters",
		true,
		false,
		false,
		false,
		nil,
	)
	if err != nil {
		return fmt.Errorf("failed to declare DLQ: %w", err)
	}
	b.logger.Info().Msg("Dead Letter Queue declared: dead_letters")

	err = b.channel.QueueBind(
		"dead_letters",
		"*_failed",
		"document_processor_dlx",
		false,
		nil,
	)
	if err != nil {
		return fmt.Errorf("failed to bind DLQ to DLX: %w", err)
	}
	b.logger.Info().Msg("Dead Letter Queue bound to DLX")

	return nil
}

func (b *RabbitMQBroker) declareDelayedExchange() error {
	err := b.channel.ExchangeDeclare(
		DelayedExchangeName,
		"x-delayed-message",
		true,
		false,
		false,
		false,
		amqp.Table{
			"x-delayed-type": "direct",
		},
	)
	if err != nil {
		return fmt.Errorf("failed to declare delayed exchange %q: %w", DelayedExchangeName, err)
	}
	b.logger.Info().Msgf("Delayed exchange declared: %s", DelayedExchangeName)

	queues := []string{
		b.config.ExtractQueue,
		b.config.EmbeddingsQueue,
		b.config.EntitiesQueue,
		b.config.MetadataQueue,
		b.config.InferencesQueue,
	}

	for _, q := range queues {
		if err := b.channel.QueueBind(q, q, DelayedExchangeName, false, nil); err != nil {
			return fmt.Errorf("failed to bind queue %q to delayed exchange: %w", q, err)
		}
		b.logger.Info().Msgf("Queue %s bound to delayed exchange", q)
	}

	return nil
}

func (b *RabbitMQBroker) declareQueues() error {
	queues := []string{
		b.config.ExtractQueue,
		b.config.EmbeddingsQueue,
		b.config.EntitiesQueue,
		b.config.MetadataQueue,
		b.config.InferencesQueue,
	}

	for _, queue := range queues {
		if err := b.declareQueue(queue); err != nil {
			return fmt.Errorf("failed to declare queue %s: %w", queue, err)
		}
		b.logger.Info().Msgf("Queue declared: %s", queue)
	}

	return nil
}

func (b *RabbitMQBroker) declareQueue(name string) error {
	args := amqp.Table{
		"x-dead-letter-exchange":    "document_processor_dlx",
		"x-dead-letter-routing-key": name + "_failed",
	}

	// Add queue length limit if configured (prevents unbounded growth)
	if b.config.QueueMaxLength > 0 {
		args["x-max-length"] = int64(b.config.QueueMaxLength)
		args["x-overflow"] = "reject-publish"
	}

	_, err := b.channel.QueueDeclare(
		name,
		true,
		false,
		false,
		false,
		args,
	)
	return err
}

func (b *RabbitMQBroker) Publish(ctx context.Context, queue string, message interface{}) error {
	// The publisher is rebuilt by reconnect(); between a connection loss and the
	// next successful reconnect it is nil. Publishing used to dereference it and
	// panic, turning a recoverable outage into a crash. Fail fast instead and let
	// the reconnect loop (or the caller's retry) do the recovering: blocking a
	// publish for seconds while holding the hot path helps nobody.
	b.mu.RLock()
	pub := b.pub
	b.mu.RUnlock()

	if pub == nil {
		return fmt.Errorf("publish to %s: broker not connected (publisher unavailable)", queue)
	}
	return pub.publishJSON(ctx, queue, message)
}

func (b *RabbitMQBroker) PublishJobMessage(ctx context.Context, jobMsg *models.JobMessage) error {
	if jobMsg.DocumentBase64 == "" && jobMsg.DocumentURL == "" {
		return fmt.Errorf("invalid job message: no document provided")
	}

	// Stamp queue entry time so consumers can report stage_queue_time.
	if jobMsg.QueuedAt == 0 {
		jobMsg.QueuedAt = time.Now().UnixMilli()
	}

	queue := b.config.ExtractQueue
	return b.Publish(ctx, queue, jobMsg)
}

func (b *RabbitMQBroker) Consume(queue string, handler func([]byte) error) error {
	return b.ConsumeWithContext(context.Background(), queue, handler)
}

// GetQueueInfo reports how many messages and consumers a queue has.
//
// It must answer quickly: admission control calls it on every ingestion, and a
// request that blocks for minutes while the broker is down is worse than an
// error that lets the caller decide. So there is at most one reconnect attempt,
// without the backoff loop that the background monitor uses.
func (b *RabbitMQBroker) GetQueueInfo(queue string) (*QueueInfo, error) {
	pool := b.currentPool()
	if pool == nil {
		if err := b.tryReconnect(); err != nil {
			return nil, fmt.Errorf("get queue info for %s: broker not connected: %w", queue, err)
		}
		pool = b.currentPool()
		if pool == nil {
			return nil, fmt.Errorf("get queue info for %s: broker not connected", queue)
		}
	}

	pc, err := pool.Checkout(CheckoutTimeout)
	if err != nil {
		return nil, fmt.Errorf("checkout channel for queue %s: %w", queue, err)
	}

	queueInfo, err := pc.QueueInspect(queue)
	pool.Return(pc)
	if err != nil {
		return nil, fmt.Errorf("inspect queue %s: %w", queue, err)
	}

	return &QueueInfo{
		Name:      queueInfo.Name,
		Messages:  queueInfo.Messages,
		Consumers: queueInfo.Consumers,
	}, nil
}

func (b *RabbitMQBroker) currentPool() *ChannelPool {
	b.mu.RLock()
	defer b.mu.RUnlock()
	return b.pool
}

func (b *RabbitMQBroker) UpdateQueueMetrics() error {
	queues := []string{
		b.config.ExtractQueue,
		b.config.EmbeddingsQueue,
		b.config.EntitiesQueue,
		b.config.MetadataQueue,
		b.config.InferencesQueue,
	}

	for _, queue := range queues {
		info, err := b.GetQueueInfo(queue)
		if err != nil {
			b.logger.Warn().Err(err).Str("queue", queue).Msg("Failed to get queue info")
			continue
		}

		metrics.QueueDepth.WithLabelValues(queue).Set(float64(info.Messages))
	}

	return nil
}

type QueueInfo struct {
	Name      string `json:"name"`
	Messages  int    `json:"messages"`
	Consumers int    `json:"consumers"`
}

func (b *RabbitMQBroker) Close() error {
	b.closeOnce.Do(func() {
		close(b.stopChan)
	})

	b.mu.Lock()
	defer b.mu.Unlock()

	if b.pool != nil {
		b.pool.Close()
	}
	if b.channel != nil {
		b.channel.Close()
	}
	if b.conn != nil {
		b.conn.Close()
	}

	b.logger.Info().Msg("RabbitMQ connection closed")
	return nil
}

func (b *RabbitMQBroker) HealthCheck() error {
	b.mu.RLock()
	defer b.mu.RUnlock()

	if b.conn == nil || b.conn.IsClosed() {
		return fmt.Errorf("RabbitMQ connection is closed")
	}
	return nil
}

func (b *RabbitMQBroker) startMonitoring() {
	notifyClose := b.conn.NotifyClose(make(chan *amqp.Error))
	b.closedChan = notifyClose

	go func() {
		for {
			select {
			case <-b.stopChan:
				return
			case err, ok := <-notifyClose:
				if !ok {
					return
				}
				b.logger.Warn().Err(err).Msg("RabbitMQ connection closed detected, attempting to reconnect...")
				b.reconnect()
			}
		}
	}()
}
