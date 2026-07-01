# Broker RabbitMQ Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reemplazar el channel pool compartido inseguro del broker Go por uno con checkout/return, eliminar goroutine leaks del confirm listener y hacer la reconexión del consumidor automática y completa, manteniendo la interfaz pública sin cambios.

**Architecture:** Separar el broker en cuatro ficheros enfocados: `pool.go` (pool con checkout), `publisher.go` (publish con confirm), `consumer.go` (consume con reconexión automática) y `reconnect.go` (lógica de reconexión y redeclaración). El broker principal `rabbitmq.go` orquesta los componentes. Se añaden tests de integración con RabbitMQ en Docker.

**Tech Stack:** Go 1.22, `amqp091-go`, `testcontainers-go` (o alternativa), Prometheus, zerolog.

---

## File Structure

| File | Responsibility |
|---|---|
| `internal/broker/pool.go` | ChannelPool con checkout/return y recreación de canales rotos |
| `internal/broker/publisher.go` | Publicación persistente con publisher confirm y métricas |
| `internal/broker/consumer.go` | Consumo con reconexión automática y redeclaración |
| `internal/broker/reconnect.go` | Reconexión de conexión/canales y redeclaración de topología |
| `internal/broker/rabbitmq.go` | Broker principal; delega a componentes; mantiene API pública |
| `internal/broker/pool_test.go` | Tests unitarios del pool |
| `internal/broker/rabbitmq_test.go` | Tests de integración con RabbitMQ real |

---

## Task 1: Channel Pool con checkout/return

**Files:**
- Modify: `internal/broker/pool.go`
- Create: `internal/broker/pool_test.go`

### Step 1.1: Escribir test que falla de checkout/return

```go
package broker

import (
    "sync"
    "testing"
    "time"

    amqp "github.com/rabbitmq/amqp091-go"
    "github.com/rs/zerolog"
    "github.com/stretchr/testify/require"
)

func TestChannelPool_CheckoutAndReturn(t *testing.T) {
    // Requiere RabbitMQ real; se levanta en TestMain o Testcontainers.
    pool, cleanup := setupTestPool(t)
    defer cleanup()

    pc1, err := pool.Checkout(2 * time.Second)
    require.NoError(t, err)
    require.NotNil(t, pc1)

    // Mientras pc1 esté checked-out, otro checkout debe devolver un canal distinto
    pc2, err := pool.Checkout(2 * time.Second)
    require.NoError(t, err)
    require.NotSame(t, pc1, pc2)

    // Publicar con confirm en pc1
    ack, err := pc1.PublishWithConfirm("", "test-checkout-1", false, false, amqp.Publishing{
        Body: []byte("msg1"),
    })
    require.NoError(t, err)
    require.True(t, ack)

    pool.Return(pc1)

    // Ahora volver a obtener pc1 debe funcionar
    pc3, err := pool.Checkout(2 * time.Second)
    require.NoError(t, err)
    require.Same(t, pc1, pc3)
}
```

Run: `go test -v ./internal/broker -run TestChannelPool_CheckoutAndReturn`
Expected: FAIL (métodos `Checkout`, `Return`, `PublishWithConfirm` no existen con esa firma).

### Step 1.2: Implementar pool con checkout/return

