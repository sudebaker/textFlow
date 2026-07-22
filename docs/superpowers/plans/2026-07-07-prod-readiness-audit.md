# Plan: Production-Readiness Remediation — textFlow

**Auditoría**: codebase-memory graph queries + verificación cruzada de archivos
**Fecha**: 2026-07-07
**Baseline**: commit `7d87a45` (origin/main)
**Veredicto actual**: NO production-ready (2 CRÍTICOS, 5 HIGH, 6 MEDIUM)

---

## Prioridades

| Prioridad | Cantidad | Bloquea deploy seguro | Bloquea deploy en esta máquina |
|-----------|----------|----------------------|-------------------------------|
| P0 — CRÍTICO | 2 | ✅ Sí | — |
| P1 — HIGH | 5 | Parcial | ✅ H2 |
| P2 — MEDIUM | 6 | No | No |

---

## FASE P0 — CRÍTICOS (bloqueantes para cualquier deploy)

### Task 0.1 — Des-trackear `deploy/docker/.env` de git

**What**: El archivo `deploy/docker/.env` está commiteado (5 commits en `git log`). `.gitignore` sólo excluye `deploy/.env`, no `deploy/docker/.env`. Riesgo de fuga de credenciales cuando se reemplacen los placeholders `CHANGE_ME_*`.

**Files**:
- `deploy/docker/.env` — untrackear, NO borrar del working dir
- `.gitignore` — añadir `deploy/docker/.env` y `deploy/docker/.env.local`
- Mantener trackeado: `deploy/docker/.env.example` (template)

**Steps**:
1. `git rm --cached deploy/docker/.env` (mantiene working copy)
2. Editar `.gitignore` y añadir:
   ```
   deploy/docker/.env
   deploy/docker/.env.local
   deploy/docker/.env.*.local
   ```
3. Verificar que `.env.example` sigue trackeado: `git status deploy/docker/.env.example` → tracked
4. **Importante**: avisar al usuario que cualquier credencial ya commiteada en history debe rotarse (aunque ahora son placeholders, conviene `git log -p deploy/docker/.env` antes de cerrar)
5. Commit: `chore(git): untrack deploy/docker/.env to prevent secret leakage`

**Verify**:
- `git ls-files deploy/docker/.env` → vacío
- `ls deploy/docker/.env` → existe (working copy intacta)
- `git check-ignore -v deploy/docker/.env` → match en `.gitignore`

---

### Task 0.2 — Auth API key middleware en orchestrator

**What**: `cmd/orchestrator/main.go` no tiene auth. Todos los endpoints (`/documents/upload`, `/documents/process`, SSE, `/delete/:id`) están abiertos. Para air-gapped on-prem, API key header check es suficiente.

**Files**:
- `internal/middleware/auth.go` — **crear** (nuevo)
- `cmd/orchestrator/main.go` — registrar middleware
- `internal/config/config.go` — añadir campo `APIKey string env:"API_KEY"` (opcional; si vacío, auth deshabilitada para dev)
- `deploy/docker/.env.example` y `deploy/docker/.env` — añadir `API_KEY=CHANGE_ME_api_key_min32chars`
- `internal/middleware/auth_test.go` — **crear**

**Design**:
```go
// internal/middleware/auth.go
package middleware

import (
    "net/http"
    "crypto/subtle"
    "github.com/gin-gonic/gin"
)

func APIKeyAuth(expectedKey string) gin.HandlerFunc {
    return func(c *gin.Context) {
        if expectedKey == "" {
            c.Next() // dev mode: disabled
            return
        }
        provided := c.GetHeader("X-API-Key")
        if provided == "" || subtle.ConstantTimeCompare([]byte(provided), []byte(expectedKey)) != 1 {
            c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "invalid_api_key"})
            return
        }
        c.Next()
    }
}
```

**Steps**:
1. Crear `internal/middleware/auth.go` con la función de arriba
2. Registrar en `cmd/orchestrator/main.go` justo antes de las rutas protegidas (NO `/health` ni `/metrics`):
   ```go
   protected := router.Group("/", middleware.APIKeyAuth(cfg.APIKey))
   protected.POST("/documents/upload", uploadHandler)
   protected.POST("/documents/process", processHandler)
   // ... resto
   ```
