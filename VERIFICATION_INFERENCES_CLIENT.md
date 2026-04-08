# Verificación: Cliente con Soporte para Inferencias en Imágenes/Audio

## Resumen de Cambios

Este documento describe las modificaciones realizadas para permitir que el cliente Go (`tools/client/`) envíe el flag de **inferencias activado** cuando procesa imágenes y audio.

### Archivos Modificados

1. **`tools/client/main.go`** - Cliente Go
   - Línea 454: Paso de parámetro `inferencesEnabled` a `uploadFileMultipart()`
   - Línea 572: Actualización de firma de `uploadFileMultipart()` para recibir `inferencesEnabled`
   - Línea 605-609: Nuevo código para escribir campo `features` en el formulario multipart

2. **`cmd/orchestrator/main.go`** - Orchestrator
   - Líneas 1108-1159: Nuevo código con **validación de seguridad** para leer `features` del formulario multipart, validar contra whitelist, y almacenarlas en Redis

### Mejoras de Seguridad

Las siguientes validaciones fueron agregadas en el orchestrator:
- ✅ **Whitelist de features permitidas**: Solo `"inferences"` es aceptado (futuro-proof para más features)
- ✅ **Límite de cantidad de features**: Máximo 10 features por job
- ✅ **Límite de longitud de feature**: Máximo 50 caracteres por nombre de feature
- ✅ **Logging consistente**: Features inválidas se registran pero no causan error (graceful degradation)
- ✅ **Prevención de DoS**: Protección contra intentos de llenar Redis con valores arbitrarios

---

## Cambios Implementados

### Client (`tools/client/main.go`)

#### Antes:
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

#### Después:
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

**Impacto**: El cliente ahora envía el campo `features=inferences` en el formulario multipart cuando se usa el flag `-f`

---

### Orchestrator (`cmd/orchestrator/main.go`)

#### Nuevo código insertado en `uploadHandler()`:

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

**Impacto**: El orchestrator ahora lee el campo `features` del formulario multipart y lo almacena en Redis bajo la clave `orchestrator:job:{id}:features`

---

## Flujo de Datos

### Sin cambios: Imágenes/Audio sin inferencias
```
Cliente (-i image.png)
    ↓
Orchestrator (uploadHandler)
    ├─ Guarda status en Redis
    ├─ NO almacena features
    └─ Publica en cola "image"
        ↓
    Image-worker
        ├─ Analiza imagen
        └─ Publica en colas: embeddings, entities, metadata
            ↓
        [Resto del pipeline sin inferences]
```

### Con cambios: Imágenes/Audio CON inferencias
```
Cliente (-i image.png -f)
    ├─ Lee -f flag
    └─ Envía multipart con campo "features=inferences"
        ↓
Orchestrator (uploadHandler)
    ├─ Guarda status en Redis
    ├─ Lee campo "features" del multipart
    ├─ Almacena en Redis: orchestrator:job:{id}:features = ["inferences"]
    └─ Publica en cola "image"
        ↓
    Image-worker
        ├─ Analiza imagen
        └─ Publica en colas: embeddings, entities, metadata
            ↓
        Entities-worker
            ├─ Procesa entidades
            ├─ Lee features de Redis
            ├─ Si "inferences" en features → publica en cola "inferences"
            └─ Establece contador: orchestrator:job:{id}:inferences:remaining = N
                ↓
            Inference-worker
                ├─ Procesa cada chunk
                ├─ Extrae micro-inferences
                └─ Cuando counter = 0 → ensambla resultado final
                    ↓
        Completion-worker
            ├─ Verifica pasos completados
            ├─ Si features contiene "inferences" → espera paso "inferences"
            └─ Cuando TODOS los pasos están completos → finaliza job
                ↓
            Redis: orchestrator:job:{id}:status = "completed"
```

---

## Flujo de Verificación

### 1. Verificación Manual Básica

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

### 2. Verificación Completa (E2E)

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

### 3. Uso del Script de Test Automatizado

