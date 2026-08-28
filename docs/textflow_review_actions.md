# textFlow: revisión de infraestructura y acciones pendientes

**Fecha:** 2026-08-28  
**Repositorio:** `sudebaker/textFlow`  
**Rama revisada:** `main`  
**Commit revisado:** `c6e21b3`

## Objetivo

Este documento recoge la revisión de la implementación actual de la infraestructura de textFlow y las acciones recomendadas antes de considerar esta fase cerrada.

La conclusión general es positiva:

> La arquitectura actual es sólida y no requiere una reescritura. Hay varios puntos concretos que conviene corregir o completar.

---

# Resumen de prioridades

| Prioridad | Tema | Estado |
|---|---|---|
| P0 | Metadata cuando la entrada es `document_url` | **BUG** |
| P1 | Cancelación cooperativa real en workers | **INCOMPLETO** |
| P1 | Garbage Collection del Artifact Store | **PENDIENTE** |
| P1 | Cache del image-analyzer no considera `prompt` | **BUG POTENCIAL / CONTRATO AMBIGUO** |
| P2 | `stage.queued` no parece emitirse realmente | **INCOMPLETO** |
| P2 | Evitar segunda lectura completa del documento | **MEJORA** |
| P2 | Dashboard Grafana P50/P95 | **PENDIENTE** |
| P3 | Semántica de `balanced` vs `full` | **DECISIÓN DE DISEÑO** |
| -- | GPU scheduler | **NO TOCAR TODAVÍA** |
| -- | TTFT/TPOT | **NO FORZAR CON OLLAMA** |

---

# 1. P0: corregir metadata cuando la entrada es `document_url`

## Problema

En `cmd/extraction-worker/worker.py`, `_process_message_async()` diferencia entre:

```python
if body.get("document_path"):
    ...
else:
    ...
```

El `else` utiliza:

```python
base64.b64decode(body.get("document_base64", ""))
```

El problema aparece cuando el job utiliza `document_url`.

El flujo esperado es:

```text
document_url
    ↓
descarga del documento
    ↓
Docling
    ↓
metadata
```

Pero el código de metadata puede terminar creando un fichero temporal a partir de:

```text
document_base64 = ""
```

y, por tanto, trabajar sobre un fichero vacío.

## Acción

Revisar el flujo de ingestión y garantizar que para `document_url`:

1. se descarga el documento una única vez;
2. los bytes descargados se conservan/reutilizan;
3. metadata recibe el fichero real o los bytes reales;
4. SHA-256, tamaño y MIME corresponden al documento descargado.

No cambiar el contrato público si no es necesario.

## Criterio de aceptación

Un job enviado mediante `document_url` debe producir:

- texto correcto;
- metadata correcta;
- tamaño correcto;
- SHA-256 correcto;
- ningún fichero temporal vacío utilizado para metadata.

Añadir test específico para URL.

---

# 2. P1: completar cancelación cooperativa real

## Situación actual

La API permite:

```http
POST /v1/documents/{id}/cancel
```

y establece el estado:

```text
cancelled
```

Eso está correctamente planteado.

El problema es que marcar Redis como `cancelled` no implica necesariamente que un worker que ya está procesando una operación larga se detenga.

Especialmente relevante para:

- Docling
- Whisper
- GLiNER
- embeddings
- llamadas LLM
- procesamiento multimodal

## Acción

Añadir una abstracción común, por ejemplo:

```python
is_job_cancelled(job_id) -> bool
```

en `worker_common`.

Los workers deberían comprobar cancelación en puntos seguros:

```text
inicio del stage
        ↓
antes de operaciones caras
        ↓
entre batches
        ↓
antes de publicar el siguiente stage
        ↓
finalización
```

No intentar matar violentamente threads o procesos CUDA en mitad de una operación. La cancelación debe ser cooperativa.

## Criterio de aceptación

Si un job se cancela:

1. los workers detectan el estado;
2. no empiezan stages nuevos;
3. interrumpen el procesamiento en puntos seguros;
4. no publican stages posteriores;
5. el job permanece `cancelled`;
6. no termina accidentalmente como `completed`.

Añadir tests para al menos un worker síncrono y uno asíncrono.

---

# 3. P1: Artifact Store necesita Garbage Collection

## Situación actual

El Artifact Store utiliza almacenamiento content-addressed mediante SHA-256.

Conceptualmente:

```text
sha256
  ↓
artifact
```