3. Añadir `APIKey` a `Config` con `env:"API_KEY"` (sin `required` — dev-friendly)
4. Añadir `Validate()` check: si `APIKey != "" && len < 32` → error
5. Crear `internal/middleware/auth_test.go` (3 tests: deshabilitado, key válida, key inválida)
6. Actualizar `.env.example` con `API_KEY=` commented out + warning
7. `go build ./cmd/orchestrator && go test ./internal/middleware/...`
8. Commit: `feat(security): add API key auth middleware to orchestrator`

**Verify**:
- `curl -X POST localhost:8080/documents/upload` sin header → 401
- `curl -X POST localhost:8080/documents/upload -H "X-API-Key: wrong"` → 401
- `curl -X POST localhost:8080/documents/upload -H "X-API-Key: <correcto>"` → 400 (pasa auth, falla validación de input)
- `curl localhost:8080/health` → 200 (sin auth)

---

## FASE P1 — HIGH

### Task 1.1 — CI/CD funcional (validación)

**What**: Existe `.github/workflows/ci.yml` (2.9KB) pero no verificamos que pase. `make test` da connection refused a miniredis; la corrida real pasa (vimos `TestRedisClient_TTLSetFailure_PropagatesError PASS`). Hay que auditar el CI y arreglar lo que fallen.

**Files**: `.github/workflows/ci.yml`

**Steps**:
1. Leer `.github/workflows/ci.yml` completo
2. Lanzar workflow manualmente via `gh workflow run ci.yml` o push a rama de test
3. Si falla:
   - Separar `inference-worker` y `metadata-worker` en jobs distintos (colisión módulo `worker.py`)
   - Para tests Go que necesitan Redis: usar `services: redis: image: redis:7-alpine` en el job
   - Para tests de broker que necesitan RabbitMQ real: `services: rabbitmq: image: rabbitmq:3.13-management`
4. Añadir job de `make lint` (golangci-lint action)
5. Añadir `pip install -r cmd/*/requirements-test.txt` antes de pytest
6. Commit: `fix(ci): separate conflicting Python tests and add service containers`

**Verify**: workflow `ci.yml` en `main` → green

---

### Task 1.2 — GPU opcional en docker-compose (desbloquea esta máquina)

**What**: 4 servicios (`embeddings-worker`, `entities-worker`, `completion-worker`, `docling`) declaran `devices: [driver: nvidia]` sin fallback. Esta máquina no tiene NVIDIA. Compose no arranca.

**Files**: `deploy/docker/docker-compose.yml`; `deploy/docker/.env.example`

**Design**: Dos archivos override:
- `docker-compose.yml` — defaults a CPU, sin `devices`, `EMBEDDINGS_DEVICE=cpu`
- `docker-compose.gpu.yml` — override file que añade `devices: [nvidia]` y cambia `EMBEDDINGS_DEVICE=cuda`

Uso:
```bash
# CPU (esta máquina):
docker compose -f docker-compose.yml up
# GPU (producción):
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up
```

**Steps**:
1. Editar `deploy/docker/docker-compose.yml`:
   - Quitar los 4 bloques `devices: [...]` de reservations
   - Cambiar `EMBEDDINGS_DEVICE=${EMBEDDINGS_DEVICE:-cpu}` (era cuda)
   - Mismo para `ENTITIES_DEVICE` y `DOCLING_DEVICE`
   - Borrar `CUDA_VISIBLE_DEVICES=0` y `NVIDIA_VISIBLE_DEVICES=0` de los 3 workers (ponerlos en el override)
2. Crear `deploy/docker/docker-compose.gpu.yml` con override para los 4 servicios:
   ```yaml
   services:
     embeddings-worker:
       environment:
         - EMBEDDINGS_DEVICE=cuda
         - CUDA_VISIBLE_DEVICES=0
         - NVIDIA_VISIBLE_DEVICES=0
       deploy:
         resources:
           reservations:
             devices:
               - driver: nvidia
                 count: 1
                 capabilities: [gpu]
     entities-worker: { ... igual ... }
     completion-worker: { ... igual ... }
     docling: { ... igual ... }
   ```
3. Documentar en `deploy/docker/QUICKSTART.md` los dos modos
4. Verificar: `docker compose -f deploy/docker/docker-compose.yml config --quiet` (sin GPU)
5. Verificar: `docker compose -f deploy/docker/docker-compose.yml -f deploy/docker/docker-compose.gpu.yml config --quiet` (con GPU)
6. Commit: `feat(docker): make GPU optional via compose override file`

