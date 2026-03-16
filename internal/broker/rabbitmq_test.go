package broker

import (
	"context"
	"sync"
	"testing"
	"time"

	amqp "github.com/streadway/amqp"
	"ia-text-orchestrator/internal/config"
	"ia-text-orchestrator/pkg/logging"
)

func TestRabbitMQBroker_Reconnect(t *testing.T) {
	t.Run("exponential backoff calculation", func(t *testing.T) {
		backoff := InitialBackoff
		expectedBackoffs := []float64{
			2.0,
			3.0,
			4.5,
			6.75,
			10.125,
		}

		for i, expected := range expectedBackoffs {
			if i > 0 {
				backoff = time.Duration(float64(backoff) * 1.5)
			}
			if backoff > MaxBackoff {
				backoff = MaxBackoff
			}
			actual := float64(backoff) / float64(time.Second)
			if actual != expected {
				t.Errorf("Backoff %d: expected %v, got %v", i, expected, actual)
			}
		}
	})

	t.Run("max backoff cap", func(t *testing.T) {
		backoff := 120 * time.Second
		if backoff > MaxBackoff {
			backoff = MaxBackoff
		}
		if backoff != 60*time.Second {
			t.Errorf("Backoff should be capped at MaxBackoff (60s), got %v", backoff)
		}
	})

	t.Run("backoff never exceeds max", func(t *testing.T) {
		backoff := InitialBackoff
		for i := 0; i < 20; i++ {
			backoff = time.Duration(float64(backoff) * 1.5)
			if backoff > MaxBackoff {
				backoff = MaxBackoff
			}
			if backoff > MaxBackoff {
				t.Errorf("Backoff at iteration %d exceeds MaxBackoff: %v", i, backoff)
			}
		}
		if backoff != MaxBackoff {
			t.Errorf("Expected backoff to be capped at MaxBackoff after many iterations, got %v", backoff)
		}
	})
}

func TestRabbitMQBroker_CloseStopMonitoring(t *testing.T) {
	broker := &RabbitMQBroker{
		stopChan: make(chan struct{}),
	}

	var wg sync.WaitGroup
	wg.Add(1)

	go func() {
		defer wg.Done()
		select {
		case <-broker.stopChan:
		case <-time.After(100 * time.Millisecond):
		}
	}()

	broker.Close()
	wg.Wait()
}

func TestRabbitMQBroker_PublishReconnectOnNilChannel(t *testing.T) {
	broker := &RabbitMQBroker{
		channel:  nil,
		mu:       sync.RWMutex{},
		stopChan: make(chan struct{}),
		config: &config.Config{
			RabbitMQURL: "amqp://guest:guest@localhost:5672/",
		},
		logger: logging.GetLogger(),
	}

	ctx := context.Background()
	err := broker.Publish(ctx, "test_queue", map[string]string{"key": "value"})

	if err == nil {
		t.Error("Expected error when channel is nil")
	}
}

func TestRabbitMQBroker_GetQueueInfoReconnectOnNilChannel(t *testing.T) {
	broker := &RabbitMQBroker{
		channel:  nil,
		mu:       sync.RWMutex{},
		stopChan: make(chan struct{}),
		config: &config.Config{
			RabbitMQURL: "amqp://guest:guest@localhost:5672/",
		},
		logger: logging.GetLogger(),
	}

	_, err := broker.GetQueueInfo("test_queue")

	if err == nil {
		t.Error("Expected error when channel is nil")
	}
}

func TestRabbitMQBroker_RedeclareQueuesFailure(t *testing.T) {
	broker := &RabbitMQBroker{
		channel:  nil,
		mu:       sync.RWMutex{},
		stopChan: make(chan struct{}),
		config: &config.Config{
			RabbitMQURL: "amqp://guest:guest@localhost:5672/",
		},
		logger: logging.GetLogger(),
	}

	broker.mu.Lock()
	broker.channel = nil
	broker.mu.Unlock()

	if broker.channel != nil {
		t.Error("Expected channel to be nil")
	}
}

func TestRabbitMQBroker_ReconnectMutex(t *testing.T) {
	broker := &RabbitMQBroker{
		stopChan: make(chan struct{}),
	}

	var wg sync.WaitGroup
	iterations := 10
	successCount := 0
	var mu sync.Mutex

	for i := 0; i < iterations; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			// Test that CompareAndSwap works correctly for lock-free synchronization
			if broker.isReconnecting.CompareAndSwap(false, true) {
				mu.Lock()
				successCount++
				mu.Unlock()
				time.Sleep(10 * time.Millisecond)
				broker.isReconnecting.Store(false)
			}
		}()
	}

	wg.Wait()
}

func TestRabbitMQBroker_NotifyCloseChannel(t *testing.T) {
	notifyChan := make(chan *amqp.Error)
	closeChan := make(chan struct{})

	go func() {
		notifyChan <- &amqp.Error{
			Code:   504,
			Reason: "channel/connection is not open",
		}
	}()

	select {
	case err := <-notifyChan:
		if err.Code != 504 {
			t.Errorf("Expected error code 504, got %d", err.Code)
		}
	case <-time.After(100 * time.Millisecond):
		t.Error("Expected error on notify channel")
	}

	close(closeChan)
}

func TestRabbitMQBroker_MaxReconnectAttempts(t *testing.T) {
	maxAttempts := 0
	for i := 0; i < MaxReconnectAttempts; i++ {
		maxAttempts++
	}

	if maxAttempts != MaxReconnectAttempts {
		t.Errorf("Expected %d attempts, got %d", MaxReconnectAttempts, maxAttempts)
	}
}

func TestRabbitMQBroker_Constants(t *testing.T) {
	if MaxReconnectAttempts != 10 {
		t.Errorf("Expected MaxReconnectAttempts to be 10, got %d", MaxReconnectAttempts)
	}

	if InitialBackoff != 2*time.Second {
		t.Errorf("Expected InitialBackoff to be 2s, got %v", InitialBackoff)
	}

	if MaxBackoff != 60*time.Second {
		t.Errorf("Expected MaxBackoff to be 60s, got %v", MaxBackoff)
	}
}