Esto es correcto y debe mantenerse.

El problema aparece al borrar jobs.

Eliminar las referencias Redis no implica necesariamente eliminar el fichero físico del Artifact Store.

No se puede simplemente borrar el artifact al borrar un job porque un mismo artifact puede estar referenciado por varios jobs.

## Acción

Implementar GC basado en reachability:

```text
Redis / referencias de jobs
          ↓
     live artifacts
          ↓
   comparar con store
          ↓
 eliminar unreachable
```

Recomendación:

- no borrar inmediatamente;
- aplicar una edad mínima;
- permitir ejecución periódica;
- registrar número de artifacts eliminados y bytes recuperados.

Ejemplo conceptual:

```text
GC:
  collect live SHA256 refs
  scan artifact store
  ignore recent artifacts
  delete unreachable old artifacts
  report reclaimed bytes
```

## Criterio de aceptación

Debe ser imposible eliminar un artifact todavía referenciado por otro job.

Debe ser posible recuperar espacio de artifacts huérfanos.

---

# 4. P1: corregir cache del image-analyzer

## Problema

Actualmente la cache utiliza conceptualmente:

```text
image:<sha256(image)>
```

pero el endpoint acepta un `prompt`.

Si el prompt modifica el comportamiento del análisis:

```text
imagen X + prompt A
        ↓
resultado A
        ↓
cache image:X

imagen X + prompt B
        ↓
cache image:X
        ↓
resultado A
```

Eso sería incorrecto.

## Decisión recomendada

Hay dos opciones:

### Opción A: preferida si el servicio es exclusivamente OCR

Eliminar `prompt` del contrato público y mantener el servicio estrictamente dedicado a:

```text
imagen → texto visible
```

### Opción B

Si `prompt` debe permanecer, la clave debe incluir al menos:

```text
image hash
prompt
model
preprocessing/version
```

Por ejemplo:

```text
sha256(image_bytes + prompt + model + preprocessing_version)
```

## Criterio de aceptación

Dos prompts diferentes nunca pueden devolver accidentalmente el resultado cacheado del otro.

---

# 5. P2: `stage.queued`

El contrato contempla:

```text
stage.queued
stage.started
stage.completed
stage.failed
```

Los eventos `stage.started` y `stage.completed` están integrados.

Revisar si `stage.queued` se emite realmente antes de publicar el mensaje a RabbitMQ.

## Acción

Si el evento forma parte del contrato:

```text
stage.queued
      ↓
RabbitMQ
      ↓
stage.started
```

debe emitirse de forma consistente.

Si deliberadamente no se quiere implementar, eliminarlo del contrato/documentación para evitar una falsa garantía.

---

# 6. P2: evitar segunda lectura completa del documento

Actualmente el flujo puede leer el documento completo para procesamiento y posteriormente volver a abrirlo para metadata/hash.

Conceptualmente:

```text
read file → Docling

read file → SHA256 / metadata
```

Esto duplica I/O y puede incrementar presión de memoria.

## Acción

Cuando sea sencillo:

- calcular SHA-256 durante la ingestión;
- conservar tamaño/MIME;
- reutilizar esa información;
- evitar una segunda lectura completa.

Preferir hashing streaming para ficheros grandes si se necesita recalcular:

```python
hash.update(chunk)
```

No convertir esto en prioridad de rendimiento hasta tener datos de benchmark que justifiquen el cambio.

---

# 7. P2: dashboard Grafana

La instrumentación de:

- queue time
- job duration
- stage duration
- percentiles

ya está razonablemente integrada.

Falta completar la visualización operativa.

## Dashboard recomendado

Por worker/stage:

```text
P50 latency
P95 latency
queue time P50
queue time P95
throughput
errors
jobs completed
jobs failed
```

Idealmente con filtros:

```text
worker
stage
profile
time range
```

Esto debería ser una capa de observabilidad, no lógica de aplicación.

---

# 8. P3: decidir semántica de `balanced` y `full`

Actualmente `fast`, `balanced` y `full` existen.

Revisar si realmente se desea:

```text
balanced ≈ full
```

o si `full` debería incluir explícitamente más procesamiento.

Una semántica posible:

```text
fast
  extraction
  metadata

balanced
  extraction
  metadata
  embeddings
  entities

full
  extraction
  metadata
  embeddings
  entities
  inferences
```

No modificarlo sin decisión explícita.

El sistema ya permite activar features adicionales mediante `feature_extras`, por lo que la capacidad existe.

