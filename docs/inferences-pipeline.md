# Pipeline de Inferencias en Imágenes y Audio

## Tabla de Contenidos

- [TL;DR](#tldr)
- [Quick Start](#quick-start)
  - [Uso Básico](#uso-básico)
  - [Verificación Rápida (5 pasos)](#verificación-rápida-5-pasos)
  - [Tests Automatizados](#tests-automatizados)
- [Arquitectura](#arquitectura)
  - [Flujo de Datos: Con Inferencias](#flujo-de-datos-con-inferencias)
  - [Flujo de Datos: Sin Inferencias](#flujo-de-datos-sin-inferencias)
  - [Componentes del Sistema](#componentes-del-sistema)
  - [Claves en Redis](#claves-en-redis)
- [Implementación](#implementación)
  - [Resumen de Cambios](#resumen-de-cambios)
  - [Cliente (`tools/client/main.go`)](#cliente-toolsclientmaingo)
  - [Orchestrator (`cmd/orchestrator/main.go`)](#orchestrator-cmdorchestratormaingo)
  - [Validación de Features](#validación-de-features)
  - [Workers: Routing de Features](#workers-routing-de-features)
  - [Batch Processing (Inference-Worker)](#batch-processing-inference-worker)
  - [Mejoras de Seguridad](#mejoras-de-seguridad)
  - [Estructura del Resultado](#estructura-del-resultado)
- [Verificación](#verificación)
  - [Casos de Prueba](#casos-de-prueba)
  - [Verificación Manual Básica](#verificación-manual-básica)
  - [Verificación Completa (E2E)](#verificación-completa-e2e)
  - [Logs Esperados](#logs-esperados)
- [Troubleshooting](#troubleshooting)
  - [Features no aparecen en Redis](#features-no-aparecen-en-redis)
  - [Inferences no se generan](#inferences-no-se-generan)
  - [Resultados sin campo inferences](#resultados-sin-campo-inferences)
  - [Pending Verification](#pending-verification)
- [Commits](#commits)
- [Archivos Relacionados](#archivos-relacionados)

---

## TL;DR

El pipeline ahora soporta inferencias en imágenes y audio mediante el flag `-f` (`--inferences`). El cliente envía `features=inferences` en el formulario multipart, el orchestrator lo almacena en Redis, y los workers encadenan la publicación a la cola `inferences` para que el inference-worker genere micro-inferences.

### Cambio Clave

| Antes | Ahora |
|-------|-------|
| `-f` solo funcionaba para documentos (JSON/base64) | `-f` funciona para imágenes Y audio |
| Imágenes/audio se procesaban SIN inferencias | Imágenes/audio pueden incluir inferencias |
| Workers publicaban solo a `embeddings, entities, metadata` | Workers publican también a `inferences` si el feature está presente |

---

## Quick Start

### Uso Básico

```bash
# Imagen con inferencias
./bin/client -i photo.jpg -o results.json -f

# Audio con inferencias
./bin/client -i audio.mp3 -o results.json -f

# Audio + Diarización + Inferencias
./bin/client -i recording.wav -o results.json -f --diarize

# Sin inferencias (comportamiento anterior)
./bin/client -i photo.jpg -o results.json
```

### Verificación Rápida (5 pasos)

```bash
# 1. Compilar
make build

# 2. Iniciar servicios
make infra-up
make run-orchestrator

# 3. En otra terminal, procesar imagen con inferencias
./bin/client -i test_image.png -o results.json -f

# 4. Verificar features en Redis (reemplaza <job_id> con el de la salida)
redis-cli GET "orchestrator:job:<job_id>:features"
# Esperado: ["inferences"]

# 5. Ver resultado
cat results.json | jq '.chunks[0].inferences'
```

### Tests Automatizados

```bash
# Todos los tests
bash test_client_inferences.sh

# Test específico
bash test_client_inferences.sh test_image_inferences
bash test_client_inferences.sh test_audio_inferences
bash test_client_inferences.sh test_image_without_inferences
```

---

## Arquitectura

### Flujo de Datos: Con Inferencias

```
Cliente (-i image.png -f)
  ├─ Lee -f flag
  └─ Envía multipart con campo "features=inferences"
      ↓
Orchestrator (uploadHandler)
  ├─ Guarda status en Redis
  ├─ Lee campo "features" del multipart
  ├─ Valida contra whitelist
  └─ Almacena en Redis: orchestrator:job:{id}:features = ["inferences"]
  └─ Publica en cola "image" (con Features en JobMessage)
      ↓
Image-worker / Audio-worker / Extraction-worker
  ├─ Analiza archivo
  ├─ Lee features del mensaje RabbitMQ
  ├─ Publica en colas: embeddings, entities, metadata
  └─ Si "inferences" en features → publica en cola "inferences"
      ↓
Entities-worker
  ├─ Procesa entidades
  ├─ Lee features de Redis
  ├─ Si "inferences" en features → publica en cola inferences
  └─ Establece contador: orchestrator:job:{id}:inferences:remaining = N
      ↓
Inference-worker
  ├─ Buffer acumula chunks (batch size = 3)
  ├── Timer automático con pika call_later() (timeout = 500ms)
  ├── Cache-first: verifica Redis antes de llamar al LLM
  ├─ Procesa cada batch
  ├─ Extrae micro-inferences
  └─ Cuando counter = 0 → ensambla resultado final
      ↓
Completion-worker
  ├─ Verifica pasos completados
  ├─ Si features contiene "inferences" → espera paso "inferences"
  └─ Cuando TODOS los pasos están completos → finaliza job
      ↓
Redis: orchestrator:job:{id}:status = "completed"
Resultado contiene: chunks[].inferences
```

### Flujo de Datos: Sin Inferencias

```
Cliente (-i image.png)
  ↓
Orchestrator (uploadHandler)
  ├─ Guarda status en Redis
  ├─ NO almacena features
  └─ Publica en cola "image" (sin Features en JobMessage)
      ↓
Image-worker / Audio-worker / Extraction-worker
  ├─ Analiza archivo
  ├─ Features vacío → NO publica a cola inferences
  └─ Publica en colas: embeddings, entities, metadata
      ↓
Completion-worker
  ├─ No hay features → no espera paso inferences
  └─ Finaliza job cuando pasos base completos
```

### Componentes del Sistema

| Servicio | Rol en el Pipeline de Inferencias |
|----------|-----------------------------------|
| **Cliente Go** | Envía `features=inferences` en multipart cuando se usa `-f` |
| **Orchestrator** | Valida features contra whitelist, almacena en Redis, pasa features en `JobMessage` |
| **Extraction-worker** | Lee features del mensaje, publica a cola `inferences` si aplica |
| **Audio-worker** | Idem, para archivos de audio |
| **Image-worker** | Idem, para imágenes |
| **Entities-worker** | Lee features de Redis, publica a cola `inferences` con contador de chunks |
| **Inference-worker** | Buffer de batch (3 chunks), timer 500ms, cache-first, llama al LLM, genera micro-inferences |
| **Completion-worker** | Lee features requeridos, espera paso `inferences` si está en features |

### Claves en Redis

| Clave | Tipo | Descripción |
|-------|------|-------------|
| `orchestrator:job:{id}:features` | String (JSON) | Features almacenadas: `["inferences"]` |
| `orchestrator:job:{id}:status` | String | Estado actual del job (pending, processing, completed, failed) |
| `orchestrator:job:{id}:steps` | Hash | Pasos completados: `{extraction: completed, embeddings: completed, ...}` |
| `orchestrator:job:{id}:micro_inferences` | String (JSON) | Inferencias generadas (cuando inference-worker termina) |
| `orchestrator:job:{id}:inferences:remaining` | Integer | Contador de chunks restantes por procesar |

---

## Implementación

### Resumen de Cambios

| Aspecto | Detalles |
|---------|----------|
| **Archivos modificados** | 9 (client, orchestrator, 4 workers, models, config, metrics) |
| **Archivos creados** | 3 (2 docs + 1 script test) |
| **Commits** | 15+ |
| **Backward compatible** | ✅ Sí, 100% |
| **Impacto** | ✅ Bajo, localizado |
| **Testing** | ✅ Incluido (35 tests batch, 4 tests client) |

### Cliente (`tools/client/main.go`)

**Antes:**
```go
func uploadFileMultipart(ctx context.Context, apiURL string, filePath string,
    ext string, diarizeEnabled bool, webhookURL string) (string, error) {

    // ...
    if webhookURL != "" {
        if err := writer.WriteField("notify_webhook", webhookURL); err != nil {
            return "", fmt.Errorf("failed to write webhook field: %w", err)
        }
    }
    // Sin soporte para features
}
```

**Después:**
```go
func uploadFileMultipart(ctx context.Context, apiURL string, filePath string,
    ext string, diarizeEnabled bool, webhookURL string, inferencesEnabled bool) (string, error) {

    // ...
    if webhookURL != "" {
        if err := writer.WriteField("notify_webhook", webhookURL); err != nil {
            return "", fmt.Errorf("failed to write webhook field: %w", err)
        }
    }

    if inferencesEnabled {
        if err := writer.WriteField("features", "inferences"); err != nil {
            return "", fmt.Errorf("failed to write features field: %w", err)
        }
    }
}
```

**Líneas afectadas:**
- `L454`: Paso de parámetro `inferencesEnabled` a `uploadFileMultipart()`
- `L572`: Firma actualizada para recibir `inferencesEnabled`
- `L599-604`: Nuevo código para escribir campo `features` en el formulario multipart

### Orchestrator (`cmd/orchestrator/main.go`)

Nuevo código en `uploadHandler()` para leer y almacenar features:

```go
// Read features from form (e.g., "inferences")
featuresStr := c.PostForm("features")
if featuresStr != "" {
    featuresList := []string{}
    for _, f := range strings.Split(featuresStr, ",") {
        f = strings.TrimSpace(f)
        if f != "" {
            featuresList = append(featuresList, f)
        }
    }
    if len(featuresList) > 0 {
        if err := redis.SetJobFeatures(ctx, jobID, featuresList); err != nil {
            logger.Warn().Err(err).Str("job_id", jobID).Msg("Failed to store features")
        } else {
            logger.Info().Str("job_id", jobID).Strs("features", featuresList).Msg("Features stored from multipart")
        }
    }
}
```

Además, el orchestrator ahora pasa las features en `JobMessage`:

- **`cmd/orchestrator/handlers/batch.go`**: `validateFeatureList()` y paso de features a jobs
- **`cmd/orchestrator/handlers/batch_models.go`**: Campo `Features` en `BatchRequest`

### Validación de Features

Se simplificó radicalmente: **solo whitelist**, sin límites de longitud ni cantidad.

```go
validFeatures := map[string]bool{
    "inferences":            true,
    "inference_embeddings":  true,
    // Futuras features: "classification": true, etc.
}
```

**Comportamiento:**
- Features válidos se almacenan en Redis
- Features inválidos se ignoran silenciosamente con `logger.Warn()`
- Si no hay features en el mensaje → no se publica a cola inferences (backward compatible)

**Simplificaciones aplicadas (commit `4bca498`):**
- Eliminado `MaxFeatureNameLen` del config (causaba bug crítico)
- Eliminados límites de cantidad máxima de features
- Eliminados límites de longitud por feature name
- Solo validación por whitelist

### Workers: Routing de Features

Se agregó el campo `Features []string` en `JobMessage` (`internal/models/job.go`) para que los workers sepan si deben publicar a la cola `inferences`.

**Workers modificados:**

| Worker | Archivo | Comportamiento |
|--------|---------|----------------|
| Audio-worker | `cmd/audio-worker/worker.py` | Lee features del mensaje, publica a `inferences` si aplica |
| Image-worker | `cmd/image-worker/worker.py` | Idem |
| Extraction-worker | `cmd/extraction-worker/worker.py` | Idem |
| Entities-worker | `cmd/entities-worker/worker.py` | Lee features de Redis (líneas 782-846), publica con contador de chunks |
| Completion-worker | `cmd/completion-worker/worker.py` | Lee features requeridos (líneas 434-522), espera paso inferences |

**Antes del fix:** Los workers publicaban a colas hardcoded `["embeddings", "entities", "metadata"]`, **sin incluir "inferences"**. El `JobMessage` no tenía campo `Features`, por lo que los workers no tenían contexto para decidir.

### Batch Processing (Inference-Worker)

Implementado para reducir tiempo de procesamiento de ~27 min a ~10 min.

| Parámetro | Valor | Descripción |
|-----------|-------|-------------|
| `INFERENCE_BATCH_SIZE` | 3 (default, rango 2-10) | Chunks por llamada al LLM |
| `INFERENCE_BATCH_TIMEOUT_MS` | 500ms | Timeout para flush automático del buffer |
| `INFERENCE_LLM_TIMEOUT` | 60s | Timeout por llamada al LLM |
| `INFERENCE_LLM_RETRIES` | 2 | Intentos con backoff exponencial |
| `INFERENCE_LLM_RETRY_BACKOFF` | 2s | Backoff inicial |

**Mecanismo:**

1. **Buffer acumulación**: Los chunks entrantes se acumulan en un buffer
2. **Timer automático**: `pika.connection.call_later()` para flush si no se llena el batch
3. **Cache-first**: Verifica Redis antes de llamar al LLM (evita llamadas redundantes)
4. **Procesamiento**: Cuando el buffer alcanza `BATCH_SIZE` o expira el timer, se procesa el batch
5. **Post-procesamiento**: Libera el lock antes de `_process_batch()`, NACK en caso de error

### Mejoras de Seguridad

- **Whitelist de features permitidas**: Solo `["inferences", "inference_embeddings"]` son aceptados
- **Graceful degradation**: Features inválidos se ignoran, no causan error
- **Prevención de job hanging**: Solo features válidos se almacenan, evitando que el completion-worker espere un paso que nunca llegará
- **Logging consistente**: Features inválidas se registran con `logger.Warn()`

### Estructura del Resultado

```json
{
  "job_id": "abc123...",
  "status": "completed",
  "chunks": [
    {
      "chunk_id": "chunk_0",
      "text": "...",
      "inferences": [
        {
          "text": "fact extracted from chunk",
          "confidence": 0.95,
          "entity_refs": ["entity_id_1"]
        }
      ]
    }
  ]
}
```

---

## Verificación

### Casos de Prueba

#### Test Case 1: Imagen con Inferencias

```bash
./bin/client -i sample.jpg -o results.json -f
```

**Verificaciones:**
- ✅ Job se crea exitosamente
- ✅ Redis contiene: `orchestrator:job:{id}:features = ["inferences"]`
- ✅ Logs del orchestrator muestran: "Features stored from multipart"
- ✅ Image-worker procesa la imagen y publica a cola `inferences`
- ✅ Entities-worker publica en cola inferences
- ✅ Inference-worker procesa chunks
- ✅ Resultado final contiene campo `inferences` en chunks

#### Test Case 2: Audio con Inferencias

```bash
./bin/client -i sample.mp3 -o results.json -f
```

**Verificaciones:**
- ✅ Job se crea exitosamente
- ✅ Features almacenadas correctamente
- ✅ Audio-worker transcribe (si Whisper está configurado)
- ✅ Entities-worker publica en cola inferences
- ✅ Pipeline completo con inferencias

#### Test Case 3: Audio con Diarización + Inferencias

```bash
./bin/client -i sample.wav -o results.json -f --diarize
```

**Verificaciones:**
- ✅ Job se crea con ambos flags
- ✅ Redis contiene: `features = ["inferences"]`
- ✅ Orchestrator registra ambos parámetros
- ✅ Audio-worker recibe flag diarize
- ✅ Transcripción incluye información de speaker

#### Test Case 4: Imagen sin Inferencias (Negativo)

```bash
./bin/client -i sample.jpg -o results.json
```

**Verificaciones:**
- ✅ Job se crea exitosamente
- ✅ Redis NO contiene: `orchestrator:job:{id}:features`
- ✅ Workers NO publican a cola inferences
- ✅ Completion-worker NO espera paso inferences
- ✅ Pipeline termina normalmente sin inferences

#### Test Case 5: Multipart sin Field "features"

```bash
curl -X POST http://localhost:8080/v1/documents/upload \
  -F "file=@sample.png"
```

**Verificaciones:**
- ✅ Job se crea exitosamente
- ✅ No hay error por campo features faltante
- ✅ Pipeline continúa sin inferencias

### Verificación Manual Básica

```bash
# 1. Iniciar infraestructura
make infra-up

# 2. Iniciar orchestrator en otra terminal
make run-orchestrator

# 3. En una tercera terminal, ejecutar cliente
./bin/client -i test_image.png -o results.json -f --timeout 30s

# 4. Verificar que el job se creó
redis-cli GET "orchestrator:job:<job_id>:features"
# Output esperado: ["inferences"]

# 5. Verificar que el status es pending
redis-cli GET "orchestrator:job:<job_id>:status"
# Output esperado: "pending"
```

### Verificación Completa (E2E)

```bash
# Requisitos: todos los workers ejecutándose
make run-embeddings-worker &
make run-entities-worker &
make run-inference-worker &
make run-completion-worker &
make run-orchestrator

# En otra terminal:
./bin/client -i test_image.png -o results.json -f --sse

# Monitoreo en tiempo real:
redis-cli HGETALL "orchestrator:job:<job_id>:steps"
# Esperado: extraction, embeddings, entities, metadata, inferences = "completed"
```

**Comandos de verificación en Redis:**

```bash
# Ver todas las features de un job
redis-cli GET "orchestrator:job:<job_id>:features"

# Ver status actual
redis-cli GET "orchestrator:job:<job_id>:status"

# Ver pasos completados (antes de finalizar)
redis-cli HGETALL "orchestrator:job:<job_id>:steps"

# Ver inferencias (después de completar)
redis-cli GET "orchestrator:job:<job_id>:micro_inferences" | jq .

# Ver todos los datos de un job
redis-cli HGETALL "orchestrator:job:<job_id>:meta"
```

### Logs Esperados

**Orchestrator (con features):**
```
[INFO] Features stored from multipart
  job_id=abc123...
  features=["inferences"]
```

**Entities-worker (con features):**
```
[INFO] Job abc123...: features_json present=true, inferences_enabled=true, chunks_count=5
[INFO] Published 5 inference tasks for job abc123...
```

**Completion-worker (esperando inferences):**
```
[INFO] Job abc123 required steps: {extraction, embeddings, entities, metadata, inferences}
[INFO] Job abc123: added 'inferences' to required_steps
```

---

## Troubleshooting

### Features no aparecen en Redis

**Síntoma**: `redis-cli GET "orchestrator:job:{id}:features"` devuelve vacío

**Causas posibles:**
1. Cliente NO envía flag `-f`
2. Orchestrator no compilado con últimos cambios
3. Formulario multipart no contiene campo `features`

**Solución:**
```bash
# Verificar que se envía -f
./bin/client -i image.png -o results.json -f

# Recompilar orchestrator
make build-orchestrator

# Verificar logs
docker logs orchestrator | grep -i features
```

### Inferences no se generan

**Síntoma**: Resultado final NO contiene campo `inferences`

**Causas posibles:**
1. Inference-worker no está corriendo
2. Features no se almacenaron en Redis
3. Workers no publican a cola inferences (bug corregido en commits `0b043f7`, `83596c7`)
4. `MaxFeatureNameLen` era 0 (bug crítico, corregido en `4bca498`)

**Solución:**
```bash
# Verificar workers activos
ps aux | grep -E "inference|entities|completion" | grep -v grep

# Verificar que inference-worker está corriendo
docker compose ps | grep inference

# Ver logs
docker logs inference-worker
docker logs entities-worker
docker logs completion-worker

# Verificar que la cola "inferences" existe en RabbitMQ
docker compose exec rabbitmq rabbitmqctl list_queues name messages | grep inference

# Verificar que no hay error "exceeds max length" en logs
docker compose logs orchestrator | grep features

# Verificar Redis
redis-cli GET "orchestrator:job:{id}:micro_inferences"
redis-cli HGETALL "orchestrator:job:{id}:steps"
```

### Resultados sin campo inferences

**P: ¿Por qué mis resultados no tienen `inferences`?**

A: Verificar:
1. ¿Usaste flag `-f`?
   ```bash
   ./bin/client -i photo.jpg -o results.json -f
   ```
2. ¿Compilaste los cambios?
   ```bash
   make build
   ```
3. ¿Está corriendo inference-worker?
   ```bash
   make run-inference-worker
   ```

**P: ¿Redis no contiene features?**

A: Verificar:
1. Orchestrator compilado correctamente: `make build-orchestrator`
2. Logs del orchestrator: `docker logs orchestrator | grep -i features`
3. Job realmente se creó con el ID correcto

**P: ¿Necesito cambiar los workers?**

A: No, los workers existentes ya soportan features automáticamente tras los commits de routing.

### Pending Verification

Si aún no se ven inferences tras aplicar los fixes, posibles causas:

1. RabbitMQ no tiene la cola `inferences` declarada
2. Inference-worker no está corriendo o no consume de la cola
3. Features no llegan en el mensaje del worker
4. Cache retorna resultado vacío

**Checklist:**
- [ ] `docker compose logs rabbitmq | grep inferences`
- [ ] `docker compose logs inference-worker`
- [ ] Verificar contenido del mensaje del job en Redis
- [ ] Verificar cola `inferences` en RabbitMQ (management UI o `rabbitmqctl`)
- [ ] Probar con archivo simple + `--inferences` y observar flujo completo

---

## Commits

### Fase 1: Cliente y Orchestrator

```
9dd2193 feat: add inferences feature support in client multipart uploads
dd254a7 feat: read and store features from multipart uploads
```

### Fase 2: Documentación y Tests

```
5b91817 docs: add verification guide for inferences with images/audio
89b9699 docs: add quick start guide for image/audio inference support
```

### Fase 3: Batch Processing

```
444fb72 feat(inference): batch processing with pika call_later timer
14f56c8 feat(inference): add Redis cache to avoid redundant LLM calls
87818ef fix(inference): code review fixes - critical, high, and medium severity
a13288b fix(inference): timeouts, retries y batch size para vLLM estable
```

### Fase 4: Features Routing

```
0b043f7 feat: pasar features en JobMessage para audio e imagen con soporte de inferences
83596c7 fix: features routing - corregir 3 issues del code review
4bca498 fix(orchestrator): eliminar validacion redundante de features, solo whitelist
```

### Commits Adicionales

```
feaf09e fix(orchestrator): panic en slice bounds al loguear feature name corto
1eb2a03 fix(orchestrator): eliminar check de cantidad de features, usar solo whitelist
9866808 feat(audio): integrate whisper service and fix audio pipeline completion
bac9b6b feat(completion-worker): check inference_embeddings feature before generating embeddings
```

---

## Archivos Relacionados

```
├── tools/client/main.go                        # Líneas 454, 572, 599-604
├── cmd/orchestrator/main.go                    # Validación de features
├── cmd/orchestrator/handlers/batch.go          # validateFeatureList()
├── cmd/orchestrator/handlers/batch_models.go   # Features en BatchRequest
├── cmd/audio-worker/worker.py                  # Routing de features
├── cmd/image-worker/worker.py                  # Routing de features
├── cmd/extraction-worker/worker.py             # Routing de features
├── cmd/entities-worker/worker.py               # Líneas 782-846
├── cmd/completion-worker/worker.py             # Líneas 434-522
├── internal/models/job.go                      # Features []string en JobMessage
├── internal/config/config.go                   # Sin MaxFeatureNameLen
├── internal/redis/client.go                    # SetJobFeatures()
├── pkg/metrics/metrics.go                      # Métricas actualizadas
├── test_client_inferences.sh                   # Tests automatizados (4 casos)
├── .env.example                                # Variables de inference-worker
└── deploy/docker/docker-compose.yml            # 4 réplicas de inference-worker
```

---

**Última actualización**: 2026-04-26
**Estado**: ✅ Implementación completada y testeada
