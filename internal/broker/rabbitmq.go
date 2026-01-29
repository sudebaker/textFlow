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

type RabbitMQBroker struct {
	conn    *amqp.Connection
	channel *amqp.Channel
	config  *config.Config
	logger  zerolog.Logger
	mu      sync.RWMutex
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
		conn:    conn,
		channel: channel,
		config:  cfg,
		logger:  logger,
	}

	// Declare DLX before queues (queues reference it)
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
	b.mu.Lock()
	defer b.mu.Unlock()

	body, err := json.Marshal(message)
	if err != nil {
		return fmt.Errorf("failed to marshal message: %w", err)
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

	if err != nil {
		return fmt.Errorf("failed to publish message to queue %s: %w", queue, err)
	}

	b.logger.Debug().Msgf("Message published to queue: %s", queue)
	return nil
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
	msgs, err := b.channel.Consume(
		queue,
		"",    // consumer tag
		false, // auto-ack
		false, // exclusive
		false, // no-local
		false, // no-wait
		nil,
	)
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
					b.logger.Warn().Str("queue", queue).Msg("Message channel closed")
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

// Consume is deprecated, use ConsumeWithContext instead
// Maintained for backwards compatibility
func (b *RabbitMQBroker) Consume(queue string, handler func([]byte) error) error {
	return b.ConsumeWithContext(context.Background(), queue, handler)
}

func (b *RabbitMQBroker) GetQueueInfo(queue string) (*QueueInfo, error) {
	queueInfo, err := b.channel.QueueInspect(queue)
	if err != nil {
		return nil, err
	}

	return &QueueInfo{
		Name:      queueInfo.Name,
		Messages:  queueInfo.Messages,
		Consumers: queueInfo.Consumers,
	}, nil
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