**Verify**:
- `docker compose -f deploy/docker/docker-compose.yml config | grep -A2 "devices:"` → no aparece en workers
- `docker compose -f deploy/docker/docker-compose.yml -f deploy/docker/docker-compose.gpu.yml config | grep -A2 "devices:"` → aparece nvidia

---

### Task 1.3 — Eliminar `sys.exit(0)` de signals.py

**What**: `pkg/worker_common/signals.py:56` mata el proceso inmediatamente después de setear `should_exit=True`, sin drenar el mensaje en curso. Jobs quedan en estado `processing` para siempre en Redis.

**Files**:
- `pkg/worker_common/signals.py` — eliminar `sys.exit(0)` de `_handle_signal`
- `pkg/tests/test_graceful_shutdown.py` — el test `test_signal_handler_does_not_call_sys_exit` PERO el path real todavía lo llama. Corregir o recrear el test.

**Design**: SignalHandler debería sólo setear el flag y dejar que el consumer loop termine el mensaje. El `sys.exit(0)` debería ejecutarse desde el main loop del worker, no del signal handler.

```python
def _handle_signal(self, signum, frame):
    signal_name = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
    logger.info(f"Received {signal_name}, initiating graceful shutdown...")
    self.should_exit = True
    if self._cleanup_callback:
        try:
            self._cleanup_callback()
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")
    # NO sys.exit aquí — dejar que el consumer loop lo vea
```

**Steps**:
1. Eliminar línea 56 (`sys.exit(0)`) de `signals.py`
2. En cada worker que usa `SignalHandler`, el main loop ya consulta `should_exit`? Verificar:
   - `grep -r "should_exit" cmd/*/worker.py`
3. Si un worker no consulta `should_exit`, añadir al final del consume loop:
   ```python
   if signal_handler.should_exit:
       break
   ```
4. Actualizar `test_signal_handler_does_not_call_sys_exit` — actualmente pasa pero el path real llama sys.exit. Re-escribir el test:
   ```python
   def test_signal_handler_sets_flag_without_exit(self):
       handler = SignalHandler()
       with patch.object(sys, 'exit') as mock_exit:
           handler._handle_signal(signal.SIGTERM, None)
           assert handler.should_exit is True
           mock_exit.assert_not_called()
   ```
5. `pytest pkg/tests/test_graceful_shutdown.py -v`
6. Commit: `fix(workers): remove sys.exit(0) from signal handler to allow message drain`

**Verify**:
- `python -c "from pkg.worker_common.signals import SignalHandler; import signal; h = SignalHandler(); h._handle_signal(signal.SIGTERM, None); print('alive, should_exit=', h.should_exit)"` → imprime, no termina

---

### Task 1.4 — Eliminar bare `except:` restantes

**What**: Plan original task 2.2 sólo cubría `extraction-worker`. Quedan 3 bare excepts:
- `cmd/docling-server/docling_server.py:101`
- `cmd/docling-server/docling_server.py:199`
- `tests/e2e/test_inference_embeddings_e2e.py:225`

**Files**: los 3 archivos

**Steps**:
1. Para cada ocurrencia, reemplazar `except:` por `except Exception as e:` y añadir el error al log:
   ```python
   except Exception as e:
       logger.error(f"Unexpected error: {e}", exc_info=True)
   ```
2. `python -c "import cmd.docling-server.docling_server" 2>&1` (check smoke)
3. Commit: `fix(docling,e2e): replace bare except with except Exception`

**Verify**: `grep -rn "except:\s*$" cmd/ tests/ pkg/` → 0 resultados

---

### Task 1.5 — Error suppression `SetJobFeatures` (decision)

**What**: `cmd/orchestrator/main.go:480` — si falla el store de features, loguea Error pero no retorna. El job continúa sin features. Necesitamos decidir:

**Opción A**: features son opcionales → el behaviour actual es correcto, sólo mejorar el log para incluir `job_id` y avisar al cliente en la respuesta HTTP que features no se almacenaron (campo `warnings: []`).

**Opción B**: features son obligatorias → retornar 500.

Recomiendo **opción A** (mantiene el behaviour actual, más resiliente) + añadir `job_id` al log.

**Files**: `cmd/orchestrator/main.go:480`

**Steps**:
1. Cambiar l.480:
   ```go
   logger.Error().Err(err).Str("job_id", jobID).Msg("Failed to store job features")
   ```
2. (Opcional, no bloquea) añadir `c.JSON(http.StatusAccepted, ...)` con warning si quieres avisar al cliente
3. Commit: `fix(orchestrator): add job_id to SetJobFeatures error log`

