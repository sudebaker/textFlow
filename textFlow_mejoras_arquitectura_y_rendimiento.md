# textFlow --- Plan de mejoras arquitectónicas, funcionales y de rendimiento

**Documento para el equipo de desarrollo**\
**Fecha:** 2026-08-17\
**Objetivo:** consolidar las mejoras arquitectónicas propuestas para
textFlow y añadir un plan específico para reducir latencia, aumentar
throughput y hacer más eficiente el procesamiento multimodal.

------------------------------------------------------------------------

## 1. Resumen ejecutivo

textFlow debe evolucionar desde un pipeline rígido de procesamiento
hacia un **motor multimodal genérico, componible y orientado a
artefactos**, capaz de procesar documentos, imágenes y audio mediante
pipelines configurables.

La arquitectura propuesta debe mantener las ventajas actuales:

-   RabbitMQ para desacoplar workers.
-   Redis para estado y resultados intermedios.
-   Workers especializados.
-   Procesamiento offline.
-   GPU workers independientes.
-   Prometheus para métricas.
-   Idempotencia y recuperación ante fallos.

Sobre esta base se proponen dos líneas de evolución:

### A. Evolución arquitectónica

1.  Pipelines definidos como DAG.
2.  `PipelineDefinition` configurable.
3.  Plugins/stages desacoplados.
4.  Estados explícitos por stage.
5.  Sistema de `Artifact`.
6.  Idempotencia y versionado.
7.  Separación entre almacenamiento de estado y almacenamiento de
    artefactos.
8.  Eventos de dominio.
9.  Cancelación de jobs.
10. Scheduler/gestor de recursos GPU.
11. Profiles de procesamiento.
12. Procesamiento incremental y resultados parciales.

### B. Optimización de rendimiento

1.  Perfilar cada etapa antes de optimizar.
2.  Optimizar Docling y clasificación previa del documento.
3.  Evitar trabajo innecesario de OCR.
4.  Optimizar metadata y análisis de texto.
5.  Ajustar dinámicamente batches de BGE-M3 y GLiNER.
6.  Paralelizar regex y GLiNER.
7.  Sustituir deduplicación O(n²) por candidate bucketing.
8.  Revisar batching/continuous batching del inference-worker.
9.  Reducir serialización y copias de chunks.
10. Optimizar Redis/RabbitMQ únicamente después de medir.
11. Añadir procesamiento adaptativo según tipo de documento.
12. Optimizar imagen mediante resize y audio mediante VAD.
13. Separar `time_to_text` de `time_to_processed_document`.
14. Exponer resultados parciales a los agentes.

------------------------------------------------------------------------

# 2. Principios de diseño

## 2.1. Procesar únicamente lo necesario

Un job no debe ejecutar todas las capacidades disponibles por defecto.

Cada petición debe especificar o resolver un conjunto de features:

``` json
{
  "features": [
    "text",
    "chunks",
    "entities",
    "embeddings"
  ]
}
```

Las inferencias LLM, embeddings secundarios, metadata enriquecida, etc.
deben ejecutarse solamente cuando sean necesarias.

------------------------------------------------------------------------

## 2.2. Todo resultado importante es un Artifact

Cada stage debe producir artefactos identificables y versionados.

Ejemplo:

``` text
Document
 └── extracted_text:v1
      └── chunks:v1
           ├── embeddings:v1
           ├── entities:v1
           └── inferences:v1
```

Esto permite:

-   reutilizar resultados;
-   evitar reprocesamiento;
-   reanudar jobs;
-   cambiar un stage sin repetir todo el pipeline;
-   cachear resultados;
-   comparar versiones de modelos;
-   construir pipelines incrementales.

------------------------------------------------------------------------

# 3. Arquitectura de pipelines basada en DAG

## 3.1. PipelineDefinition

Crear una representación declarativa del pipeline.

Ejemplo conceptual:

``` yaml
pipeline:
  name: document_full
  version: "1.0"

stages:
  - id: extraction
    type: document.extraction

  - id: chunking
    type: text.chunking
    depends_on:
      - extraction

  - id: entities
    type: nlp.entities
    depends_on:
      - chunking

  - id: embeddings
    type: nlp.embeddings
    depends_on:
      - chunking

  - id: inference
    type: llm.inference
    depends_on:
      - entities
      - chunking

  - id: inference_embeddings
    type: nlp.embeddings
    depends_on:
      - inference
```

El scheduler debe ejecutar stages cuando sus dependencias estén
satisfechas.

------------------------------------------------------------------------

## 3.2. Ejecución paralela

El DAG debe permitir:

``` text
                    extraction
                        |
                     chunking
                        |
             +----------+----------+
             |          |          |
             v          v          v
         entities   embeddings   metadata
             |
             +----------+
                        |
                    inference
                        |
               inference embeddings
```

No se debe convertir un DAG en una cadena simplemente porque sea más
sencillo de implementar.

------------------------------------------------------------------------

# 4. Sistema de stages/plugins

Cada worker debe implementar una interfaz común.

Conceptualmente:

``` python
class Stage:
    name: str
    version: str

    async def execute(
        self,
        context: StageContext
    ) -> StageResult:
        ...
```

El stage debe declarar:

-   inputs;
-   outputs;
-   dependencias;
-   recursos requeridos;
-   versión;
-   si necesita GPU;
-   memoria estimada;
-   posibilidad de procesamiento por batch;
-   si soporta reanudación.

Ejemplo:

``` yaml
resources:
  gpu: true
  gpu_memory_gb: 4
  batchable: true
```

------------------------------------------------------------------------

# 5. Estados explícitos por stage

Cada stage debe tener estados como:

``` text
PENDING
QUEUED
RUNNING
COMPLETED
FAILED
RETRYING
CANCELLED
SKIPPED
```

Y registrar:

``` text
started_at
completed_at
duration
attempt
worker
model
model_version
error
```

Esto permitirá distinguir:

-   fallo real;
-   stage omitido;
-   stage todavía pendiente;
-   stage reintentado;
-   resultado recuperado de cache.

------------------------------------------------------------------------

# 6. Idempotencia

Cada operación debe poder ejecutarse más de una vez sin generar
corrupción ni duplicados.

Crear una clave determinista:

``` text
artifact_hash =
    SHA256(
        input_hash +
        stage_name +
        stage_version +
        model_version +
        parameters
    )
```

Si ya existe el artifact:

``` text
CACHE HIT
```

y el stage no vuelve a ejecutarse.

Esto es especialmente importante para:

-   embeddings;
-   OCR;
-   entidades;
-   inferencias;
-   transcripciones.

------------------------------------------------------------------------

# 7. Versionado

Versionar explícitamente:

``` text
pipeline_version
stage_version
model_version
configuration_version
```

Ejemplo:

``` text
entities:
  stage_version: 2.1
  model: gliner-small-v2.1
```

Un cambio de modelo no debe sobrescribir silenciosamente resultados
anteriores.

------------------------------------------------------------------------

# 8. Almacenamiento de artefactos

Separar:

### Redis

Para:

-   estado;
-   locks;
-   progreso;
-   referencias;
-   pequeños resultados temporales;
-   coordinación.

### Object storage / filesystem de artifacts

Para:

-   documentos;
-   texto completo grande;
-   imágenes;
-   audio;
-   embeddings grandes;
-   resultados voluminosos.

Redis no debería convertirse en el sistema de almacenamiento permanente
de blobs grandes.

------------------------------------------------------------------------

# 9. Eventos de dominio

Publicar eventos como:

``` text
job.created
stage.queued
stage.started
stage.completed
stage.failed
artifact.created
job.completed
job.failed
job.cancelled
```

Esto permitirá conectar:

-   UI;
-   agentes;
-   observabilidad;
-   scheduler;
-   futuros consumidores.

------------------------------------------------------------------------

# 10. Cancelación