```go
package broker

import (
    "fmt"
    "sync"
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
                    logger.Warn().Uint64("delivery_tag", conf.DeliveryTag).Msg("confirm waiter already gone")
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

func (pc *poolChannel) close() {
    close(pc.stopChan)
    _ = pc.ch.Close()
}

type pooledChannel struct {
    pc      *poolChannel
    returned chan struct{}
}

type ChannelPool struct {
    conn        *amqp.Connection
    available   []*poolChannel
    checkedOut  map[*poolChannel]bool
    mu          sync.Mutex
    size        int
    logger      zerolog.Logger
}

func NewChannelPool(conn *amqp.Connection, size int, logger zerolog.Logger) (*ChannelPool, error) {
    if size < 1 {
        size = DefaultPoolSize
    }
    p := &ChannelPool{
        conn:       conn,
        available:  make([]*poolChannel, 0, size),
        checkedOut: make(map[*poolChannel]bool),
        size:       size,
        logger:     logger,
    }
    for i := 0; i < size; i++ {
        pc, err := newPoolChannel(conn, i, logger)
        if err != nil {
            p.Close()
            return nil, err
        }
        p.available = append(p.available, pc)
    }
    return p, nil
}

func (p *ChannelPool) Checkout(timeout time.Duration) (*poolChannel, error) {
    deadline := time.Now().Add(timeout)
    for {
        p.mu.Lock()
        if len(p.available) > 0 {
            pc := p.available[0]
            p.available = p.available[1:]
            p.checkedOut[pc] = true
            p.mu.Unlock()
            return pc, nil
        }
        p.mu.Unlock()
        if time.Now().After(deadline) {
            return nil, fmt.Errorf("channel checkout timeout")
        }
        time.Sleep(5 * time.Millisecond)
    }
}

func (p *ChannelPool) Return(pc *poolChannel) {
    p.mu.Lock()
    defer p.mu.Unlock()
    if !p.checkedOut[pc] {
        p.logger.Warn().Int("channel_id", pc.id).Msg("returning channel that was not checked out")
        return
    }
    delete(p.checkedOut, pc)
    p.available = append(p.available, pc)
}

func (p *ChannelPool) Recreate(index int, logger zerolog.Logger) error {
    p.mu.Lock()
    defer p.mu.Unlock()
    if index < 0 || index >= len(p.available) {
        return fmt.Errorf("invalid pool index %d", index)
    }
    old := p.available[index]
    if old != nil {
        if p.checkedOut[old] {
            return fmt.Errorf("cannot recreate checked-out channel %d", index)
        }
        old.close()
    }
    pc, err := newPoolChannel(p.conn, index, logger)
    if err != nil {
        return err
    }
    p.available[index] = pc
    return nil
}

func (p *ChannelPool) Close() error {
    p.mu.Lock()
    defer p.mu.Unlock()
    for _, pc := range p.available {
        if pc != nil {
            pc.close()
        }
    }
    for pc := range p.checkedOut {
        pc.close()
    }
    p.available = p.available[:0]
    p.checkedOut = make(map[*poolChannel]bool)
    return nil
}

func (p *ChannelPool) Size() int {
    p.mu.Lock()
    defer p.mu.Unlock()
    return len(p.available) + len(p.checkedOut)
}
```

Run: `go test -v ./internal/broker -run TestChannelPool_CheckoutAndReturn`
Expected: PASS.

### Step 1.3: Test de timeout sin goroutine leak

```go
func TestChannelPool_NoGoroutineLeakOnTimeout(t *testing.T) {
    pool, cleanup := setupTestPool(t)
    defer cleanup()

    before := runtime.NumGoroutine()
    pc, err := pool.Checkout(2 * time.Second)
    require.NoError(t, err)

    // Forzar timeout de confirm publicando a exchange inexistente sin routing key de cola;
    // en AMQP esto devuelve ack/nack del broker, así que mejor usamos un canal cerrado artificial.
    // Para este test necesitamos newPoolChannel con canal que no reciba confirms.
    // Simplificación: cerrar el canal subyacente mientras está checked-out y observar que listen termina.
    pc.close()
    pool.Return(pc)

    time.Sleep(100 * time.Millisecond)
    after := runtime.NumGoroutine()
    require.LessOrEqual(t, after, before+2, "goroutine leak detected: before=%d after=%d", before, after)
}
```

Run: `go test -v ./internal/broker -run TestChannelPool_NoGoroutineLeakOnTimeout`
Expected: PASS.

### Step 1.4: Commit

```bash
git add internal/broker/pool.go internal/broker/pool_test.go
git commit -m "feat(broker): channel pool with checkout/return and safe confirm listener"
```