---

## FASE P2 — MEDIUM

### Task 2.1 — Refactor `validateStructuredEntity` (complejidad 75)

`cmd/regex-entity-extractor/main.go` — complejidad 75 es inmantenible. Extraer sub-funciones por tipo de entidad. No urgent pero deuda técnica grave.

### Task 2.2 — `METRICS_PORT=8006` duplicado

`docker-compose.yml:315` (inference-worker) y `:531` (image-worker) usan `8006`. En la red `backend`, los dos no exponen el puerto al host (sin `ports:`), pero entre contenedores Prometheus scrapea por nombre de servicio, no puerto — así que técnicamente no choca si ambos servicios tienen IPs distintas. **Aún así**: Prometheus scrape config por job name → diferente. Verificar `deploy/prometheus/prometheus.yml` antes de tocar. Si no choca, marcar como cosmético.

### Task 2.3 — `depends_on` sin condition en audio/image worker

`docker-compose.yml:543-545` y `:608-611` — cambiar a `condition: service_healthy` para `rabbitmq`, `redis`, `whisper`. Consistencia con el resto del compose.

### Task 2.4 — Coverage tests para workers Python

Las funciones críticas sin test directo (de graph query):
- `MetadataWorker._extract_metadata` (worker.py:85)
- `extract_pdf_metadata` (extraction-worker/worker.py:125)
- `EntitiesWorker._load_model` (entities-worker/worker.py:97)
- `CompletionWorker.send_webhook` (worker.py:154)
- `InferenceWorker.extract_inferences` (worker.py:183)
- `deleteHandler` del orchestrator

Añadir tests unitarios con mocks de Redis/RabbitMQ.

### Task 2.5 — Verificar recursión en `whisper/transcribe`

`deploy/docker/whisper/app/main.transcribe` marcada recursiva, `unguarded_recursion=""` (vacío = indefinido). Revisar si la recursión tiene caso base guardado.

### Task 2.6 — Verificar graceful shutdown regex-entity-extractor

Plan task 2.6. Leer `cmd/regex-entity-extractor/main.go` y verificar si tiene `srv.Shutdown(ctx)` en SIGTERM como el orchestrator.

---

## Resumen de tareas completadas del plan anterior

| Task | Estado |
|------|--------|
| 1.1 Non-root Dockerfiles Go | ✅ HECHO (los 9 Dockerfiles tienen USER) |
| 1.2 Eliminar credenciales guest | ✅ HECHO (placeholders CHANGE_ME + Validate()) |
| 1.3 CORS wildcard | ✅ HECHO (cors_origins_list) |
| 2.1 Import time | ✅ no hay NameError ahora |
| 2.2 Bare except extraction-worker | ✅ HECHO |
| 2.3 Error suppression orchestrator | 🟡 PARCIAL (SetJobFeatures queda) |
| 2.4 TTL propagation Redis | ✅ HECHO (test PASS `TestRedisClient_TTLSetFailure_PropagatesError`) |
| 2.5 Graceful shutdown Python | ❌ NO HECHO (signals.py:56 sys.exit(0)) |
| 2.6 Graceful shutdown regex-extractor | sin verificar |
| 2.7 DLQ pattern | 🟡 PARCIAL (`_handle_transient_error` usa requeue condicionalmente) |
| 2.8 Redis reconnection workers | sin verificar |
| 3.1 Health checks compose | ✅ HECHO (todos servicios tienen healthcheck) |
| 3.2 Redis Exporter | ✅ HECHO (servicio `redis-exporter` presente) |
| 3.3 job_id logs | sin verificar |
| 4.1 RabbitMQ tests hang | sin verificar |
| 4.2 Deps entities tests | ✅ existe `requirements-test.txt` |
| 4.3 Tests middleware Go | ✅ existen `circuitbreaker_test.go`, `ratelimit_test.go`, `retry_test.go` |

---

## Orden de ejecución sugerido

1. **Task 0.1** (5min) — quick win, desbloquea rotateo de credenciales
2. **Task 1.2** (30min) — desbloquea compose en esta máquina
3. **Task 1.3** (20min) — desbloquea graceful shutdown real
4. **Task 0.2** (1h) — auth middleware
5. **Task 1.4** (10min) — bare excepts
6. **Task 1.5** (10min) — log job_id
7. **Task 1.1** (varía) — CI

**Tiempo total estimado**: ~3.5h para P0 + P1. P2 son ~6h más (no bloqueantes).
