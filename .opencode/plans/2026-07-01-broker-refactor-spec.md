# Spec: Refactorización robusta del broker RabbitMQ en Go

## Objective

Eliminar los riesgos de concurrencia, goroutine leaks y fragilidad de reconexión en el broker RabbitMQ del orchestrator (`internal/broker`). El broker es el corazón de la orquestación: cualquier pérdida de mensaje, bloqueo o reconexión incompleta impacta directamente todos los jobs de procesamiento documental.

Success criteria concretos:

1. **Channel pool seguro**: un canal del pool no puede ser usado por dos goroutines simultáneamente. Cada publicación hace checkout del canal, publica, espera confirmación y devuelve el canal.
2. **Sin goroutine leaks**: si una publicación hace timeout o es cancelada, el listener de confirms no queda bloqueado intentando escribir en un canal abandonado.
3. **Reconexión robusta del consumidor**: si el canal/conexión de consumo se cierra, el broker redeclara exchanges/queues y reinicia el consumidor de forma automática sin perder mensajes ya acked.
4. **Tests reales**: los tests de `internal/broker` deben ejecutarse contra un RabbitMQ real (Docker/Testcontainers) o, como mínimo, simular fallos de canal para validar reconexión y publisher confirms.
5. **Métricas intactas**: `RabbitMQErrors`, `QueuePublishTotal` y demás métricas deben seguir reportándose correctamente tras el refactor.
6. **Backward compatibility**: la interfaz pública `broker.New`, `Publish`, `PublishJobMessage`, `ConsumeWithContext`, `UpdateQueueMetrics`, `Close` y `GetQueueInfo` se mantiene sin cambios de firma.

## Tech Stack

- Go 1.22
- `github.com/rabbitmq/amqp091-go`
- `github.com/testcontainers/testcontainers-go` (para tests de integración; si no está disponible, usar script de Docker inline)
- `github.com/ory/dockertest/v3` (alternativa si testcontainers no está en go.mod)
- Prometheus client para métricas
- zerolog para logging

## Commands

```bash
# Run broker tests (requires RabbitMQ container)
go test -v ./internal/broker/...

# Run all Go tests
make test

# Build orchestrator
make build-orchestrator

# Lint
make lint

# Format
make format
```

## Project Structure

Archivos involucrados en el refactor:

```
internal/broker/
├── rabbitmq.go              # Broker principal; separar reconnect en reconnect.go
├── pool.go                  # ChannelPool actual; reescribir con checkout/return
├── publisher.go             # NUEVO: publicación con confirm encapsulada
├── consumer.go              # NUEVO: consumo con reconexión automática
├── reconnect.go             # NUEVO: lógica de reconexión y redeclaración
├── rabbitmq_test.go         # Tests con RabbitMQ real/Testcontainers
└── pool_test.go             # NUEVO: tests del channel pool

internal/models/
└── job.go                   # JobMessage ya definido, sin cambios
```

## Code Style

Go idiomático, siguiendo el proyecto actual:

```go
func (b *RabbitMQBroker) Publish(ctx context.Context, queue string, message interface{}) error {
    // ...
    if err != nil {
        logger.Error().Err(err).Str("queue", queue).Msg("Publish failed")
        return fmt.Errorf("publish to %s: %w", queue, err)
    }
    return nil
}
```

- Nombres exported en PascalCase, unexported en camelCase.
- Errores wrappeados con `%w`.
- Logging estructurado con zerolog.
- Mutexes cerca de los datos que protegen.
- Contextos respetados en todas las operaciones bloqueantes.

## Testing Strategy

- **Unitarios**: `pool_test.go` valida checkout/return, timeout de confirm, recreación de canales.
- **Integración**: `rabbitmq_test.go` levanta RabbitMQ 3.13-management en Docker, declara DLX/delayed exchange, publica mensajes persistentes, confirma acks, simula cierre de canal y verifica reconexión del consumidor.
- **E2E manual**: `make docker-up` y enviar un job al orchestrator; verificar en logs que publisher confirms devuelven `ack` y que el consumidor sigue funcionando tras reiniciar RabbitMQ (`docker restart textflow-rabbitmq`).
- Cobertura objetivo: `internal/broker` alcanzar ≥75% de cobertura de líneas ejecutables.

## Boundaries

- **Always**: wrappear errores, respetar contextos, mantener la interfaz pública, ejecutar `go test` antes de commit.
- **Ask first**: añadir nuevas dependencias a `go.mod`, cambiar la firma de funciones públicas del broker, modificar docker-compose para añadir tests.
- **Never**: introducir sleeps arbitrarios sin justificación, eliminar métricas existentes, hacer commit de credenciales, modificar lógica de workers Python en esta fase.

## Success Criteria

1. `go test -v ./internal/broker/...` pasa con RabbitMQ real.
2. `go test -race ./internal/broker/...` no detecta data races en 100 publicaciones concurrentes.
3. Tras matar y reiniciar el contenedor de RabbitMQ durante un test, el consumidor se reconecta y procesa mensajes nuevos sin intervención manual.
4. No hay goroutines de `poolChannel.listen` acumulándose entre tests (`runtime.NumGoroutine()` estable tras `Close()`).
5. `make build-orchestrator` y `make test` siguen funcionando.

## Open Questions

1. ¿Se permite añadir `testcontainers-go` a `go.mod`, o preferís `dockertest` o un script bash externo?
2. ¿El entorno de CI ya tiene Docker disponible para tests de integración?
3. ¿Se requiere que el broker soporte publicaciones transaccionales en el futuro, o solo publisher confirms?
