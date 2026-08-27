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
| §3, §38 | "Pasar de pipeline rígido a DAG" — como si no existiera paralelismo | **DEAD CODE.** `internal/pipeline/orchestrator.go:68` `ProcessInParallel` define fan-out con `sync.WaitGroup` pero **nadie lo invoca** (verificado 2026-08-18, 0 callers). El DAG real vive en Python: routing en `extraction-worker.py:1031-1037`, `required_steps` en `completion-worker.py:102-103`. Lo que falta es **declaratividad**, no paralelismo. |
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
   — cuello de botella de latencia que el doc no menciona. **Pero es dead code**
   (ver punto 3): nadie lo invoca. La finalización real ya es event-driven vía
   Redis pub/sub en completion-worker.
3. **`ProcessInParallel` y `WaitForCompletion` son dead code** (0 callers,
   verificado 2026-08-18). El orchestrator Go sólo publica el primer mensaje a
   `extract_text` (`rabbitmq.go:249`); el DAG real vive en Python
   (`extraction-worker.py:1031-1037` + `completion-worker.py:102-103`).
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

- **0.1** **RESUELTO (D1):** Unificar `deduplicate_entities` en
  `pkg/worker_common/entity_utils.py` con la semántica de completion-worker
  (`start_offset`/`end_offset`, dict por `entity_id`). Eliminar
  `entities_worker.py:238-263` + bloque `:376-377`. Unificar generación de
  `entity_id` con unidecode (hoy inconsistente: `entities_worker.py:66` sin
  unidecode vs `completion_worker.py:411` con unidecode). `sliding_window.merge_entities`
  queda separada (dedup posicional por overlap, concern distinto).
- **0.2** Documentar en `AGENTS.md` que ya existe: `EventBus`, SSE,
  `extract_inferences_batch`, cache de inferencias, regex como microservicio,
  y que el **DAG real vive en Python** (routing extraction-worker + required_steps
  completion-worker). Marcar `ProcessInParallel`/`WaitForCompletion` como dead
  code. Evitar que futuros planes asuman greenfield.
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
- **1.5** ~~Reemplazar `WaitForCompletion`~~ → **dead code** (0 callers). La
  finalización ya es event-driven: completion-worker suscribe a Redis pub/sub
  (`completion_worker.py:873-885`) y finaliza cuando `required_steps` se cumple.
  La tarea pasa a ser: **eliminar** `WaitForCompletion` + `ProcessInParallel`
  y documentar el DAG real en `AGENTS.md`.

**Criterio de salida:** dashboard Grafana con P50/P95 de `queue_time`,
`processing_time`, `total_time` por stage.

---

## Fase 2 — Optimizaciones de bajo riesgo (válido, pero recortado)

Del doc, sobreviven:

- **2.1** Benchmark de batch size BGE-M3 (32/64/96/128) — no existe benchmark,
  sólo default.
- **2.2** Benchmark de batch size GLiNER (16/32/64) — idem.
- **2.3** **RESUELTO (D2):** Paralelizar regex + GLiNER con
  `concurrent.futures.ThreadPoolExecutor` dentro de entities-worker. NO cola:
  regex es stateless, rápido, sin GPU (0.5 CPU/256MB); `declareQueues()`
  (`rabbitmq.go:198-215`) no declara cola regex; regex es I/O-bound y libera el
  GIL → paralelismo real contra GLiNER (CPU/GPU-bound). Degrade silencioso ya
  existe (`entities_worker.py:279-281`).
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

- **3.1** **RESUELTO (D3):** Artifact store = **FS local con hash sharding**
  (`data/{ab}/{cd}/{full_hash}.bin`, 65k buckets) + interfaz
  `ArtifactStore.Put/Get` (`FSStore` hoy, `S3Store` futuro si multi-node).
  Mover a FS: `:text`, `:chunks`, `:embeddings`, `:inference_embeddings`,
  `:results` (ya escribe FS en `completion_worker.py:174-208`). Dejar en Redis
  refs cortas + control/locks + `:micro_inferences_raw`. Generalizar
  `_cache_key` a `artifact:{stage}:{stage_version}:{input_hash}`.
- **3.2** Idempotencia por stage — válido. Generalizar
  `inference:cache:{hash}` a `artifact:{stage}:{stage_version}:{input_hash}`
  (mismo esquema de hash que D3). Reusa `computeHash` de Go y el patrón Python.
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

- **4.1** `PipelineDefinition` declarativa (YAML) — válido. **Corregido:** el DAG
  NO es `ProcessInParallel` (dead code). Migrar a config el routing real de
  `extraction-worker.py:1031-1037` + `required_steps` de
  `completion-worker.py:102-103`. **Migración = D4 (big-bang con drain).**
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

## C. Decisiones resueltas (2026-08-18)

