package broker

import (
	"os"
	"runtime"
	"sync"
	"testing"
	"time"

	amqp "github.com/rabbitmq/amqp091-go"
	"github.com/rs/zerolog"
	"github.com/stretchr/testify/require"
)

func setupTestPoolWithLogger(t *testing.T) (*ChannelPool, *amqp.Connection, func()) {
	t.Helper()
	url := os.Getenv("RABBITMQ_URL")
	if url == "" {
		t.Skip("RABBITMQ_URL not set — run: export RABBITMQ_URL='amqp://user:pass@host:5672/'")
	}
	conn, err := amqp.Dial(url)
	if err != nil {
		t.Skipf("cannot connect to RabbitMQ at %s: %v", url, err)
	}
	logger := zerolog.New(nil)
	pool, err := NewChannelPool(conn, 3, logger)
	if err != nil {
		conn.Close()
		t.Fatalf("NewChannelPool: %v", err)
	}
	cleanup := func() {
		pool.Close()
		conn.Close()
	}
	return pool, conn, cleanup
}

func TestChannelPool_CheckoutAndReturn(t *testing.T) {
	pool, _, cleanup := setupTestPoolWithLogger(t)
	defer cleanup()

	pc1, err := pool.Checkout(2 * time.Second)
	require.NoError(t, err)
	require.NotNil(t, pc1)

	pc2, err := pool.Checkout(2 * time.Second)
	require.NoError(t, err)
	require.NotNil(t, pc2)

	require.NotSame(t, pc1, pc2, "checked out channels must be different")

	ack, err := pc1.PublishWithConfirm("", "test-checkout-1", false, false, amqp.Publishing{Body: []byte("msg1")})
	require.NoError(t, err)
	require.True(t, ack)

	pool.Return(pc1)

	pc3, err := pool.Checkout(2 * time.Second)
	require.NoError(t, err)
	require.Same(t, pc1, pc3, "returned channel should be the same instance")
}

func TestChannelPool_CheckoutTimeout(t *testing.T) {
	pool, _, cleanup := setupTestPoolWithLogger(t)
	defer cleanup()

	channels := make([]*poolChannel, 0, 3)
	for i := 0; i < 3; i++ {
		pc, err := pool.Checkout(2 * time.Second)
		require.NoError(t, err)
		channels = append(channels, pc)
	}

	_, err := pool.Checkout(100 * time.Millisecond)
	require.Error(t, err)
	require.Contains(t, err.Error(), "checkout timeout")

	for _, pc := range channels {
		pool.Return(pc)
	}
}

func TestChannelPool_ConcurrentCheckoutReturn(t *testing.T) {
	pool, _, cleanup := setupTestPoolWithLogger(t)
	defer cleanup()

	const n = 20
	var wg sync.WaitGroup
	var mu sync.Mutex
	returned := make([]*poolChannel, 0, n)

	for i := 0; i < n; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			pc, err := pool.Checkout(5 * time.Second)
			if err != nil {
				t.Errorf("Checkout failed: %v", err)
				return
			}
			time.Sleep(10 * time.Millisecond)
			pool.Return(pc)

			mu.Lock()
			returned = append(returned, pc)
			mu.Unlock()
		}()
	}

	wg.Wait()
	require.Len(t, returned, n)
}

func TestChannelPool_NoConfirmListenerLeak(t *testing.T) {
	pool, _, cleanup := setupTestPoolWithLogger(t)
	defer cleanup()

	before := runtime.NumGoroutine()

	for i := 0; i < 5; i++ {
		pc, err := pool.Checkout(2 * time.Second)
		require.NoError(t, err)
		pc.close()
		pool.Return(pc)
	}

	time.Sleep(200 * time.Millisecond)
	after := runtime.NumGoroutine()
	t.Logf("goroutines: before=%d after=%d", before, after)
	require.LessOrEqual(t, after, before+2, "goroutine leak detected")
}

func TestChannelPool_ReturnClosedChannelRecreates(t *testing.T) {
	pool, _, cleanup := setupTestPoolWithLogger(t)
	defer cleanup()

	pc1, err := pool.Checkout(2 * time.Second)
	require.NoError(t, err)

	pc1.close()
	pool.Return(pc1)

	pc2, err := pool.Checkout(2 * time.Second)
	require.NoError(t, err)
	require.NotNil(t, pc2)

	ack, err := pc2.PublishWithConfirm("", "test-closed-return", false, false, amqp.Publishing{Body: []byte("ok")})
	require.NoError(t, err)
	require.True(t, ack)
}

func TestChannelPool_CheckedOutCounter(t *testing.T) {
	pool, _, cleanup := setupTestPoolWithLogger(t)
	defer cleanup()

	require.Equal(t, int64(0), pool.CheckedOut())

	pc1, _ := pool.Checkout(2 * time.Second)
	require.Equal(t, int64(1), pool.CheckedOut())

	pc2, _ := pool.Checkout(2 * time.Second)
	require.Equal(t, int64(2), pool.CheckedOut())

	pool.Return(pc1)
	require.Equal(t, int64(1), pool.CheckedOut())

	pool.Return(pc2)
	require.Equal(t, int64(0), pool.CheckedOut())
}

func TestChannelPool_SizeAndAvailable(t *testing.T) {
	pool, _, cleanup := setupTestPoolWithLogger(t)
	defer cleanup()

	require.Equal(t, 3, pool.Size())
	require.Equal(t, 3, pool.Available())

	pc1, _ := pool.Checkout(2 * time.Second)
	require.Equal(t, 3, pool.Size())
	require.Equal(t, 2, pool.Available())

	pool.Return(pc1)
	require.Equal(t, 3, pool.Size())
	require.Equal(t, 3, pool.Available())
}