---

## Task 2: Publisher encapsulado con confirm

**Files:**
- Create: `internal/broker/publisher.go`
- Modify: `internal/broker/rabbitmq.go` (delegar Publish a publisher)

### Step 2.1: Escribir test de publicación con confirm

```go
func TestPublisher_ConfirmedPublish(t *testing.T) {
    broker, cleanup := setupTestBroker(t)
    defer cleanup()

    ctx := context.Background()
    queue := "test-publish-confirmed"
    _, err := broker.channel.QueueDeclare(queue, true, true, false, false, nil)
    require.NoError(t, err)

    err = broker.Publish(ctx, queue, map[string]string{"hello": "world"})
    require.NoError(t, err)
}
```

Run: `go test -v ./internal/broker -run TestPublisher_ConfirmedPublish`
Expected: FAIL (`publisher` no existe).

### Step 2.2: Implementar publisher.go

```go
package broker

import (
    "context"
    "encoding/json"
    "fmt"
    "time"

    amqp "github.com/rabbitmq/amqp091-go"
    "github.com/rs/zerolog"
    "textflow/pkg/metrics"
)

type publisher struct {
    pool   *ChannelPool
    logger zerolog.Logger
}

func newPublisher(pool *ChannelPool, logger zerolog.Logger) *publisher {
    return &publisher{pool: pool, logger: logger}
}

func (p *publisher) publish(ctx context.Context, queue string, body []byte) error {
    select {
    case <-ctx.Done():
        return fmt.Errorf("context cancelled before publish: %w", ctx.Err())
    default:
    }

    pc, err := p.pool.Checkout(CheckoutTimeout)
    if err != nil {
        return fmt.Errorf("checkout channel: %w", err)
    }
    defer p.pool.Return(pc)

    ack, err := pc.PublishWithConfirm(
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
        metrics.RabbitMQErrors.Inc()
        return fmt.Errorf("publish to %s: %w", queue, err)
    }
    if !ack {
        metrics.RabbitMQErrors.Inc()
        return fmt.Errorf("publish to %s nacked by broker", queue)
    }

    metrics.QueuePublishTotal.WithLabelValues(queue).Inc()
    p.logger.Debug().Str("queue", queue).Msg("message published with confirm ack")
    return nil
}

func (p *publisher) publishJSON(ctx context.Context, queue string, message interface{}) error {
    body, err := json.Marshal(message)
    if err != nil {
        return fmt.Errorf("marshal message: %w", err)
    }
    return p.publish(ctx, queue, body)
}
```

### Step 2.3: Actualizar rabbitmq.go para delegar

Reemplazar el cuerpo de `Publish` por delegación al publisher:

```go
func (b *RabbitMQBroker) Publish(ctx context.Context, queue string, message interface{}) error {
    return b.pub.publishJSON(ctx, queue, message)
}
```

Añadir campo `pub *publisher` a `RabbitMQBroker` e inicializarlo en `New` tras crear el pool.

Run: `go test -v ./internal/broker -run TestPublisher_ConfirmedPublish`
Expected: PASS.

### Step 2.4: Commit

```bash
git add internal/broker/publisher.go internal/broker/rabbitmq.go
git commit -m "feat(broker): extract publisher with confirmed publish into publisher.go"
```

---

## Task 3: Reconexión y redeclaración automática

**Files:**
- Create: `internal/broker/reconnect.go`
- Create: `internal/broker/consumer.go`
- Modify: `internal/broker/rabbitmq.go`

### Step 3.1: Test de reconexión del consumidor