Un job debe poder cancelarse.

El scheduler debe propagar:

``` text
cancel(job_id)
```

a los stages activos.

Los workers deben comprobar periódicamente si el job continúa siendo
válido.

La cancelación debe impedir que nuevas etapas sean programadas.

------------------------------------------------------------------------

# 11. Scheduler de recursos GPU

Introducir una capa que conozca:

``` text
GPU
 ├── VRAM total
 ├── VRAM utilizada
 ├── modelos cargados
 ├── workers
 └── capacidad disponible
```

Esto permitirá decidir:

``` text
GLiNER → GPU 0
BGE-M3 → GPU 1
LLM → GPU 2
```

o utilizar varias GPUs según disponibilidad.

El scheduler debe evitar cargar un modelo en una GPU que no tiene
memoria suficiente.

------------------------------------------------------------------------

# 12. Profiles de procesamiento

Añadir perfiles predefinidos.

## Fast

Orientado a obtener información rápidamente:

``` text
extraction
chunking
basic metadata
entities
```

## Balanced

``` text
extraction
chunking
metadata
entities
embeddings
```

## Full

``` text
extraction
chunking
metadata
entities
embeddings
inferences
inference embeddings
```

El usuario/agente puede solicitar:

``` json
{
  "processing_profile": "fast"
}
```

o features explícitas.

------------------------------------------------------------------------

# 13. Resultados parciales y latencia percibida

Distinguir:

``` text
time_to_text
time_to_entities
time_to_embeddings
time_to_inferences
time_to_processed_document
```

Un documento puede estar disponible para el agente antes de terminar
todo el enriquecimiento.

Ejemplo:

``` text
0%   accepted
10%  extracting
25%  text_ready
45%  entities
60%  embeddings
80%  inferences
100% completed
```

Esto permite que un agente empiece a trabajar con el texto mientras
textFlow continúa enriqueciendo el documento.

------------------------------------------------------------------------

# 14. Optimización de extraction-worker

## 14.1. Medir primero

Añadir métricas:

``` text
extraction_duration_seconds
docling_duration_seconds
metadata_duration_seconds
text_analysis_duration_seconds
chunking_duration_seconds
serialization_duration_seconds
queue_publish_duration_seconds
```

No optimizar Redis, JSON o RabbitMQ antes de conocer su peso real.

------------------------------------------------------------------------

## 14.2. Detección previa del tipo de documento

Crear una clasificación rápida:

``` text
PDF
 |
 +-- PDF con capa de texto
 |      -> extracción rápida
 |
 +-- PDF escaneado
 |      -> OCR
 |
 +-- PDF complejo
 |      -> Docling completo
 |
 +-- imagen
        -> OCR/VLM
```

No debe ejecutarse OCR pesado cuando el PDF ya contiene texto
utilizable.

------------------------------------------------------------------------

## 14.3. Optimización de Docling

El endpoint async y long-polling actuales deben mantenerse.

Evaluar:

-   parámetros de OCR;
-   image export;
-   parsing innecesario;
-   número de workers Docling;
-   concurrencia;
-   tamaño de documentos;
-   tiempo de CPU vs GPU;
-   reutilización de procesos.

El modo de imágenes `placeholder` debe mantenerse cuando las imágenes
completas no sean necesarias.

------------------------------------------------------------------------

# 15. Metadata más eficiente

Actualmente se realiza trabajo adicional de metadata después de la
extracción.

Separar:

``` text
metadata_fast
    size
    MIME
    hash
    filename
    filesystem data
```

de:

``` text
metadata_deep
    exiftool
    XMP
    author
    producer
    etc.
```

Ejecutar metadata profunda solamente cuando el pipeline la requiera.

Evitar leer el fichero completo varias veces si la información ya está
disponible.

------------------------------------------------------------------------

# 16. Optimización de text analysis

La función de análisis de texto ejecuta:

-   detección de idioma;
-   regex;
-   readability;
-   estadísticas.

Evaluar:

``` text
language detection → primeros N caracteres
readability → muestra representativa
regex → una única pasada
```

No calcular métricas costosas sobre cientos de miles de caracteres si no
aportan información adicional.

------------------------------------------------------------------------

# 17. Chunking

Mantener el chunking configurable:

``` yaml
chunking:
  size: 1000
  overlap: 200
```

Pero introducir perfiles por tipo de documento.

Por ejemplo:

``` text
legal:
    chunks más pequeños
    mayor overlap

generic:
    chunks estándar

long_form:
    chunks mayores
```

También medir:

``` text
chunks_per_document
tokens_per_chunk
average_chunk_tokens
chunking_duration
```

La calidad de los embeddings depende directamente de esta decisión.

------------------------------------------------------------------------

# 18. Optimización de embeddings

Actualmente BGE-M3 se ejecuta con batch GPU configurable.

El valor inicial de 32 debe tratarse como punto de partida, no como
constante sagrada.

Benchmark:

``` text
32
64
96
128
```

según longitud media de tokens y VRAM.

Medir:

``` text
tokens/sec
chunks/sec
GPU utilization
VRAM
latency
```

El objetivo no es llenar VRAM por orgullo, sino maximizar throughput sin
provocar OOM.

------------------------------------------------------------------------

# 19. Embeddings: evitar trabajo repetido

Generar embeddings solamente si:

-   el artifact no existe;
-   cambió el texto;
-   cambió el modelo;
-   cambió la configuración relevante.

Clave:

``` text
embedding_cache_key =
    hash(chunk_text + embedding_model + model_version + params)
```

------------------------------------------------------------------------

# 20. Optimización de GLiNER

Actualmente GLiNER ya procesa chunks por batches.

Benchmark independiente de:

``` text
16
32
64
```

y medir:

``` text
chunks/sec
tokens/sec
GPU utilization
VRAM
```

También separar:

``` text
batch inference
postprocessing
deduplication
```

para conocer el verdadero cuello de botella.

------------------------------------------------------------------------

# 21. Paralelizar GLiNER y regex

Actualmente la extracción regex se realiza como una etapa posterior.

Debe poder ejecutarse en paralelo:

``` text
             chunks
             /    \
            /      \
        GLiNER     regex
            \      /
             \    /
              merge
                |
             dedup
```

La regex es barata y no debería esperar a la inferencia neuronal.

------------------------------------------------------------------------

# 22. Optimizar deduplicación de entidades

La implementación actual compara entidades mediante fuzzy matching.

El algoritmo puede degradarse aproximadamente como O(n²).

Sustituir:

``` text
todas contra todas
```

por:

``` text
label
  ↓
normalización
  ↓
bucket por prefijo/longitud
  ↓
candidate set
  ↓
fuzzy matching
```

Ejemplo:

``` text
PERSON
  garc*
  gonz*
  fern*

ORG
  banco*
  univer*
```

Solo comparar candidatos plausibles.

Mantener `rapidfuzz` para la comparación final.

------------------------------------------------------------------------

# 23. Inference-worker

Este es uno de los puntos que deben perfilarse primero.

Actualmente entities puede publicar una inferencia por chunk.

Para un documento de 200 chunks:

``` text
200 chunks
→ 200 mensajes
→ 200 requests
```

Revisar:

-   número de workers;
-   prefetch;
-   concurrencia;
-   batch size;
-   continuous batching;
-   utilización real del servidor LLM;
-   tokens/sec;
-   tiempo de cola;
-   TTFT;
-   TPOT.

Si se utiliza vLLM, comprobar que el patrón de requests permite que su
continuous batching se aproveche realmente.

------------------------------------------------------------------------

# 24. Agrupación de inferencias

Evaluar una modalidad:

``` text
inference_batch:
    chunks: [1..N]
```

en lugar de:

``` text
inference:
    chunk 1

inference:
    chunk 2

inference:
    chunk 3
```

No debe implementarse a costa de aumentar demasiado la latencia.

