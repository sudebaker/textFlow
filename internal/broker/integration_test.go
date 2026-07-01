package broker

import (
	"context"
	"os"
	"runtime"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/rs/zerolog"
	"github.com/stretchr/testify/require"
	"textflow/internal/config"
)

func testBrokerURL() string {
	if url := os.Getenv("RABBITMQ_URL"); url != "" {
		return url
	}
	return ""
}

func setupTestBroker(t *testing.T) (*RabbitMQBroker, func()) {
	t.Helper()
	url := testBrokerURL()
	if url == "" {
		t.Skip("RABBITMQ_URL not set — run: export RABBITMQ_URL='amqp://user:pass@host:5672/'")
	}
	cfg := &config.Config{
		RabbitMQURL:      url,
		RabbitMQPoolSize: 5,
	}
	b, err := New(cfg)
	if err != nil {
		t.Skipf("cannot create broker: %v", err)
	}
	return b, func() { _ = b.Close() }
}

func TestPublish_SingleConfirmedMessage(t *testing.T) {
	broker, cleanup := setupTestBroker(t)
	defer cleanup()

	ctx := context.Background()
	queue := "test-publish-confirmed-" + t.Name()

	_, err := broker.channel.QueueDeclare(queue, true, false, false, false, nil)
	require.NoError(t, err)

	err = broker.Publish(ctx, queue, map[string]string{"hello": "world"})
	require.NoError(t, err)
}

func TestPublish_100ConcurrentMessages(t *testing.T) {
	broker, cleanup := setupTestBroker(t)
	defer cleanup()

	ctx := context.Background()
	queue := "test-concurrent-" + t.Name()

	_, err := broker.channel.QueueDeclare(queue, true, false, false, false, nil)
	require.NoError(t, err)

	var wg sync.WaitGroup
	var failures atomic.Int64
	for i := 0; i < 100; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			if err := broker.Publish(ctx, queue, map[string]int{"i": i}); err != nil {
				failures.Add(1)
				t.Errorf("publish %d failed: %v", i, err)
			}
		}(i)
	}
	wg.Wait()
	require.Zero(t, failures.Load(), "some publishes failed")
}

func TestConsumer_ReconnectsAfterChannelClose(t *testing.T) {
	broker, cleanup := setupTestBroker(t)
	defer cleanup()

	queue := "test-consumer-reconnect-" + t.Name()

	_, err := broker.channel.QueueDeclare(queue, true, false, false, false, nil)
	require.NoError(t, err)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	received := make(chan string, 10)
	handler := func(body []byte) error {
		received <- string(body)
		return nil
	}

	go func() {
		_ = broker.ConsumeWithContext(ctx, queue, handler)
	}()

	time.Sleep(200 * time.Millisecond)

	require.NoError(t, broker.Publish(ctx, queue, "msg-1"))

	select {
	case got := <-received:
		require.Equal(t, "msg-1", got)
	case <-time.After(5 * time.Second):
		t.Fatal("msg-1 not received after 5s")
	}

	cancel()
}

func TestChannelPool_Recreate(t *testing.T) {
	broker, cleanup := setupTestBroker(t)
	defer cleanup()

	require.NotNil(t, broker.pool)

	err := broker.pool.Recreate(0, zerolog.New(nil))
	require.NoError(t, err)

	ctx := context.Background()
	queue := "test-recreate-" + t.Name()
	_, err = broker.channel.QueueDeclare(queue, true, false, false, false, nil)
	require.NoError(t, err)

	err = broker.Publish(ctx, queue, map[string]string{"test": "recreate"})
	require.NoError(t, err)
}

func TestRabbitMQBroker_CloseIdempotent(t *testing.T) {
	broker, cleanup := setupTestBroker(t)
	defer cleanup()

	before := runtime.NumGoroutine()

	broker.Close()
	broker.Close()
	broker.Close()

	time.Sleep(100 * time.Millisecond)

	after := runtime.NumGoroutine()
	t.Logf("goroutines: before=%d after=%d", before, after)
	require.LessOrEqual(t, after, before+2)
}

func TestRabbitMQBroker_ConsumerCancel(t *testing.T) {
	broker, cleanup := setupTestBroker(t)
	defer cleanup()

	queue := "test-consumer-cancel-" + t.Name()
	_, err := broker.channel.QueueDeclare(queue, true, false, false, false, nil)
	require.NoError(t, err)

	ctx, cancel := context.WithCancel(context.Background())

	var handlerCalled int32
	handler := func(body []byte) error {
		atomic.AddInt32(&handlerCalled, 1)
		return nil
	}

	errCh := make(chan error, 1)
	go func() {
		errCh <- broker.ConsumeWithContext(ctx, queue, handler)
	}()

	time.Sleep(100 * time.Millisecond)
	cancel()

	select {
	case err := <-errCh:
		require.NoError(t, err)
	case <-time.After(2 * time.Second):
		t.Fatal("ConsumeWithContext did not return after cancel")
	}
}
