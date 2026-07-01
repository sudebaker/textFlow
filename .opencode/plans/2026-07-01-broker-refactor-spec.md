# Spec: Refactorización robusta del broker RabbitMQ en Go

## Objective

Eliminar los riesgos de concurrencia, goroutine leaks y fragilidad de reconexión en el broker RabbitMQ del orchestrator (`internal/broker`). El broker es el corazón de la orquestación: cualquier pérdida de mensaje, bloqueo o reconexión incompleta impacta directamente todos los jobs de procesamiento documental.

Success criteria concretos:

1. **Channel pool seguro**: un canal del pool no puede ser usado por dos goroutines simultáneamente. Cada publicación hace checkout del canal, publica, espera confirmación y devuelve el canal.
2. **Sin goroutine leaks**: si una publicación hace timeout o es cancelada, el listener de confirms no queda bloqueado intentando escribir en un canal abandonado.
3. **Reconexión robusta del consumidor**: si el canal/conexión de consumo se cierra, el broker redeclara exchanges/queues y reinicia el consumidor de forma automática sin perder mensajes ya acked.
4. **Tests reales**: los tests de `internal/broker` deben ejecutarse contra un RabbitMQ real (Docker) o hacer skip si no hay URL configurada.
5. **Métricas intactas**: `RabbitMQErrors`, `QueuePublishTotal`, `RabbitMQReconnects`, `RabbitMQReconnectErrors` se mantienen.
6. **Backward compatibility**: la interfaz pública `broker.New`, `Publish`, `PublishJobMessage`, `Consume`, `ConsumeWithContext`, `UpdateQueueMetrics`, `Close` y `GetQueueInfo` se mantiene sin cambios de firma.

## Tech Stack

- Go 1.22
- `github.com/rabbitmq/amqp091-go`
- Script bash con Docker para tests de integración
- Prometheus client para métricas
- zerolog para logging

## Commands

```bash
# Unit tests (RabbitMQ real optional via RABBITMQ_URL)
go test -v ./internal/broker/...

# Integration tests con RabbitMQ en Docker (requiere Docker)
make test-broker

# Full build y tests
make build-orchestrator
make test

# Lint (requiere golangci-lint)
make lint
```

## Project Structure

```
internal/broker/
├── rabbitmq.go              # Broker principal; délega a componentes
├── pool.go                  # ChannelPool con checkout/return
├── publisher.go              # Publicación con confirm
├── consumer.go              # Consumo con reconexión automática
├── reconnect.go             # Lógica de reconexión y redeclaración
├── pool_test.go             # Tests unitarios del pool
├── integration_test.go      # Tests de integración
└── test_broker.sh          # Script que levanta RabbitMQ para tests
```

## Code Style

Go idiomático, siguiendo el proyecto actual:

```go
func (b *RabbitMQBroker) Publish(ctx context.Context, queue string, message interface{}) error {
    return b.pub.publishJSON(ctx, queue, message)
}
```

- Nombres exported en PascalCase, unexported en camelCase.
- Errores wrappeados con `%w`.
- Logging estructurado con zerolog.
- Mutexes cerca de los datos que protegen.
- Contextos respetados en todas las operaciones bloqueantes.

## Testing Strategy

- **Unitarios**: `pool_test.go` valida checkout/return, timeout de confirm, goroutine leak.
- **Integración**: `integration_test.go` y `test_broker.sh` levantan RabbitMQ en Docker, publican 100 mensajes concurrentes con `-race`, verifican reconexión del consumidor.
- **Tests que saltan**: cuando `RABBITMQ_URL` no está configurada, los tests hacen `t.Skip()` con instrucciones claras.
- Cobertura objetivo: `internal/broker` alcanzar ≥70% de cobertura de líneas ejecutables.

## Boundaries

- **Always**: wrappear errores, respetar contextos, mantener la interfaz pública, ejecutar `go test` antes de commit.
- **Ask first**: añadir nuevas dependencias a `go.mod`, cambiar la firma de funciones públicas del broker, modificar docker-compose para añadir tests.
- **Never**: introducir sleeps arbitrarios sin justificación, eliminar métricas existentes, hacer commit de credenciales.

## Success Criteria

1. `go test ./internal/broker/...` pasa (RabbitMQ opcional).
2. `make test-broker` pasa con `docker run rabbitmq:3.13-management` y `-race` detects 0 data races en 100 publicaciones concurrentes.
3. No hay goroutines de `poolChannel.listen` acumulándose entre tests.
4. `make build-orchestrator` y `make test` siguen funcionando.

## Implementation Notes

- **Script bash en vez de testcontainers**: para tests de integración se usa `scripts/test_broker.sh` que levanta un contenedor Docker temporal y exporta `RABBITMQ_URL`. Esto evita añadir nuevas dependencias Go y funciona en cualquier entorno con Docker.
- **Publisher extraído**: toda la lógica de publicación con confirm está en `publisher.go`, limpia y testeable.
- **Consumer con loop de reconexión**: `ConsumeWithContext` en `consumer.go` detecta canal cerrado y llama a `reconnect()` automáticamente.
- **Topología redeclarada en reconnect**: `declareTopology()` redeclara DLX, delayed exchange y colas tras reconexión.

## Open Questions

Ninguno — el plan está implementado y verificado.
