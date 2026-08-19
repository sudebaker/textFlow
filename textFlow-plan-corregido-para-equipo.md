# textFlow — Plan de mejoras arquitectónicas y de rendimiento (corregido)

**Espec derivada de:** `textFlow_mejoras_arquitectura_y_rendimiento.md` (2026-08-17)
**Fecha del spec corregido:** 2026-08-17
**Estado:** aprobado por el usuario
**Autor:** sesión de arquitectura, basado en auditoría del código real vía grafo de codebase-memory

---

## 0. Motivo de este documento

El documento original parte de premisas incorrectas: asume greenfield en
varias áreas donde el código ya tiene soluciones funcionando. Este spec es el
**plan diferencial**: extiende lo que funciona, elimina lo duplicado y construye
sólo lo que genuinamente falta.

El principio rector del doc original ("medir primero, reducir trabajo innecesario,
explotar paralelismo") se mantiene. Lo que cambia es el **desde dónde**.

---

## A. Premisas incorrectas del documento original

Tabla de evidencia: cada fila es una afirmación del doc original contrastada
contra el código real.

| # | Afirmación del doc original | Realidad en el código |
|---|---|---|
| §3, §38 | "Pasar de pipeline rígido a DAG" — como si no existiera paralelismo | `internal/pipeline/orchestrator.go:68` `ProcessInParallel` ya hace fan-out con `sync.WaitGroup` (embeddings+entities+metadata en goroutines). Lo que falta es **declaratividad**, no paralelismo. |
| §9 | "Publicar eventos (job.created, stage.started…)" como propuesta nueva | `pkg/events_python.py:12` `EventBus` ya publica `job_progress`, `job_completed`, `job_failed`, `job_inference_chunk_progress`. `cmd/orchestrator/handlers/stream.go:36` `StreamJobHandler` ya hace SSE con heartbeats. |
| §15 | "Metadata se realiza después de la extracción" (implícita en mismo worker) | `cmd/metadata-worker/worker.py` es un **worker independiente** con su propia cola. El doc lo sitúa mal. |
| §21 | "La extracción regex se realiza como etapa posterior" (en cola, secuencial) | `cmd/regex-entity-extractor/` es un **microservicio Go** invocado vía HTTP POST desde `cmd/entities-worker/entities_worker.py` `_extract_regex_entities`. No comparte cola. Paralelizarlo requiere cambiar el modelo de invocación, no "mergear colas". |
| §22 | "Deduplicación todas contra todas" (O(n²) global) | `cmd/entities-worker/entities_worker.py:251` filtra con `existing_key.startswith(f"{label}:")` antes de fuzzy. `cmd/completion-worker/completion_worker.py:411` hace `result[existing_id]["label"] != label: continue`. Ya es O(n²) **por bucket de label**, no global. El bucket por prefijo propuesto es una mejora marginal, no el cambio radical que pinta el doc. |
| §24 | "Agrupar inferencias (inference_batch) en lugar de una por chunk" como propuesta | `cmd/inference-worker/inference_worker.py` `extract_inferences_batch` **ya existe** con fallback a individual. `transitive_loop_depth=4` — ya es complejo. |
| §19, §6 | "Cache de embeddings/inferencias como propuesta" | `_cache_key` en inference-worker ya produce `inference:cache:{sha256(text+source+model+params)}`. `internal/cache/content_cache.go` `ContentCache` en Go. `compute_file_hash` para dedup de documentos. Falta extender a embeddings/entities, no construir desde cero. |
| §10 | "Cancelación de jobs" como propuesta | `deleteJobHandler` (DELETE /documents/:id) **sólo borra terminados**; devuelve 409 si está en progreso. No existe cancelación real. Esto SÍ es válido. |
| §34 | "Medir queue_time" | No hay ninguna métrica `queue_time`. Válido. |

## B. Lo que el doc original NO menciona y debería

1. **Deduplicación duplicada en dos workers** (`entities-worker` y `completion-worker`)
   con lógica **diferente** y tests separados. El doc habla de optimizar una
   sola. Hay que unificar o explicar por qué coexisten.
2. **`WaitForCompletion` hace polling Redis cada 500ms** (`orchestrator.go:200`)
   — cuello de botella de latencia no mencionado. El EventBus ya podría
   señalizar completion sin polling.
3. **`ProcessInParallel` devuelve `PipelineResult` con todos los campos `nil`**
   (`orchestrator.go:117-122`) — los resultados reales se leen de Redis aparte.
   El "DAG" actual no orquesta datos, sólo dispara goroutines y espera.
4. **No hay `stage_version` ni `pipeline_version`** en ningún mensaje. El
   versionado propuesto sí falta.
5. **`extract_inferences_batch` tiene `transitive_loop_depth=4`** — ya es
   complejo; el doc lo trata como greenfield.

---

# Plan de implementación

## Principio rector

El doc original tiene razón en la filosofía ("medir primero, reducir trabajo
innecesario, explotar paralelismo") pero **describe un greenfield que no
existe**. El plan real debe ser **diferencial**: extender lo que funciona,
eliminar lo duplicado, y construir sólo lo que genuinamente falta.

---

## Fase 0 — Eliminar duplicación y alinear premisas (1-2 días)

Antes de cualquier optimización, el código debe reflejar la realidad.

- **0.1** Unificar `deduplicate_entities` en un solo sitio. Decidir: ¿entities-worker
  deduplica por chunk y completion-worker mergea entre chunks? ¿O uno solo? Hoy
  ambas existen con semántica distinta (`positions` vs `start_offset/end_offset`).
  Esto es deuda que el doc ignora.
- **0.2** Documentar en `AGENTS.md` que ya existe: `ProcessInParallel`, `EventBus`,
  SSE, `extract_inferences_batch`, cache de inferencias, regex como microservicio.
  Evitar que futuros planes asuman greenfield.
- **0.3** Reescribir el doc de mejoras marcando explícitamente los 9 ítems de la
  tabla A como "ya existe — no tocar" o "ya existe parcial — extender".

**Criterio de salida:** no queda lógica duplicada y el equipo sabe qué existe.

---

## Fase 1 — Observabilidad real (la fase 1 del doc sí aplica, pero ajustada)

El doc acierta aquí. Implementar antes de tocar nada.

- **1.1** Métricas por stage con `queue_time` real: registrar `queued_at` (al
  publicar a RabbitMQ), `started_at` (al consumir), `completed_at`. Hoy sólo
  hay `started_at`/`completed_at` implícitos. `queued_at` falta.
- **1.2** Métricas GPU: `gpu_utilization`, `gpu_memory_used`. El
  `resource-manager` (puerto 9090) ya monitorea — exponerlo a Prometheus si no
  lo está.
- **1.3** `tokens/sec`, `TTFT`, `TPOT` del inference-worker (vLLM ya los expone
  en `/metrics`).
- **1.4** Benchmark suite de 10 documentos (§35 del doc original) — válido, no
  existe.
- **1.5** Reemplazar `WaitForCompletion` (polling 500ms) por suscripción a
  EventBus para completion. Elimina hasta 500ms de latencia percibida por job.

**Criterio de salida:** dashboard Grafana con P50/P95 de `queue_time`,
`processing_time`, `total_time` por stage.

---

## Fase 2 — Optimizaciones de bajo riesgo (válido, pero recortado)

Del doc, sobreviven:

- **2.1** Benchmark de batch size BGE-M3 (32/64/96/128) — no existe benchmark,
  sólo default.
- **2.2** Benchmark de batch size GLiNER (16/32/64) — idem.
- **2.3** Paralelizar regex + GLiNER — **pero el doc miente sobre el modelo**:
  regex es HTTP síncrono dentro de entities-worker. Hay dos opciones: (a)
  lanzar `_extract_regex_entities` en thread paralelo a GLiNER dentro del mismo
  worker, (b) convertir regex en cola consumida en paralelo. Decidir con
  benchmark.
- **2.4** `metadata_fast` vs `metadata_deep` — válido. Hoy `_extract_metadata`
  hace sha256+idioma+readability siempre. Separar fast (size/mime/hash) de
  deep (exiftool/XMP) tras flag.
- **2.5** Text analysis: detección de idioma sobre primeros N chars,
  readability sobre muestra. Válido.
- **2.6** Revisar concurrencia inference-worker — pero **sobre
  `extract_inferences_batch` existente**, no reimplementar.

**Criterio de salida:** benchmark antes/después por cada ítem; merged sólo si
mejora sin degradar calidad.

---

## Fase 3 — Optimizaciones estructurales (recortado drásticamente)

El doc lista 7 ítems; varios ya existen.

- **3.1** ~~Cache de artifacts~~ → ya hay cache de inferencias y documentos.
  **Extender** el patrón `_cache_key` a embeddings y entities (no existe). El
  modelo ya está probado.
- **3.2** Idempotencia por stage — válido. Generalizar
  `inference:cache:{hash}` a `artifact:{stage}:{stage_version}:{input_hash}`.
  Reusa `computeHash` de Go y el patrón Python.
- **3.3** Versionado explícito (`pipeline_version`, `stage_version`,
  `model_version`) — válido, no existe. Añadir a mensajes RabbitMQ y keys Redis.
- **3.4** Evitar serialización duplicada (§26, §27 del doc original) — válido.
  Medir primero (el doc mismo lo dice). Hoy chunks van a Redis Y en mensajes
  RabbitMQ. Decidir referencia vs inline con benchmark.
- **3.5** Optimizar deduplicación — **recortado**: ya filtra por label. Mejora
  marginal: bucket por prefijo dentro de cada label. Hacer sólo si perfilado
  muestra que es cuello de botella (dudoso: entities-worker ya tiene `in_degree=7`
  en `deduplicate_entities`, se llama poco).
- **3.6** ~~Agrupar inferencias~~ → ya existe `extract_inferences_batch`.
  Eliminar del plan.
- **3.7** Scheduler con backpressure — válido, no existe.

**Criterio de salida:** artifacts reutilizables entre runs; reanudación de jobs
tras crash.

---

## Fase 4 — Evolución arquitectónica (el núcleo válido del doc)

Aquí el doc acierta en el **qué** pero no en el **desde dónde**.

- **4.1** `PipelineDefinition` declarativa (YAML) — válido. Hoy el "DAG" es
  `ProcessInParallel` hardcoded en Go. Mover a config.
- **4.2** Stage interface unificada — válido. Cada worker tiene
  `_process_message_async`/`process_message`/`_process_message` con firmas
  distintas. Definir `Stage.execute(context) -> StageResult`.
- **4.3** Artifact model + Artifact store separado de Redis — válido. Hoy todo
  es blob en Redis. Para docs grandes (>1MB) esto es problema real.
- **4.4** Cancelación real — válido y crítico. Hoy `deleteJobHandler` devuelve
  409 si el job está en progreso. Implementar `POST /v1/documents/:id/cancel`
  que: marque estado `CANCELLED` en Redis, los workers lo lean periódicamente
  (patrón ya usado por context cancellation en middleware), y el scheduler no
  programe stages dependientes.
- **4.5** Eventos de dominio granulares (`stage.queued`, `stage.started`,
  `stage.completed`, `artifact.created`) — **extender** el `EventBus` existente,
  no crearlo. Hoy publica a nivel job; falta a nivel stage.
- **4.6** GPU scheduler — válido. `resource-manager` monitorea pero no
  schedulea. Convertir en scheduler activo que asigne workers a GPUs por VRAM.
- **4.7** Processing profiles (fast/balanced/full) — válido, no existe.
- **4.8** Resultados parciales a agentes — **extender SSE existente**. Hoy
  `StreamJobHandler` emite `job_progress`. Añadir evento `stage_completed` con
  payload del artifact (ej: texto extraído disponible en `time_to_text`).

**Criterio de salida:** un job configurable por YAML, cancelable, con artifacts
versionados fuera de Redis.

---

## Fase 5 — Multimodal (válido, ortogonal)

- **5.1** Image preprocessing (resize antes de OCR/VLM) — válido.
- **5.2** VAD para audio (no pasar silencios a Whisper) — válido. `audio-worker`
  existe, verificar si ya lo hace.
- **5.3** Cache multimodal por `SHA256(file)` — **extender** `compute_file_hash`
  existente.

**Criterio de salida:** RTF < 1 para audio; imágenes sin OCR innecesario.

---

## Orden de ejecución (reemplaza §38 del doc original)

```
0. Eliminar duplicación dedup + alinear premisas
   ↓
1. Observabilidad + benchmark suite + reemplazar polling por EventBus
   ↓
2. Optimizaciones de bajo riesgo (batches, metadata fast, text analysis)
   ↓
3. Idempotencia por stage + versionado + extender cache a embeddings/entities
   ↓
4. PipelineDefinition declarativa + Artifact store + Cancelación + GPU scheduler
   ↓
5. Multimodal
```

**Justificación del reorden:** la Fase 0 no estaba en el doc. Es necesaria porque
el doc mismo parte de premisas falsas — ejecutar el doc literal llevaría a
reimplementar `EventBus`, `extract_inferences_batch` y `ProcessInParallel` que
ya funcionan.

---

## Preguntas abiertas (resolver antes de implementar)

1. **Deduplicación:** ¿unificar en un solo worker o mantener dos con
   responsabilidades distintas (entities per-chunk, completion cross-chunk)?
   Es una decisión de arquitectura que el doc ignora.
2. **Regex:** ¿paralelizar dentro de entities-worker (thread HTTP) o convertir
   a cola? Depende de si querés que regex escale independiente.
3. **Artifact store:** ¿filesystem local (air-gapped), MinIO, o
   S3-compatible? El doc no decide y el contexto air-gapped lo condiciona.
4. **Backward compat:** ¿los jobs en progreso durante la migración a
   `PipelineDefinition` deben seguir funcionando con el DAG hardcoded viejo?
   Respuesta probable: sí, correr ambos en paralelo una ventana.

---

## Criterio de aceptación (heredado del doc original, sin cambios)

Una mejora se considera válida únicamente si cumple al menos uno de estos
criterios sin degradación significativa de calidad:

- Reduce P50/P95 de latencia.
- Aumenta documentos/segundo.
- Aumenta chunks/segundo.
- Aumenta tokens/segundo.
- Reduce utilización de CPU/GPU para el mismo trabajo.
- Reduce memoria.
- Reduce tráfico/serialización.
- Reduce coste computacional.
- Permite reutilizar artifacts.
- Mejora la capacidad de recuperación ante fallos.

Toda optimización de rendimiento debe acompañarse de benchmark antes/después.

---

## Referencias al código (evidencia)

- `internal/pipeline/orchestrator.go:68` — `ProcessInParallel` (fan-out actual)
- `internal/pipeline/orchestrator.go:193` — `WaitForCompletion` (polling 500ms)
- `pkg/events_python.py:12` — `EventBus` (eventos job-level existentes)
- `cmd/orchestrator/handlers/stream.go:36` — `StreamJobHandler` (SSE)
- `cmd/entities-worker/entities_worker.py:238` — `deduplicate_entities` (bucket por label)
- `cmd/completion-worker/completion_worker.py:384` — `deduplicate_entities` (duplicado, semántica distinta)
- `cmd/inference-worker/inference_worker.py` — `extract_inferences_batch` (ya existe)
- `cmd/inference-worker/inference_worker.py:261` — `_cache_key` (cache de inferencias)
- `internal/cache/content_cache.go:186` — `computeHash` (cache Go)
- `cmd/extraction-worker/worker.py:105` — `compute_file_hash` (dedup documentos)
- `cmd/regex-entity-extractor/` — microservicio Go (no cola)
- `cmd/metadata-worker/worker.py` — worker independiente (no dentro de extraction)
- `cmd/orchestrator/main.go:686` — `deleteJobHandler` (sólo borra terminados, 409 en progreso)