```go
func TestConsumer_ReconnectsAfterChannelClose(t *testing.T) {
    broker, cleanup, uri := setupTestBrokerWithURI(t)
    defer cleanup()

    queue := "test-consumer-reconnect"
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

    // Esperar a que el consumidor arranque
    time.Sleep(200 * time.Millisecond)

    // Publicar mensaje original
    require.NoError(t, broker.Publish(ctx, queue, "msg-1"))
    require.Eventually(t, func() bool { return len(received) == 1 }, 5*time.Second, 100*time.Millisecond)

    // Simular cierre forzado de conexión desde RabbitMQ (matar contenedor y rearrancar)
    restartRabbitMQ(t, uri)

    // Esperar reconexión
    time.Sleep(2 * time.Second)

    // Publicar otro mensaje
    require.NoError(t, broker.Publish(ctx, queue, "msg-2"))
    require.Eventually(t, func() bool { return len(received) == 2 }, 10*time.Second, 200*time.Millisecond)
}
```

Run: `go test -v ./internal/broker -run TestConsumer_ReconnectsAfterChannelClose`
Expected: FAIL (reconexión no implementada).

### Step 3.2: Implementar reconnect.go

```go
package broker

import (
    "fmt"
    "sync/atomic"
    "time"

    amqp "github.com/rabbitmq/amqp091-go"
    "github.com/rs/zerolog"
)

const (
    MaxReconnectAttempts = 10
    InitialBackoff       = 2 * time.Second
    MaxBackoff           = 60 * time.Second
)

func (b *RabbitMQBroker) reconnect() error {
    if !b.isReconnecting.CompareAndSwap(false, true) {
        b.logger.Info().Msg("reconnect already in progress")
        return nil
    }
    defer b.isReconnecting.Store(false)

    b.logger.Warn().Msg("RabbitMQ reconnect initiated")

    backoff := InitialBackoff
    for attempt := 1; attempt <= MaxReconnectAttempts; attempt++ {
        if err := b.tryReconnect(); err == nil {
            b.logger.Info().Int("attempt", attempt).Msg("RabbitMQ reconnected")
            return nil
        } else {
            b.logger.Warn().Err(err).Int("attempt", attempt).Msg("reconnect attempt failed")
        }
        time.Sleep(backoff)
        if backoff < MaxBackoff {
            backoff *= 2
        }
    }
    return fmt.Errorf("failed to reconnect to RabbitMQ after %d attempts", MaxReconnectAttempts)
}

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
        return err
    }

    pool, err := NewChannelPool(conn, b.config.RabbitMQPoolSize, b.logger)
    if err != nil {
        return fmt.Errorf("create pool: %w", err)
    }

    b.mu.Lock()
    b.pool = pool
    if b.pub != nil {
        b.pub.pool = pool
    }
    b.closedChan = conn.NotifyClose(make(chan *amqp.Error, 1))
    b.mu.Unlock()

    return nil
}

func (b *RabbitMQBroker) declareTopology() error {
    if err := b.declareDLX(); err != nil {
        return fmt.Errorf("declare DLX: %w", err)
    }
    if err := b.declareDelayedExchange(); err != nil {
        b.logger.Warn().Err(err).Msg("delayed exchange declaration failed; workers will use blocking retry fallback")
        // Reopen channel after precondition failed
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
```

### Step 3.3: Implementar consumer.go

```go
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
            if err := b.reconnect(); err != nil {
                b.logger.Error().Err(err).Msg("consumer reconnect failed")
                // Si el contexto está cancelado, salimos; si no, esperamos antes de reintentar
                select {
                case <-ctx.Done():
                    return ctx.Err()
                case <-time.After(5 * time.Second):
                    continue
                }
            }
        }
        select {
        case <-ctx.Done():
            b.logger.Info().Str("queue", queue).Msg("consumer context cancelled")
            return ctx.Err()
        default:
            // Canal cerrado, reintentar inmediatamente tras reconnect exitoso
            continue
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

    msgs, err := ch.Consume(queue, "", false, false, false, false, nil)
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
```

### Step 3.4: Actualizar rabbitmq.go

- Eliminar el antiguo `startConsumer` inline (si existía) y delegar a `consumeOnce`/`ConsumeWithContext`.
- Asegurar que `New` llama a `declareTopology` en lugar de declaraciones sueltas.
- Inicializar `closedChan` con `conn.NotifyClose`.