| # | Decisión | Evidencia |
|---|----------|-----------|
| **D1** | Unificar `deduplicate_entities` en `pkg/worker_common/entity_utils.py` con semántica de completion (`start_offset`/`end_offset`, dict por `entity_id`). Eliminar la de entities-worker + bloque `:376-377`. Unificar `entity_id` con unidecode. | Dedup de entities es redundante (completion re-corre siempre sobre el mismo set); `positions` es dato muerto (ningún consumidor, ningún modelo Go); `DEDUPLICATION_ENABLED=false` en prod; 6 tests cubren completion, 0 entities; `EntityMinimal` (`job.go:74`) exige `start_offset`/`end_offset`. |
| **D2** | Regex en **thread HTTP paralelo** dentro de entities-worker (`ThreadPoolExecutor`). NO cola. | Regex serializado detrás de GLiNER (`entities_worker.py:321-374`); I/O-bound libera GIL; stateless, rápido, sin GPU (0.5 CPU/256MB); `declareQueues()` (`rabbitmq.go:198-215`) no declara cola regex; degrade silencioso ya existe (`:279-281`). |
| **D3** | **FS local con hash sharding** (`data/{ab}/{cd}/{hash}.bin`, 65k buckets) + interfaz `ArtifactStore.Put/Get`. MinIO/S3 no. | Deploy single-node (sin k8s/swarm); completion-worker ya escribe a FS (`completion_worker.py:174-208`); `:text`/`:chunks` se leen mid-pipeline; ~6500 artifacts pequeños por doc grande; `maxmemory 1gb + noeviction` insostenible (~30-40MB/job). |
| **D4** | **Big-bang con drain** (NO dual-run). `JobTimeout=60m` acota. Añadir `pipeline_version` a `JobMessage` como escape hatch. | `ProcessInParallel`/`WaitForCompletion` son dead code (0 callers); DAG vive en Python sin versionado de mensajes; admission control ya existe (`internal/config/config.go:42-45`); air-gapped + equipo chico aceptan ventana 30-60min. |

### Detalle D4

- Migrar routing de `extraction-worker.py:1031-1037` + `required_steps` de
  `completion-worker.py:102-103` a `PipelineDefinition` (YAML).
- Añadir `pipeline_version` a `JobMessage` (`internal/models/job.go:156`) en el
  mismo deploy — los workers lo leen pero lo ignoran si vale `"v1"`; habilita
  dual-run futuro sin cambiar formato de mensajes.
- Eliminar `ProcessInParallel` (`orchestrator.go:68`) y `WaitForCompletion`
  (`orchestrator.go:193`) — dead code, no migrarlos.
- Corregir documentación: `README.md:80,122` dicen que regex-entity-extractor
  es Python — es **Go** (`main.go:1`, `Dockerfile:2`).

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

- `internal/pipeline/orchestrator.go:68` — `ProcessInParallel` (**dead code, 0 callers**)
- `internal/pipeline/orchestrator.go:193` — `WaitForCompletion` (**dead code, 0 callers**)
- `cmd/extraction-worker/worker.py:1031-1037` — **DAG real** (routing fan-out)
- `cmd/completion-worker/completion_worker.py:102-103` — **DAG real** (`required_steps`)
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

---

## Estado de implementación (2026-08-27)

### Completado

| Punto | Estado | Commit |
|-------|--------|--------|
| **0.1** Dedup unificado (D1) | ✅ | sesión previa |
| **2.3** Regex paralelo (D2) | ✅ | sesión previa |
| **3.1** Artifact store FS (D3) | ✅ | sesión previa |
| **4.1** PipelineDefinition declarativa (D4) | ✅ | sesión previa |
| **1.1** queue_time | ✅ | sesión previa |
| **3.2** Idempotencia por stage | ✅ | `42d907b` |
| **3.3** Versionado stage/model | ✅ | `42d907b` |
| **4.2** Stage interface | ✅ | `42d907b` |
| **4.4** Cancelación real | ✅ | `42d907b` |
| **4.5** Eventos stage-level | ✅ | `42d907b` |
| **5.1/5.2/5.3** Multimodal | ✅ | `ce4cc2a` |
| **4.8** SSE stage_completed | ✅ | `ded92a0` |
| **4.7** Processing profiles | ✅ | `d7757d8` |
| **3.7** Backpressure downstream | ✅ | `30996e5` |
| **3.4** Refs vs inline chunks | ✅ | `74b3268` |
| **1.2** Métricas GPU + Prometheus/Grafana | ✅ | `31cb00f` |
| **1.4** Benchmark suite P50/P95 | ✅ | `4fe548a` |

### Pendiente (diferido)

| Punto | Estado | Motivo |
|-------|--------|--------|
| **1.3** TTFT/TPOT | ⏸️ Diferido | Requiere streaming o scrape de `/metrics` de vLLM. **Ollama (motor temporal) no expone TTFT/TPOT** ni su API de streaming es compatible. `tokens/sec` ya cubierto (`inference_worker_llm_tokens_per_sec`). Revisitar cuando vLLM sea el motor real. |
| **4.6** GPU scheduler activo | ⏸️ Pendiente | Greenfield: convertir resource-manager en scheduler activo que asigne workers a GPUs por VRAM (API de leases + consumo en workers). Requiere GPU real estable. **Nota:** el toolkit NVIDIA de este host está roto (nvidia-uvm major 511 vs 510) — puede resolverse con un reinicio del host; verificar antes de implementar. |
| **3.5** Optimizar dedup (bucket por prefijo) | ⏸️ Opcional | Mejora marginal; el spec dice "solo si perfilado muestra cuello de botella". |
| **2.1/2.2** Benchmarks batch BGE-M3/GLiNER | ✅ Existen | `scripts/bench/bench_embeddings.py`, `bench_gliner.py` (ya presentes). |

### Criterio de salida Fase 1 (dashboard Grafana)

La infra Prometheus/Grafana está desplegada (`31cb00f`), pero **falta el dashboard JSON de Grafana** con P50/P95 de queue_time/processing_time/total_time por stage. Los datos están disponibles en Prometheus (`{worker}_queue_time_seconds`, `{worker}_job_duration_seconds`); falta el panel. Pendiente de crear.