---

# 9. No implementar todavía GPU scheduler

No convertir el GPU scheduler en una prioridad en esta fase.

El hardware y el runtime GPU deben estabilizarse primero.

La arquitectura actual permite introducir posteriormente:

```text
GPU scheduler
    ↓
worker placement
    ↓
GPU-aware queues
```

pero hacerlo ahora añadiría complejidad antes de disponer de datos reales.

Primero:

1. estabilizar workers;
2. ejecutar benchmarks;
3. medir VRAM;
4. medir throughput;
5. identificar contención real.

Después diseñar scheduler.

---

# 10. No forzar TTFT/TPOT todavía

No intentar construir una métrica artificial de:

```text
TTFT
TPOT
```

sobre el stack actual si el backend principal sigue siendo Ollama y no proporciona esas métricas de forma fiable.

Cuando el serving de modelos se base en un runtime con métricas apropiadas, entonces sí tiene sentido instrumentarlas.

---

# 11. Componentes que se consideran correctamente implementados

No rehacer estas partes salvo que aparezca un bug durante los tests.

## D1: deduplicación de entidades

Centralizada en:

```text
pkg/worker_common/entity_utils.py
```

La deduplicación es estable y compartida entre workers.

## D2: regex paralelo

La implementación actual utiliza ejecución concurrente de regex mientras GLiNER trabaja.

Conceptualmente:

```text
GLiNER ───────────────┐
                      ├── merge
regex HTTP ───────────┘
```

Es una solución adecuada. No crear otra cola RabbitMQ únicamente para esto.

## D3: Artifact Store

El diseño content-addressed es correcto.

Mantener:

```text
SHA-256
atomic write
shared artifacts
```

y añadir únicamente lifecycle/GC.

## D4: PipelineDefinition

`PipelineDefinition` está realmente integrado en routing y completion.

Mantener esta arquitectura.

## Profiles

Los profiles están integrados y tienen fallback.

No duplicar lógica de routing en cada worker.

## Stage interface

`Stage` funciona como contrato/adaptador ligero.

No intentar convertirlo ahora en un framework que gobierne todo el runtime.

## Multimodal

La arquitectura:

```text
image → OCR → text/chunks → pipeline
audio → transcription → text/chunks → pipeline
```

es correcta y debe mantenerse.

---

# 12. Plan de ejecución recomendado

## Fase 1: bugs funcionales

1. Corregir `document_url` + metadata.
2. Corregir cache de image-analyzer.
3. Añadir tests de regresión.

## Fase 2: lifecycle

4. Implementar cancelación cooperativa.
5. Implementar Artifact GC.
6. Verificar que artifacts compartidos nunca se eliminan prematuramente.

## Fase 3: observabilidad

7. Completar `stage.queued`.
8. Completar dashboard Grafana.

## Fase 4: optimización

9. Evitar segunda lectura del documento.
10. Ejecutar benchmarks reales.
11. Revisar profiles basándose en datos.

## Fase 5: futuro

12. GPU scheduler.
13. Métricas TTFT/TPOT cuando el serving las soporte correctamente.

---

# 13. Criterio de cierre

Esta fase puede considerarse cerrada cuando:

- [ ] `document_url` produce metadata correcta.
- [ ] cancelación cooperativa funciona en workers relevantes.
- [ ] Artifact GC está implementado y probado.
- [ ] image cache no tiene colisiones por prompt/configuración.
- [ ] `stage.queued` está implementado o eliminado del contrato.
- [ ] dashboard operativo P50/P95 está disponible.
- [ ] tests de regresión pasan.
- [ ] benchmarks se ejecutan sobre hardware objetivo.
- [ ] no existen duplicaciones de routing que contradigan `PipelineDefinition`.

No es necesario:

- [ ] implementar GPU scheduler ahora.
- [ ] implementar TTFT/TPOT ahora.
- [ ] rehacer la arquitectura de workers.
- [ ] sustituir `PipelineDefinition`.
- [ ] convertir `Stage` en un framework de ejecución.

---

# Veredicto

La implementación actual de textFlow está en un estado razonablemente maduro.

Los problemas detectados son principalmente de **completitud, lifecycle y algunos bugs concretos**, no de arquitectura.

La recomendación es **corregir los puntos P0/P1, completar observabilidad y lifecycle, ejecutar benchmarks y evitar añadir nueva complejidad antes de tener datos reales del hardware**.