El scheduler puede decidir:

``` text
interactive → chunks pequeños
batch → chunks agrupados
```

------------------------------------------------------------------------

# 25. Inference embeddings

Los embeddings de micro-inferencias deben reutilizar el mismo sistema de
batching/cache que los embeddings normales.

No ejecutar una llamada independiente por cada inference si puede
agruparse:

``` text
all inference texts
        ↓
batch
        ↓
BGE-M3
```

------------------------------------------------------------------------

# 26. Redis y RabbitMQ

Actualmente los chunks se almacenan en Redis y también se incluyen en
mensajes RabbitMQ.

Evaluar la duplicación:

``` text
chunks
 ↓
JSON
 ↓
Redis
 ↓
JSON
 ↓
RabbitMQ
```

Alternativas:

### A. Referencia

``` json
{
  "job_id": "...",
  "chunks_ref": "orchestrator:job:...:chunks"
}
```

### B. MessagePack

Mantener payload inline pero sustituir JSON por MessagePack cuando sea
apropiado.

### C. Artifact reference

La solución arquitectónicamente preferida a largo plazo:

``` json
{
  "job_id": "...",
  "artifact_id": "..."
}
```

No cambiar esto sin benchmark. El objetivo es reducir
copia/serialización, no mover el cuello de botella de un sitio a otro.

------------------------------------------------------------------------

# 27. Evitar serializaciones innecesarias

Medir:

``` text
json.dumps duration
json.loads duration
payload size
Redis write
Redis read
Rabbit publish
Rabbit receive
```

Especialmente importante para documentos grandes.

------------------------------------------------------------------------

# 28. Procesamiento multimodal: imágenes

Pipeline recomendado:

``` text
image
  ↓
format detection
  ↓
orientation
  ↓
resize
  ↓
quality assessment
  ↓
OCR / VLM
```

No enviar una imagen gigantesca al modelo si una resolución inferior
conserva todo el texto relevante.

Registrar:

``` text
original_resolution
processed_resolution
image_tokens
OCR_duration
VLM_duration
```

------------------------------------------------------------------------

# 29. Procesamiento multimodal: audio

Pipeline:

``` text
audio
  ↓
format detection
  ↓
normalization
  ↓
VAD
  ↓
speech segments
  ↓
transcription
```

No pasar silencios a Whisper/ASR.

Registrar:

``` text
audio_duration
speech_duration
silence_ratio
transcription_duration
real_time_factor
```

El objetivo es:

``` text
RTF < 1
```

y cuanto menor, mejor.

------------------------------------------------------------------------

# 30. Cache multimodal

Cachear resultados determinísticamente:

``` text
SHA256(file)
```

Si el fichero no ha cambiado:

``` text
OCR → cache hit
transcription → cache hit
extraction → cache hit
```

Y si solo cambia una etapa posterior, reutilizar los artifacts
anteriores.

------------------------------------------------------------------------

# 31. Scheduler y backpressure

Evitar que una etapa rápida produzca más trabajo del que la siguiente
puede consumir.

Ejemplo:

``` text
extraction
   ↓↓↓↓↓↓↓↓↓
entities
   ↓
inference
   ↓
   queue saturada
```

El scheduler debe conocer:

``` text
queue depth
worker capacity
GPU utilization
VRAM
```

y aplicar backpressure.

------------------------------------------------------------------------

# 32. Concurrencia configurable

Cada worker debe tener:

``` text
PREFETCH_COUNT
MAX_CONCURRENCY
BATCH_SIZE
```

configurables independientemente.

No asumir que aumentar concurrencia siempre aumenta throughput.

Hay que buscar el punto:

``` text
throughput máximo
+
latencia aceptable
+
sin OOM
```

------------------------------------------------------------------------

# 33. Métricas obligatorias

## Por job

``` text
job_duration
documents_processed
chunks_generated
entities_found
inferences_generated
cache_hits
cache_misses
```

## Por stage