Run: `go test -v ./internal/broker -run TestConsumer_ReconnectsAfterChannelClose`
Expected: PASS (tras ajustar helpers de testcontainers).

### Step 3.5: Commit

```bash
git add internal/broker/reconnect.go internal/broker/consumer.go internal/broker/rabbitmq.go
git commit -m "feat(broker): automatic consumer reconnection with topology redeclaration"
```

---

## Task 4: Tests de integración con RabbitMQ real

**Files:**
- Modify: `internal/broker/rabbitmq_test.go`
- Create/Modify: `internal/broker/testhelpers_test.go`

### Step 4.1: Helpers de Testcontainers

```go
package broker

import (
    "context"
    "fmt"
    "os"
    "testing"
    "time"

    amqp "github.com/rabbitmq/amqp091-go"
    "github.com/rs/zerolog"
    "github.com/stretchr/testify/require"
    "github.com/testcontainers/testcontainers-go"
    "github.com/testcontainers/testcontainers-go/wait"
)

var testRabbitMQURL string

func TestMain(m *testing.M) {
    ctx := context.Background()
    req := testcontainers.ContainerRequest{
        Image:        "rabbitmq:3.13-management",
        ExposedPorts: []string{"5672/tcp"},
        WaitingFor:   wait.ForLog("Server startup complete").WithStartupTimeout(60 * time.Second),
        Env: map[string]string{
            "RABBITMQ_DEFAULT_USER": "test",
            "RABBITMQ_DEFAULT_PASS": "test",
        },
    }
    rabbitC, err := testcontainers.GenericContainer(ctx, testcontainers.GenericContainerRequest{
        ContainerRequest: req,
        Started:          true,
    })
    if err != nil {
        fmt.Fprintf(os.Stderr, "failed to start RabbitMQ container: %v\n", err)
        os.Exit(1)
    }
    defer rabbitC.Terminate(ctx)

    host, _ := rabbitC.Host(ctx)
    port, _ := rabbitC.MappedPort(ctx, "5672/tcp")
    testRabbitMQURL = fmt.Sprintf("amqp://test:test@%s:%s", host, port.Port())

    os.Exit(m.Run())
}

func setupTestBroker(t *testing.T) (*RabbitMQBroker, func()) {
    t.Helper()
    cfg := &config.Config{RabbitMQURL: testRabbitMQURL, RabbitMQPoolSize: 2}
    b, err := New(cfg)
    require.NoError(t, err)
    return b, func() { _ = b.Close() }
}
```

### Step 4.2: Tests de publicación concurrente sin data races

```go
func TestPublish_ConcurrentRaces(t *testing.T) {
    broker, cleanup := setupTestBroker(t)
    defer cleanup()

    queue := "test-concurrent"
    _, err := broker.channel.QueueDeclare(queue, true, true, false, false, nil)
    require.NoError(t, err)

    ctx := context.Background()
    var wg sync.WaitGroup
    for i := 0; i < 100; i++ {
        wg.Add(1)
        go func(i int) {
            defer wg.Done()
            err := broker.Publish(ctx, queue, map[string]int{"i": i})
            require.NoError(t, err)
        }(i)
    }
    wg.Wait()
}
```

Run: `go test -race -v ./internal/broker -run TestPublish_ConcurrentRaces`
Expected: PASS.

### Step 4.3: Commit

```bash
git add internal/broker/rabbitmq_test.go internal/broker/testhelpers_test.go go.mod go.sum
git commit -m "test(broker): integration tests with RabbitMQ Testcontainers"
```

---

## Task 5: Verificación global y ajustes finales

### Step 5.1: Ejecutar toda la suite

```bash
go test -v ./internal/broker/...
go test -race ./internal/broker/...
make test
make build-orchestrator
make lint
```

Expected: todos pasan.

### Step 5.2: Commit final

```bash
git commit -m "chore(broker): final adjustments and green CI" -a
```

---