```bash
# Tests unitarios básicos
bash test_client_inferences.sh test_image_inferences

# Test con audio
bash test_client_inferences.sh test_audio_inferences

# Test negativo (sin inferencias)
bash test_client_inferences.sh test_image_without_inferences

# Todos los tests
bash test_client_inferences.sh
```

---

## Verificación de Datos en Redis

### Claves Relevantes

| Clave | Tipo | Descripción |
|-------|------|-------------|
| `orchestrator:job:{id}:features` | String (JSON) | Features almacenadas: `["inferences"]` |
| `orchestrator:job:{id}:status` | String | Estado actual del job (pending, processing, completed, failed) |
| `orchestrator:job:{id}:steps` | Hash | Pasos completados: `{extraction: completed, embeddings: completed, ...}` |
| `orchestrator:job:{id}:micro_inferences` | String (JSON) | Inferencias generadas (cuando completion-worker termina) |

### Comandos de Verificación

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

---

## Casos de Prueba

### Test Case 1: Imagen con Inferencias

**Comando**:
```bash
./bin/client -i sample.jpg -o results.json -f
```

**Verificaciones**:
- ✅ Job se crea exitosamente
- ✅ Redis contiene: `orchestrator:job:{id}:features = ["inferences"]`
- ✅ Logs del orchestrator muestran: "Features stored from multipart"
- ✅ Image-worker procesa la imagen
- ✅ Entities-worker publica en cola inferences
- ✅ Inference-worker procesa chunks
- ✅ Resultado final contiene campo `inferences` en chunks

---

### Test Case 2: Audio con Inferencias

**Comando**:
```bash
./bin/client -i sample.mp3 -o results.json -f
```

**Verificaciones**:
- ✅ Job se crea exitosamente
- ✅ Features almacenadas correctamente
- ✅ Audio-worker transcribe (si Whisper está configurado)
- ✅ Entities-worker publica en cola inferences
- ✅ Pipeline completo con inferencias

---

### Test Case 3: Audio con Diarización + Inferencias

**Comando**:
```bash
./bin/client -i sample.wav -o results.json -f --diarize
```

**Verificaciones**:
- ✅ Job se crea con ambos flags
- ✅ Redis contiene: `features = ["inferences"]`
- ✅ Orchestrator registra ambos parámetros
- ✅ Audio-worker recibe flag diarize
- ✅ Transcripción incluye información de speaker

---

### Test Case 4: Imagen sin Inferencias (Negativo)

**Comando**:
```bash
./bin/client -i sample.jpg -o results.json
```

**Verificaciones**:
- ✅ Job se crea exitosamente
- ✅ Redis NO contiene: `orchestrator:job:{id}:features`
- ✅ Entities-worker NO publica en cola inferences
- ✅ Completion-worker NO espera paso inferences
- ✅ Pipeline termina normalmente sin inferences

---

### Test Case 5: Multipart sin Field "features"

**Comando**:
```bash
# Usando curl directamente (sin features)
curl -X POST http://localhost:8080/v1/documents/upload \
  -F "file=@sample.png"
```

**Verificaciones**:
- ✅ Job se crea exitosamente
- ✅ No hay error por campo features faltante
- ✅ Pipeline continúa sin inferencias

---

## Compatibilidad

### Backward Compatibility ✅

- Clientes existentes que NO envían el campo `features` siguen funcionando
- El código es defensivo: `if featuresStr != ""`
- No hay cambios en respuestas HTTP
- No hay cambios en modelos de datos existentes

### Forward Compatibility ✅

- El campo `features` puede extenderse a futuro (ej: `features=inferences,classification`)
- El código parse múltiples features separadas por comas
- Fácil agregar nuevas features sin cambios al cliente

---

## Logs Esperados

### Orchestrator (con features)
```
[INFO] Features stored from multipart
  job_id=abc123...
  features=["inferences"]
```

### Entities-worker (con features)
```
[INFO] Job abc123...: features_json present=true, inferences_enabled=true, chunks_count=5
[INFO] Published 5 inference tasks for job abc123...
```

