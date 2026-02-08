package broker

import (
	"context"
	"encoding/json"
	"fmt"
	"sync"
	"time"

	"github.com/rs/zerolog"
	amqp "github.com/streadway/amqp"
	"ia-text-orchestrator/internal/config"
	"ia-text-orchestrator/internal/models"
	"ia-text-orchestrator/pkg/logging"
	"ia-text-orchestrator/pkg/metrics"
)

const (
	MaxReconnectAttempts   = 10
	InitialBackoff         = 2 * time.Second
	MaxBackoff             = 60 * time.Second
	ChannelMonitorInterval = 5 * time.Second
)

type RabbitMQBroker struct {
	conn           *amqp.Connection
	channel        *amqp.Channel
	config         *config.Config
	logger         zerolog.Logger
	mu             sync.RWMutex
	closedChan     <-chan *amqp.Error
	reconnectMutex sync.Mutex
	stopChan       chan struct{}
	isReconnecting bool
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

	if err := broker.declareQueues(); err != nil {
		broker.Close()
		return nil, err
	}

	return broker, nil
}

// declareDLX declares the Dead Letter Exchange and Dead Letter Queue
func (b *RabbitMQBroker) declareDLX() error {
	// 1. Declare Dead Letter Exchange
	err := b.channel.ExchangeDeclare(
		"document_processor_dlx", // name
		"topic",                  // type
		true,                     // durable
		false,                    // auto-delete
		false,                    // internal
		false,                    // no-wait
		nil,                      // arguments
	)
	if err != nil {
		return fmt.Errorf("failed to declare DLX exchange: %w", err)
	}
	b.logger.Info().Msg("Dead Letter Exchange declared: document_processor_dlx")

	// 2. Declare Dead Letter Queue (where failed messages will go)
	_, err = b.channel.QueueDeclare(
		"dead_letters", // name
		true,           // durable
		false,          // delete when unused
		false,          // exclusive
		false,          // no-wait
		nil,            // arguments
	)
	if err != nil {
		return fmt.Errorf("failed to declare DLQ: %w", err)
	}
	b.logger.Info().Msg("Dead Letter Queue declared: dead_letters")

	// 3. Bind DLQ to DLX with wildcard routing key to catch all failed messages
	err = b.channel.QueueBind(
		"dead_letters",           // queue name
		"*_failed",               // routing key pattern
		"document_processor_dlx", // exchange
		false,                    // no-wait
		nil,                      // arguments
	)
	if err != nil {
		return fmt.Errorf("failed to bind DLQ to DLX: %w", err)
	}
	b.logger.Info().Msg("Dead Letter Queue bound to DLX")

	return nil
}

