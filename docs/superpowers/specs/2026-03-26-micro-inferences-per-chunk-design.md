# Diseño: Micro-inferencias por chunk (Fan-out con contador atómico)

**Fecha:** 2026-03-26  
**Estado:** Aprobado  
**Afecta a:** inference-worker, entities-worker, completion-worker

---

## Contexto y problema

La implementación actual del inference-worker procesa el documento **completo**:
- Lee `orchestrator:job:{id}:text` (documento entero)
- Trunca a 2000 caracteres (pérdida masiva de información)
- Un solo LLM call sin contexto de entidades
- No paralelizable (bloquea un worker por documento)

La documentación de SIIT (`separacion_orchestrator_ocugraphrag.md`) especifica
explícitamente que el inference-worker debe recibir
`{chunk_text, entities, source_type, job_id}` — es decir, operar por chunk.

---

## Decisión arquitectónica

Rediseñar el pipeline de micro-inferencias a **fan-out por chunk**:
el entities-worker publica N mensajes (uno por chunk con texto) en lugar de 1
mensaje por documento. Los inference-workers procesan en paralelo. Un contador
atómico Redis (`DECR`) detecta cuándo el último chunk termina y ensambla el
resultado final.

---

## Flujo de datos nuevo

```
entities-worker (fin de todos los chunks, si "inferences" ∈ features):
  1. Filtrar chunks con texto → valid_chunks (lista)
  2. Construir entities_by_chunk: {chunk_id: [entities]}
  3. Leer source_type desde Redis (source_classification) o usar "generico"
  4. SETEX orchestrator:job:{id}:inferences:remaining = len(valid_chunks), TTL=86400
  5. Publicar len(valid_chunks) mensajes a queue "inferences"

inference-worker (por mensaje, ejecutándose en paralelo):
  1. Parsear mensaje: {job_id, chunk_id, chunk_text, entities, source_type}
  2. Construir prompt con entidades como contexto
  3. LLM call → parsear JSON de respuesta
  4. RPUSH orchestrator:job:{id}:micro_inferences_raw  ← resultado del chunk
  5. remaining = DECR orchestrator:job:{id}:inferences:remaining
  6. Si remaining <= 0 (último chunk):
       a. LRANGE micro_inferences_raw 0 -1 → parsear cada elemento
       b. SET orchestrator:job:{id}:micro_inferences = <assembled_json>
       c. DEL micro_inferences_raw
       d. DEL orchestrator:job:{id}:inferences:remaining
       e. HSET steps "inferences" "completed"
       f. publish_job_progress(job_id, 80, "inferences")

completion-worker:
  Sin cambios en lógica de espera. Parsea micro_inferences con estructura
  agrupada por chunk_id en lugar de lista plana.
```

---

## Formato de mensajes

### Mensaje RabbitMQ → queue "inferences"
```json
{
  "job_id": "abc-123",
  "chunk_id": "chunk-4",
  "chunk_text": "El notario D. Francisco García autorizó la escritura...",
  "entities": [
    {"text": "Francisco García", "label": "PERSON", "confidence": 0.92},
    {"text": "450.000€", "label": "MONEY", "confidence": 0.99}
  ],
  "source_type": "notariado",
  "total_chunks": 12
}
```

### Resultado final en Redis `micro_inferences`
```json
[
  {
    "chunk_id": "chunk-4",
    "inferences": [
      {
        "text": "Francisco García autorizó escritura de compraventa por 450.000€",
        "confidence": 0.91,
        "entities": ["Francisco García", "450.000€"]
      }
    ]
  }
]
```

---

## Claves Redis

| Clave | Tipo | TTL | Descripción |
|-------|------|-----|-------------|
| `orchestrator:job:{id}:inferences:remaining` | STRING (int) | 24h | Contador atómico por job |
| `orchestrator:job:{id}:micro_inferences_raw` | LIST | 24h | Resultados intermedios por chunk |
| `orchestrator:job:{id}:micro_inferences` | STRING (json) | existente | Resultado final ensamblado |

Las claves intermedias son eliminadas por el último inference-worker al ensamblar.

---

## Prompt rediseñado

```
Dado el siguiente fragmento de texto y las entidades detectadas, extrae
todos los hechos concretos y verificables. Cada hecho debe mencionar al
menos una entidad detectada. Máximo 8 hechos.

Devuelve ÚNICAMENTE un array JSON con objetos que tengan:
- "text": la afirmación factual directa
- "confidence": valor entre 0.0 y 1.0
- "entities": lista de nombres de entidades mencionadas en el hecho

Entidades detectadas: {entities_str}

Fragmento de texto:
{chunk_text}

Hechos:
```

---

## Archivos modificados

### `cmd/entities-worker/worker.py`
- Bloque lines 694–717 (trigger actual): reemplazar por fan-out
- Añadir construcción de `entities_by_chunk` durante el loop principal (line 577)
- Leer `source_type` desde Redis antes del fan-out
- Usar conexión RabbitMQ existente del `connect_rabbitmq` context manager del `main()` — NO crear nueva conexión por job

### `cmd/inference-worker/worker.py`
- `process()`: nuevo mensaje format; eliminar lectura de texto completo
- `extract_inferences()`: firma nueva `(chunk_text, entities, source_type)`; prompt rediseñado; sin truncación a 2000 chars
- Añadir lógica RPUSH + DECR + ensamblaje condicional en `process()`
- Métricas existentes se mantienen

### `cmd/completion-worker/worker.py`
- `finalize_job()`: parsear `micro_inferences` como lista agrupada
- Log actualizado: contar total de inferences sumando `len(chunk["inferences"])` para cada chunk

---

## Manejo de errores

- **NACK con requeue=True**: el mensaje se reencola; DECR no se ejecuta hasta
  procesamiento exitoso → no se pierde ni se duplica el decremento
- **Chunk vacío (no hay entidades)**: entities-worker sólo publica mensajes para
  chunks que tienen texto; si entities está vacío se pasa `[]` y el prompt lo indica
- **LLM no configurado (LLM_URL vacío)**: se RPUSH `{chunk_id, inferences: []}` y
  se DECR igualmente → el step se completa con inferences vacías, no bloquea el job
- **Contador < 0**: condición `remaining <= 0` (no `== 0`) protege contra el
  edge case de procesamiento duplicado

---

## Lo que NO cambia

- Orquestrador Go: ningún cambio
- `docker-compose.yml`: ningún cambio
- Queue name `inferences`: se mantiene
- API externa del orchestrator: ningún cambio
- Estructura del job en Redis: se añaden 2 claves temporales, la final mantiene el mismo nombre

---

## Benchmark esperado

SIIT: 647 micros en un caso real → ~53 min end-to-end (28 docs, RTX 3090).
Con fan-out por chunk, el throughput de inferences escala linealmente con
el número de inference-workers. Con 3 workers GPU, el tiempo de inferences
se divide por ~3 frente al diseño secuencial actual.