``` text
stage_duration
stage_queue_time
stage_execution_time
stage_failures
stage_retries
```

## GPU

``` text
gpu_utilization
gpu_memory_used
gpu_memory_free
temperature
power
```

## Modelos

``` text
tokens/sec
requests/sec
batch_size
TTFT
TPOT
```

------------------------------------------------------------------------

# 34. Métrica crítica: queue time

No confundir:

``` text
processing_time
```

con:

``` text
queue_time + processing_time
```

Un worker puede procesar en 5 segundos, pero si espera 20 segundos en
RabbitMQ, el usuario percibirá 25 segundos.

Por eso registrar:

``` text
queued_at
started_at
completed_at
```

para cada stage.

------------------------------------------------------------------------

# 35. Benchmark suite

Crear un conjunto fijo de documentos:

``` text
01_small_text_pdf
02_large_text_pdf
03_scanned_pdf
04_legal_pdf
05_table_heavy_pdf
06_image
07_audio_short
08_audio_long
09_spreadsheet
10_mixed_document
```

Cada cambio debe compararse contra esta suite.

Métricas:

``` text
P50 latency
P95 latency
throughput
GPU utilization
VRAM
CPU utilization
RAM
queue time
```

------------------------------------------------------------------------

# 36. Objetivos de rendimiento

No fijar todavía números absolutos sin benchmark.

Primera fase:

1.  Establecer baseline.
2.  Identificar top 3 cuellos de botella.
3.  Optimizar.
4.  Volver a medir.
5.  Mantener una regresión automática.

Objetivo:

``` text
latency ↓
throughput ↑
cost per document ↓
quality ≈
```

La calidad no debe sacrificarse silenciosamente por velocidad.

------------------------------------------------------------------------

# 37. Plan de implementación recomendado

## Fase 1 --- Observabilidad

-   [ ] Instrumentar cada stage.
-   [ ] Medir queue time.
-   [ ] Medir CPU/GPU.
-   [ ] Medir tokens/sec.
-   [ ] Crear benchmark suite.
-   [ ] Crear dashboard Prometheus/Grafana.

## Fase 2 --- Optimizaciones de bajo riesgo

-   [ ] Benchmark BGE-M3 batch size.
-   [ ] Benchmark GLiNER batch size.
-   [ ] Paralelizar regex + GLiNER.
-   [ ] Optimizar text analysis.
-   [ ] Separar metadata fast/deep.
-   [ ] Eliminar trabajo innecesario.
-   [ ] Revisar inference concurrency.

## Fase 3 --- Optimizaciones estructurales

-   [ ] Cache de artifacts.
-   [ ] Idempotencia.
-   [ ] Versionado.
-   [ ] Evitar serialización duplicada.
-   [ ] Optimizar deduplicación.
-   [ ] Agrupar inferencias.
-   [ ] Scheduler con backpressure.

## Fase 4 --- Evolución arquitectónica

-   [ ] PipelineDefinition.
-   [ ] DAG.
-   [ ] Stage interface.
-   [ ] Artifact model.
-   [ ] Event system.
-   [ ] Cancelación.
-   [ ] Scheduler GPU.
-   [ ] Processing profiles.
-   [ ] Resultados parciales.

## Fase 5 --- Multimodal

-   [ ] Image preprocessing.
-   [ ] VAD.
-   [ ] Audio pipeline.
-   [ ] Multimodal cache.
-   [ ] Métricas multimodales.

------------------------------------------------------------------------

# 38. Prioridad inmediata

La primera implementación NO debería intentar hacer todo
simultáneamente.

Orden recomendado:

``` text
1. PERFILAR
      ↓
2. Docling
      ↓
3. Inference
      ↓
4. GLiNER
      ↓
5. Embeddings
      ↓
6. Deduplicación
      ↓
7. Serialization/I/O
      ↓
8. Arquitectura DAG
```

El criterio debe ser:

> Optimizar primero lo que más tiempo consume, no lo que resulta más
> divertido de programar.

