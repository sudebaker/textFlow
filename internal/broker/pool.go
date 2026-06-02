package broker

import (
	"fmt"
	"sync"
	"time"

	amqp "github.com/rabbitmq/amqp091-go"
	"github.com/rs/zerolog"
)

const (
	ConfirmTimeout = 5 * time.Second
	DefaultPoolSize = 5
)

type poolChannel struct {
	ch       *amqp.Channel
	mu       sync.Mutex
	waiters  map[uint64]chan amqp.Confirmation
	stopChan chan struct{}
}

func newPoolChannel(conn *amqp.Connection, logger zerolog.Logger) (*poolChannel, error) {
	ch, err := conn.Channel()
	if err != nil {
		return nil, fmt.Errorf("failed to create pool channel: %w", err)
	}

	if err := ch.Confirm(false); err != nil {
		ch.Close()
		return nil, fmt.Errorf("failed to enable publisher confirms: %w", err)
	}

	pc := &poolChannel{
		ch:       ch,
		waiters:  make(map[uint64]chan amqp.Confirmation),
		stopChan: make(chan struct{}),
	}

	confirms := ch.NotifyPublish(make(chan amqp.Confirmation, 256))
	go pc.listen(confirms, logger)

	return pc, nil
}

func (pc *poolChannel) listen(confirms <-chan amqp.Confirmation, logger zerolog.Logger) {
	for {
		select {
		case <-pc.stopChan:
			return
		case conf, ok := <-confirms:
			if !ok {
				return
			}
			pc.mu.Lock()
			waiter, exists := pc.waiters[conf.DeliveryTag]
			if exists {
				delete(pc.waiters, conf.DeliveryTag)
			}
			pc.mu.Unlock()
			if exists {
				waiter <- conf
			}
		}
	}
}

func (pc *poolChannel) PublishWithConfirm(exchange, key string, mandatory, immediate bool, msg amqp.Publishing) (bool, error) {
	pc.mu.Lock()
	tag := pc.ch.GetNextPublishSeqNo()
	waiter := make(chan amqp.Confirmation, 1)
	pc.waiters[tag] = waiter
	pc.mu.Unlock()

	err := pc.ch.Publish(exchange, key, mandatory, immediate, msg)
	if err != nil {
		pc.mu.Lock()
		delete(pc.waiters, tag)
		pc.mu.Unlock()
		return false, err
	}

	select {
	case conf := <-waiter:
		return conf.Ack, nil
	case <-time.After(ConfirmTimeout):
		pc.mu.Lock()
		delete(pc.waiters, tag)
		pc.mu.Unlock()
		return false, fmt.Errorf("publish confirm timeout after %v", ConfirmTimeout)
	}
}

func (pc *poolChannel) QueueInspect(name string) (amqp.Queue, error) {
	return pc.ch.QueueInspect(name)
}

func (pc *poolChannel) close() {
	close(pc.stopChan)
	_ = pc.ch.Close()
}

// ChannelPool is a round-robin shared set of AMQP channels with publisher confirms enabled.
// Get() returns a channel pointer without checkout/return semantics — each channel
// is protected by its own mutex and can be used concurrently by multiple goroutines.
// This is NOT a connection pool; all channels share the same *amqp.Connection.
type ChannelPool struct {
	conn     *amqp.Connection
	channels []*poolChannel
	mu       sync.Mutex
	next     int
	size     int
}

func NewChannelPool(conn *amqp.Connection, size int, logger zerolog.Logger) (*ChannelPool, error) {
	if size < 1 {
		size = DefaultPoolSize
	}
	pool := &ChannelPool{
		conn:     conn,
		channels: make([]*poolChannel, 0, size),
		size:     size,
	}
	for i := 0; i < size; i++ {
		pc, err := newPoolChannel(conn, logger)
		if err != nil {
			pool.Close()
			return nil, fmt.Errorf("failed to create pool channel %d: %w", i, err)
		}
		pool.channels = append(pool.channels, pc)
	}
	return pool, nil
}

func (p *ChannelPool) Get() (*poolChannel, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if len(p.channels) == 0 {
		return nil, fmt.Errorf("channel pool is empty")
	}
	pc := p.channels[p.next]
	p.next = (p.next + 1) % len(p.channels)
	return pc, nil
}

func (p *ChannelPool) Recreate(index int, logger zerolog.Logger) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	if index < 0 || index >= len(p.channels) {
		return fmt.Errorf("invalid pool index %d", index)
	}
	if p.channels[index] != nil {
		p.channels[index].close()
	}
	pc, err := newPoolChannel(p.conn, logger)
	if err != nil {
		return fmt.Errorf("failed to recreate pool channel %d: %w", index, err)
	}
	p.channels[index] = pc
	return nil
}

func (p *ChannelPool) Close() error {
	p.mu.Lock()
	defer p.mu.Unlock()
	for _, pc := range p.channels {
		if pc != nil {
			pc.close()
		}
	}
	p.channels = p.channels[:0]
	return nil
}

func (p *ChannelPool) Size() int {
	p.mu.Lock()
	defer p.mu.Unlock()
	return len(p.channels)
}