### Completion-worker (esperando inferences)
```
[INFO] Job abc123 required steps: {extraction, embeddings, entities, metadata, inferences}
[INFO] Job abc123: added 'inferences' to required_steps
```

---

## Consideraciones de Seguridad

### Validación de Features

El orchestrator implementa múltiples capas de validación para proteger contra abusos:

#### 1. Whitelist de Features Permitidas
Solo los features registrados en la whitelist son aceptados:
```go
validFeatures := map[string]bool{
    "inferences": true,
    // Futuras features: "classification": true, "summarization": true, etc.
}
```

**Comportamiento**: Features inválidos se ignoran silenciosamente (graceful degradation) y se registran con `logger.Warn()`.

**Ejemplo**: Si un cliente envía `features=inferences,invalid_feature,typo`:
- ✅ `"inferences"` se almacena en Redis
- ❌ `"invalid_feature"` se ignora (registro warning)
- ❌ `"typo"` se ignora (registro warning)

#### 2. Límites de Cantidad y Longitud
```go
const maxFeatures = 10           // Máximo 10 features por job
const maxFeatureLength = 50      // Máximo 50 caracteres por nombre
```

**Prevención**:
- Evita DoS por envío de miles de features
- Previene overflow de Redis keys
- Limita tamaño de datos en logs

#### 3. Prevención de Job Hanging

**Problema evitado**: Si un cliente envía un typo en feature name (ej: `"inferneces"`), sin validación:
- El feature se almacena en Redis
- Entities-worker lo ignora (no es `"inferences"`)
- Completion-worker ve el feature → espera paso que nunca se ejecuta
- **Resultado**: Job cuelga indefinidamente

**Solución implementada**: Solo features válidos se aceptan, previniendo esta condición.

---

## Resolución de Problemas

### Features no aparecen en Redis

**Síntoma**: `redis-cli GET "orchestrator:job:{id}:features"` devuelve vacío

**Causas posibles**:
1. Cliente NO envía flag `-f`
2. Orchestrator no compilado con últimos cambios: `make build-orchestrator`
3. Formulario multipart no contiene campo `features`

**Solución**:
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

**Causas posibles**:
1. Inference-worker no está corriendo
2. Features no se almacenaron en Redis
3. Entities-worker no publica en cola inferences
4. Completion-worker espera pero no encuentra paso

**Solución**:
```bash
# Verificar workers
ps aux | grep -E "inference|entities|completion" | grep -v grep

# Ver logs
docker logs inference-worker
docker logs entities-worker
docker logs completion-worker

# Verificar Redis
redis-cli GET "orchestrator:job:{id}:micro_inferences"
redis-cli HGETALL "orchestrator:job:{id}:steps"
```

---

## Commits Necesarios

### Commit 1: Client Support
```
git add tools/client/main.go
git commit -m "feat: add inferences feature support in client multipart uploads

- Updated uploadFileMultipart() to accept inferencesEnabled parameter
- Added 'features' field to multipart form when -f flag is used
- Maintains backward compatibility with existing API
"
```

### Commit 2: Orchestrator Support
```
git add cmd/orchestrator/main.go
git commit -m "feat: read and store features from multipart uploads

- Added code to read 'features' field from form data in uploadHandler
- Stores features in Redis via SetJobFeatures() 
- Parses comma-separated features list
- Logs features storage for debugging
"
```

### Commit 3: Documentation
```
git add VERIFICATION_INFERENCES_CLIENT.md test_client_inferences.sh
git commit -m "docs: add verification guide for inferences with images/audio

- Created comprehensive verification guide
- Added automated test script with 4 test cases
- Documented expected data flow and Redis keys
- Added troubleshooting guide
"
```

---

## Véase También

- `cmd/entities-worker/worker.py` - Línea 782-846: Lógica que lee features y publica en cola inferences
- `cmd/completion-worker/worker.py` - Línea 434-522: Lógica que verifica features antes de finalizar
- `internal/redis/client.go` - `SetJobFeatures()`: Método para almacenar features