func (b *RabbitMQBroker) declareQueues() error {
	queues := []string{
		b.config.ExtractQueue,
		b.config.EmbeddingsQueue,
		b.config.EntitiesQueue,
		b.config.MetadataQueue,
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
	_, err := b.channel.QueueDeclare(
		name,
		true,  // durable
		false, // delete when unused
		false, // exclusive
		false, // no-wait
		amqp.Table{
			"x-dead-letter-exchange":    "document_processor_dlx",
			"x-dead-letter-routing-key": name + "_failed",
		},
	)
	return err
}

func (b *RabbitMQBroker) Publish(ctx context.Context, queue string, message interface{}) error {
	select {
	case <-ctx.Done():
		return fmt.Errorf("context cancelled before publish: %w", ctx.Err())
	default:
	}

	body, err := json.Marshal(message)
	if err != nil {
		return fmt.Errorf("failed to marshal message: %w", err)
	}

	for attempt := 0; attempt < 3; attempt++ {
		b.mu.RLock()
		if b.channel == nil {
			b.mu.RUnlock()
			b.logger.Warn().Msgf("Channel is nil, triggering reconnect (attempt %d/3)", attempt+1)
			b.reconnect()
			time.Sleep(time.Duration(attempt+1) * time.Second)
			continue
		}

		err = b.channel.Publish(
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
		b.mu.RUnlock()

		if err != nil {
			metrics.RabbitMQErrors.Inc()
			b.logger.Warn().Err(err).Msgf("Publish failed, attempting reconnection (attempt %d/3)", attempt+1)
			b.reconnect()
			time.Sleep(time.Duration(attempt+1) * time.Second)
			continue
		}

		metrics.QueuePublishTotal.WithLabelValues(queue).Inc()
		b.logger.Debug().Msgf("Message published to queue: %s", queue)
		return nil
	}

	metrics.RabbitMQErrors.Inc()
	return fmt.Errorf("failed to publish message to queue %s after 3 attempts", queue)
}

func (b *RabbitMQBroker) PublishJobMessage(ctx context.Context, jobMsg *models.JobMessage) error {
	if jobMsg.DocumentBase64 == "" && jobMsg.DocumentURL == "" {
		return fmt.Errorf("invalid job message: no document provided")
	}

	queue := b.config.ExtractQueue
	return b.Publish(ctx, queue, jobMsg)
}

// ConsumeWithContext starts consuming messages with context cancelation support
func (b *RabbitMQBroker) ConsumeWithContext(ctx context.Context, queue string, handler func([]byte) error) error {
	b.mu.RLock()
	if b.channel == nil {
		b.mu.RUnlock()
		return fmt.Errorf("channel is nil, cannot start consumer")
	}

	msgs, err := b.channel.Consume(
		queue,
		"",    // consumer tag
		false, // auto-ack
		false, // exclusive
		false, // no-local
		false, // no-wait
		nil,
	)
	b.mu.RUnlock()

	if err != nil {
		return fmt.Errorf("failed to start consuming queue %s: %w", queue, err)
	}

	go func() {
		defer func() {
			b.logger.Info().Str("queue", queue).Msg("Consumer stopped")
		}()

		for {
			select {
			case <-ctx.Done():
				b.logger.Info().Str("queue", queue).Msg("Context cancelled, stopping consumer")
				return

			case msg, ok := <-msgs:
				if !ok {
					b.logger.Warn().Str("queue", queue).Msg("Message channel closed, attempting to reconnect consumer...")
					b.reconnect()
					b.startConsumer(ctx, queue, handler)
					return
				}

				if err := handler(msg.Body); err != nil {
					b.logger.Error().Err(err).Str("queue", queue).Msg("Error processing message")
					msg.Nack(false, false)
				} else {
					msg.Ack(false)
				}
			}
		}
	}()

	return nil
}

func (b *RabbitMQBroker) startConsumer(ctx context.Context, queue string, handler func([]byte) error) {
	b.mu.RLock()
	if b.channel == nil {
		b.mu.RUnlock()
		b.logger.Error().Str("queue", queue).Msg("Cannot start consumer: channel is nil")
		return
	}

	msgs, err := b.channel.Consume(
		queue,
		"",    // consumer tag
		false, // auto-ack
		false, // exclusive
		false, // no-local
		false, // no-wait
		nil,
	)
	b.mu.RUnlock()

	if err != nil {
		b.logger.Error().Err(err).Str("queue", queue).Msg("Failed to restart consumer")
		return
	}

	go func() {
		defer func() {
			b.logger.Info().Str("queue", queue).Msg("Consumer restarted and stopped")
		}()

		for {
			select {
			case <-ctx.Done():
				return
			case msg, ok := <-msgs:
				if !ok {
					return
				}

				if err := handler(msg.Body); err != nil {
					b.logger.Error().Err(err).Str("queue", queue).Msg("Error processing message")
					msg.Nack(false, false)
				} else {
					msg.Ack(false)
				}
			}
		}
	}()
}

// Consume is deprecated, use ConsumeWithContext instead
// Maintained for backwards compatibility
func (b *RabbitMQBroker) Consume(queue string, handler func([]byte) error) error {
	return b.ConsumeWithContext(context.Background(), queue, handler)
}

func (b *RabbitMQBroker) GetQueueInfo(queue string) (*QueueInfo, error) {
	for attempt := 0; attempt < 3; attempt++ {
		b.mu.RLock()
		if b.channel == nil {
			b.mu.RUnlock()
			b.logger.Warn().Msgf("Channel is nil, triggering reconnect (attempt %d/3)", attempt+1)
			b.reconnect()
			time.Sleep(time.Duration(attempt+1) * time.Second)
			continue
		}

		queueInfo, err := b.channel.QueueInspect(queue)
		b.mu.RUnlock()

		if err != nil {
			b.logger.Warn().Err(err).Msgf("GetQueueInfo failed, attempting reconnection (attempt %d/3)", attempt+1)
			b.reconnect()
			time.Sleep(time.Duration(attempt+1) * time.Second)
			continue
		}

		return &QueueInfo{
			Name:      queueInfo.Name,
			Messages:  queueInfo.Messages,
			Consumers: queueInfo.Consumers,
		}, nil
	}

	return nil, fmt.Errorf("failed to get queue info for %s after 3 attempts", queue)
}

// UpdateQueueMetrics updates Prometheus metrics for all queues
func (b *RabbitMQBroker) UpdateQueueMetrics() error {
	queues := []string{
		b.config.ExtractQueue,
		b.config.EmbeddingsQueue,
		b.config.EntitiesQueue,
		b.config.MetadataQueue,
	}

	for _, queue := range queues {
		info, err := b.GetQueueInfo(queue)
		if err != nil {
			b.logger.Warn().Err(err).Str("queue", queue).Msg("Failed to get queue info")
			continue
		}

		// Update queue depth metric
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
	close(b.stopChan)

	b.mu.Lock()
	defer b.mu.Unlock()

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

func (b *RabbitMQBroker) reconnect() {
	b.reconnectMutex.Lock()
	if b.isReconnecting {
		b.reconnectMutex.Unlock()
		return
	}
	b.isReconnecting = true
	b.reconnectMutex.Unlock()

	backoff := InitialBackoff
	attempts := 0

	for attempts < MaxReconnectAttempts {
		select {
		case <-b.stopChan:
			b.isReconnecting = false
			return
		default:
		}

		b.mu.Lock()
		b.channel = nil
		b.mu.Unlock()

		time.Sleep(backoff)

		b.mu.Lock()
		conn, err := amqp.Dial(b.config.RabbitMQURL)
		if err != nil {
			b.mu.Unlock()
			b.logger.Warn().Err(err).Msgf("Reconnection attempt %d/%d failed", attempts+1, MaxReconnectAttempts)
			attempts++
			backoff = time.Duration(float64(backoff) * 1.5)
			if backoff > MaxBackoff {
				backoff = MaxBackoff
			}
			continue
		}

		channel, err := conn.Channel()
		if err != nil {
			conn.Close()
			b.mu.Unlock()
			b.logger.Warn().Err(err).Msgf("Failed to open channel on reconnection attempt %d/%d", attempts+1, MaxReconnectAttempts)
			attempts++
			backoff = time.Duration(float64(backoff) * 1.5)
			if backoff > MaxBackoff {
				backoff = MaxBackoff
			}
			continue
		}

		b.conn = conn
		b.channel = channel
		b.mu.Unlock()

		notifyClose := b.conn.NotifyClose(make(chan *amqp.Error))
		b.closedChan = notifyClose

		if err := b.redeclareQueues(); err != nil {
			b.logger.Error().Err(err).Msg("Failed to redeclare queues after reconnection")
			attempts++
			backoff = time.Duration(float64(backoff) * 1.5)
			if backoff > MaxBackoff {
				backoff = MaxBackoff
			}
			continue
		}

		metrics.RabbitMQReconnects.Inc()
		b.logger.Info().Msg("Successfully reconnected to RabbitMQ and redeclared queues")
		b.isReconnecting = false
		return
	}

	b.isReconnecting = false
	metrics.RabbitMQReconnectErrors.Inc()
	b.logger.Error().Msgf("Failed to reconnect after %d attempts", MaxReconnectAttempts)
}

func (b *RabbitMQBroker) redeclareQueues() error {
	if err := b.declareDLX(); err != nil {
		return fmt.Errorf("failed to redeclare DLX: %w", err)
	}
	if err := b.declareQueues(); err != nil {
		return fmt.Errorf("failed to redeclare queues: %w", err)
	}
	return nil
}