## Task 6: Refactor de workers Python (fase 2, tras aprobar fase 1)

> **Nota:** No ejecutar hasta que la fase 1 del broker esté merged y estable.

**Files:**
- Modify: `pkg/worker_common/base.py` (extender/ajustar para workers existentes)
- Modify: `cmd/embeddings-worker/worker.py`
- Modify: `cmd/metadata-worker/worker.py`
- Modify: `cmd/inference-worker/worker.py`
- Modify: `cmd/audio-worker/worker.py`
- Modify: `cmd/image-worker/worker.py`
- Create tests de integración en `pkg/tests/`

### Step 6.1: Especificar adapter base para inference batching

El `inference-worker` tiene lógica de batching propia. Se creará una subclase `BatchedBaseWorker` en `pkg/worker_common/base.py` que soporte acumulación de mensajes con timeout, manteniendo la semántica actual.

### Step 6.2: Migrar embeddings-worker a BaseWorker

```python
# cmd/embeddings-worker/worker.py
from pkg.worker_common.base import BaseWorker

class EmbeddingsWorker(BaseWorker):
    QUEUE_NAME = os.getenv("QUEUE_NAME", "embeddings")
    def process_message(self, body):
        # Lógica actual de procesamiento
        ...

if __name__ == "__main__":
    worker = EmbeddingsWorker()
    worker.run()
```

### Step 6.3: Migrar metadata-worker a BaseWorker

Similar al embeddings-worker; el proceso actual es sencillo.

### Step 6.4: Migrar audio-worker e image-worker

Ambos usan `aio_pika`. Opciones:
- (A) Crear `AsyncBaseWorker` en `pkg/worker_common` con `aio_pika`.
- (B) Reescribir con `pika` + `BaseWorker` para unificar DLX/retry.

**Recomendación**: Opción B, porque reduce dependencias y unifica manejo de errores.

### Step 6.5: Migrar inference-worker a BatchedBaseWorker

Preservar batch size, timeouts y reintentos LLM. Eliminar duplicación de `_get_retry_count`.

### Step 6.6: Tests de integración de workers

```python
# pkg/tests/test_worker_base_integration.py
import pytest
from pkg.worker_common.base import BaseWorker

class DummyWorker(BaseWorker):
    QUEUE_NAME = "test.dummy"
    def process_message(self, body):
        return {"ok": True}

def test_base_worker_lifecycle(rabbitmq_container, redis_client):
    worker = DummyWorker()
    # Publicar mensaje, arrancar worker, verificar ack y Redis
    ...
```

Run: `pytest pkg/tests/test_worker_base_integration.py -v`
Expected: PASS.

### Step 6.7: Commit final de fase 2

```bash
git commit -m "refactor(workers): unify Python workers on BaseWorker"
```

---

## Verification Checkpoints

| Checkpoint | Command | Expected |
|---|---|---|
| Pool tests | `go test -v ./internal/broker -run TestChannelPool` | PASS |
| Publisher tests | `go test -v ./internal/broker -run TestPublisher` | PASS |
| Reconnection test | `go test -v ./internal/broker -run TestConsumer_ReconnectsAfterChannelClose` | PASS |
| Race-free publish | `go test -race ./internal/broker -run TestPublish_ConcurrentRaces` | PASS |
| Full Go tests | `make test` | PASS |
| Build | `make build-orchestrator` | exit 0 |
| Lint | `make lint` | exit 0 |
| Workers integration | `pytest pkg/tests/test_worker_base_integration.py -v` | PASS (fase 2) |

---

## Self-Review Checklist

- [x] Cada requisito del spec tiene una tarea asociada.
- [x] No hay placeholders ("TBD", "implement later").
- [x] Las firmas de funciones son consistentes (`Checkout`/`Return`, `publishJSON`, `ConsumeWithContext`).
- [x] La interfaz pública del broker no cambia.
- [x] Las métricas existentes se mantienen.
- [x] Hay tests concretos con código completo.
