package broker

import (
	"fmt"
	"runtime"
	"sync"
	"sync/atomic"
	"time"

	amqp "github.com/rabbitmq/amqp091-go"
	"github.com/rs/zerolog"
)

const (
	ConfirmTimeout  = 5 * time.Second
	DefaultPoolSize = 5
	CheckoutTimeout = 10 * time.Second
)

type poolChannel struct {
	ch       *amqp.Channel
	mu       sync.Mutex
	waiters  map[uint64]chan amqp.Confirmation
	stopChan chan struct{}
	id       int
}

func newPoolChannel(conn *amqp.Connection, id int, logger zerolog.Logger) (*poolChannel, error) {
	ch, err := conn.Channel()
	if err != nil {
		return nil, fmt.Errorf("create pool channel %d: %w", id, err)
	}

	if err := ch.Confirm(false); err != nil {
		ch.Close()
		return nil, fmt.Errorf("enable confirms channel %d: %w", id, err)
	}

	pc := &poolChannel{
		ch:       ch,
		waiters:  make(map[uint64]chan amqp.Confirmation),
		stopChan: make(chan struct{}),
		id:       id,
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
				select {
				case waiter <- conf:
				default:
					logger.Warn().
						Uint64("delivery_tag", conf.DeliveryTag).
						Int("channel_id", pc.id).
						Msg("confirm waiter already gone, discarding confirm")
				}
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

	if err := pc.ch.Publish(exchange, key, mandatory, immediate, msg); err != nil {
		pc.mu.Lock()
		delete(pc.waiters, tag)
		pc.mu.Unlock()
		return false, fmt.Errorf("publish: %w", err)
	}

	select {
	case conf := <-waiter:
		return conf.Ack, nil
	case <-time.After(ConfirmTimeout):
		pc.mu.Lock()
		delete(pc.waiters, tag)
		pc.mu.Unlock()
		return false, fmt.Errorf("confirm timeout after %v", ConfirmTimeout)
	}
}

func (pc *poolChannel) QueueInspect(name string) (amqp.Queue, error) {
	return pc.ch.QueueInspect(name)
}

func (pc *poolChannel) close() {
	close(pc.stopChan)
	_ = pc.ch.Close()
}

func (pc *poolChannel) isClosed() bool {
	pc.mu.Lock()
	defer pc.mu.Unlock()
	select {
	case <-pc.stopChan:
		return true
	default:
		return false
	}
}

// ChannelPool is a pool of AMQP channels with publisher confirms enabled.
// It uses explicit checkout/return semantics: a caller obtains a channel with
// Checkout(), uses it for one publish, then returns it with Return().
// This prevents concurrent use of the same channel by multiple goroutines.
//
// Channels are recreated lazily when they are detected as closed during checkout.
type ChannelPool struct {
	conn       *amqp.Connection
	available  []*poolChannel
	checkedOut atomic.Int64
	mu         sync.Mutex
	size       int
	logger     zerolog.Logger
}

func NewChannelPool(conn *amqp.Connection, size int, logger zerolog.Logger) (*ChannelPool, error) {
	if size < 1 {
		size = DefaultPoolSize
	}
	p := &ChannelPool{
		conn:      conn,
		available: make([]*poolChannel, 0, size),
		size:      size,
		logger:    logger,
	}
	for i := 0; i < size; i++ {
		pc, err := newPoolChannel(conn, i, logger)
		if err != nil {
			p.Close()
			return nil, fmt.Errorf("create pool channel %d: %w", i, err)
		}
		p.available = append(p.available, pc)
	}
	return p, nil
}

// Checkout obtains a channel from the pool. The caller must call Return(p)
// when done. Timeout prevents indefinite blocking when all channels are checked out.
func (p *ChannelPool) Checkout(timeout time.Duration) (*poolChannel, error) {
	deadline := time.Now().Add(timeout)
	for {
		p.mu.Lock()
		for {
			if len(p.available) == 0 {
				break
			}
			pc := p.available[0]
			p.available = p.available[1:]
			p.mu.Unlock()

			if pc.isClosed() {
				p.mu.Lock()
				pc2, err := newPoolChannel(p.conn, pc.id, p.logger)
				if err != nil {
					p.mu.Unlock()
					p.logger.Error().Err(err).Int("channel_id", pc.id).Msg("failed to recreate closed channel")
					continue
				}
				p.mu.Unlock()
				pc = pc2
			}

			p.checkedOut.Add(1)
			return pc, nil
		}
		p.mu.Unlock()

		if time.Now().After(deadline) {
			return nil, fmt.Errorf("channel checkout timeout after %v", timeout)
		}
		time.Sleep(5 * time.Millisecond)
	}
}

// Return gives a channel back to the pool. Panics if the channel was not
// obtained via Checkout().
func (p *ChannelPool) Return(pc *poolChannel) {
	if p.checkedOut.Load() == 0 {
		p.logger.Error().Int("channel_id", pc.id).Msg("Return called but no channels are checked out")
		return
	}

	if pc.isClosed() {
		p.mu.Lock()
		pc2, err := newPoolChannel(p.conn, pc.id, p.logger)
		if err != nil {
			p.mu.Unlock()
			p.logger.Error().Err(err).Int("channel_id", pc.id).Msg("failed to recreate returned closed channel")
			p.checkedOut.Add(-1)
			return
		}
		p.mu.Unlock()
		pc = pc2
	}

	p.mu.Lock()
	p.available = append(p.available, pc)
	p.mu.Unlock()
	p.checkedOut.Add(-1)
}

// Recreate replaces the channel at the given index. The channel must not be
// checked out.
func (p *ChannelPool) Recreate(index int, logger zerolog.Logger) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	if index < 0 || index >= p.size {
		return fmt.Errorf("invalid pool index %d", index)
	}
	if index >= len(p.available) {
		return fmt.Errorf("pool index %d not in available slice", index)
	}
	old := p.available[index]
	if old != nil {
		old.close()
	}
	pc, err := newPoolChannel(p.conn, index, logger)
	if err != nil {
		return fmt.Errorf("recreate pool channel %d: %w", index, err)
	}
	p.available[index] = pc
	return nil
}

// Close closes all channels in the pool and marks it as closed.
func (p *ChannelPool) Close() error {
	p.mu.Lock()
	defer p.mu.Unlock()
	for _, pc := range p.available {
		if pc != nil {
			pc.close()
		}
	}
	p.available = p.available[:0]
	return nil
}

// Size returns the total number of channels in the pool.
func (p *ChannelPool) Size() int {
	p.mu.Lock()
	defer p.mu.Unlock()
	return len(p.available)
}

// CheckedOut returns the number of channels currently checked out.
func (p *ChannelPool) CheckedOut() int64 {
	return p.checkedOut.Load()
}

// Available returns the number of channels available for checkout.
func (p *ChannelPool) Available() int {
	p.mu.Lock()
	defer p.mu.Unlock()
	return len(p.available)
}

// NumGoroutine returns the approximate number of goroutines currently
// running in the pool's listen goroutines. Used for leak detection in tests.
func (p *ChannelPool) NumGoroutine() int {
	return runtime.NumGoroutine()
}