------------------------------------------------------------------------

# 39. Resultado esperado

La arquitectura final debería permitir:

``` text
                  ┌───────────────┐
                  │   Job API     │
                  └───────┬───────┘
                          │
                          v
                  ┌───────────────┐
                  │   Scheduler   │
                  └───────┬───────┘
                          │
                    Pipeline DAG
                          │
        ┌─────────────────┼─────────────────┐
        v                 v                 v
   Extraction          Metadata          Chunking
        │                                   │
        │                     ┌─────────────┼─────────────┐
        │                     v             v             v
        │                 Entities     Embeddings     ...
        │                     │             │
        │                     └──────┬──────┘
        │                            v
        │                        Inference
        │                            │
        │                            v
        │                   Inference embeddings
        │
        └─────────────────────────────────────────┐
                                                  v
                                           Artifact Store
                                                  │
                                                  v
                                            Agent / API
```

El objetivo final no es simplemente que textFlow procese un documento
más rápido.

Es que **textFlow pueda decidir dinámicamente qué procesar, dónde
procesarlo, cuándo procesarlo y qué resultados reutilizar**, manteniendo
el sistema desacoplado y preparado para crecer con más modelos y más
hardware.

------------------------------------------------------------------------

# 40. Criterio de aceptación

Una mejora se considera válida únicamente si cumple al menos uno de
estos criterios sin degradación significativa de calidad:

-   Reduce P50/P95 de latencia.
-   Aumenta documentos/segundo.
-   Aumenta chunks/segundo.
-   Aumenta tokens/segundo.
-   Reduce utilización de CPU/GPU para el mismo trabajo.
-   Reduce memoria.
-   Reduce tráfico/serialización.
-   Reduce coste computacional.
-   Permite reutilizar artifacts.
-   Mejora la capacidad de recuperación ante fallos.

Toda optimización de rendimiento debe acompañarse de benchmark
antes/después.

------------------------------------------------------------------------

## Apéndice A --- Checklist técnico

### Arquitectura

-   [ ] DAG
-   [ ] PipelineDefinition
-   [ ] Stage interface
-   [ ] Artifact model
-   [ ] Versioning
-   [ ] Idempotency
-   [ ] Event system
-   [ ] Cancellation
-   [ ] GPU scheduler

### Extraction

-   [ ] PDF type detection
-   [ ] OCR only when needed
-   [ ] Docling benchmark
-   [ ] Metadata fast path
-   [ ] Text analysis optimization
-   [ ] Chunking benchmark

### NLP

-   [ ] BGE-M3 batch benchmark
-   [ ] GLiNER batch benchmark
-   [ ] Regex parallelization
-   [ ] Entity dedup optimization
-   [ ] Embedding cache

### LLM

-   [ ] Inference queue profiling
-   [ ] Queue time
-   [ ] Continuous batching
-   [ ] Inference batching
-   [ ] Concurrency benchmark
-   [ ] Inference embedding batching

### Infrastructure

-   [ ] Redis payload benchmark
-   [ ] RabbitMQ payload benchmark
-   [ ] MessagePack benchmark
-   [ ] Artifact storage
-   [ ] Backpressure
-   [ ] GPU scheduling

### Multimodal

-   [ ] Image resize
-   [ ] Image token metrics
-   [ ] Audio VAD
-   [ ] Audio normalization
-   [ ] ASR benchmark
-   [ ] Multimodal cache

### Observability

-   [ ] P50/P95
-   [ ] Queue time
-   [ ] Stage time
-   [ ] GPU utilization
-   [ ] VRAM
-   [ ] CPU
-   [ ] Tokens/sec
-   [ ] Cache hit ratio

------------------------------------------------------------------------

**Principio rector:** primero medir, después optimizar. El sistema debe
ganar velocidad reduciendo trabajo innecesario y explotando paralelismo,
no simplemente añadiendo más workers hasta que las GPUs empiecen a mirar
a RabbitMQ con resentimiento.
