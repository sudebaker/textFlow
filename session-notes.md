## Session Notes — textFlow: fixes post-Fase 2 + smoke test inferencia mac-mini

### Cambios aplicados

#### 1. Reparación del contrato `BaseAsyncWorker` en audio/image workers
- **Archivos:** `cmd/audio-worker/worker.py`, `cmd/image-worker/worker.py`
- Renombrado `_process_message_async(self, message)` → `process_message(self, body)` para cumplir el método abstracto de `BaseAsyncWorker`.
- Eliminado el `async with message.process(requeue=False):` duplicado (la base ya gestiona el ack).
- Eliminado `body = json.loads(message.body)` (la base ya decodifica el dict).
- Reemplazado `AudioWorker().start()` / `ImageWorker().start()` por `asyncio.run(AudioWorker().run())` (la base no define `start()`).
- Eliminado `self.jobs_completed += 1` y `self.jobs_failed += 1` (la base ya incrementa el counter Prometheus `jobs_total`).
- En el `except` se conservó `self.event_bus.publish_job_failed(job_id, str(e))` y se eliminaron los sets Redis duplicados que ya hace la base.

#### 2. Fixes en `pkg/worker_common/async_base.py`
- Corregido el almacenamiento de job failed: ahora usa `hset(..., "status", "failed")` en lugar de `set(...)` para mantener compatibilidad con `orchestrator:job:{id}:status` esperado por el orchestrator Go.
- Sanitizado el nombre de métricas Prometheus: `worker_name.replace("-", "_")` para evitar nombres inválidos (`audio-worker_jobs_total`).
- Corregido import de `JSONResponse`: ahora se importa desde `fastapi.responses` para compatibilidad con FastAPI reciente.

#### 3. Fix en `pkg/worker_common/pubsub_base.py`
- Aplicadas las mismas correcciones que en `async_base.py`: sanitización de nombres de métricas y `JSONResponse` desde `fastapi.responses`.

#### 4. Limpieza de configs huérfanas
- **Comandos:** `rm -rf cmd/audio-worker/app/config cmd/image-worker/app/config`
- Eliminados `settings.py` y `__init__.py` duplicados que no se importaban desde `worker.py` tras la migración a `pydantic_settings` inline.

#### 5. Actualización de configuración de inferencia
- **Archivo:** `deploy/docker/.env`
- `LLM_URL=http://mac-mini:11434` → `LLM_URL=http://mac-mini:11234`.
- `LLM_MODEL=qwen3.5:9b-32k` → `LLM_MODEL=model` (descubierto vía `GET /v1/models` en mac-mini:11234).
- Corregidos comentarios inline que rompían el parseo de variables: `MAX_RETRIES`, `JOB_TTL`, `RETRY_DELAY`, nombres de colas y thresholds de entidades.

#### 6. Fix de parsing de vhost en RabbitMQ
- **Archivo:** `pkg/worker_common/rabbitmq.py`
- Cambiada la lógica `virtual_host=parsed.path[1:] if parsed.path else "/"` para que una URL terminada en `/` mapee al vhost `/` en lugar de una cadena vacía. Esto solucionaba el error `NOT_ALLOWED - vhost  not found` de pika.

#### 7. Dependencias Docker del inference worker
- **Archivo:** `cmd/inference-worker/requirements.txt`
- Añadidos `fastapi>=0.110.0`, `uvicorn>=0.27.0`, `httpx>=0.27.0` porque `pkg.worker_common.base` los importa.

#### 8. Smoke tests realizados
- **Audio/image workers:** arrancaron en contenedores Docker y respondieron `/health` healthy contra RabbitMQ/Redis (sin servicios whisper/multimodal-llm no se envió job real).
- **Inference worker end-to-end:**
  - Contenedor `textflow-inference` contra RabbitMQ/Redis de prueba.
  - Descubrimiento del modelo `model` en `http://mac-mini:11234/v1/models`.
  - Publicación manual a la cola `inferences` con texto de notariado.
  - Llamada real a `/v1/chat/completions` en mac-mini.
  - Resultado guardado en Redis: `orchestrator:job:smoke-test-001:micro_inferences`.

#### 9. Reindexación del proyecto
- **Comando:** `index_repository` de `codebase-memory-mcp` en modo `full` con `persistence=true`.
- Resultado: 3.477 nodos, 9.484 edges.

#### 10. Commits y push
- `17a64ee fix(Phase 2): repair BaseAsyncWorker contract for audio+image workers + inference endpoint`
- `99bb67b fix(rabbitmq,inference): correct vhost parsing and add missing inference deps`
- Push a `origin/main`: `46d1200..99bb67b`.

---

### Decisiones tomadas

1. **Patrón de contrato `BaseAsyncWorker`: dict vs `IncomingMessage`.**
   - Se mantuvo el patrón de la base: `process_message(self, body)` recibe el JSON ya decodificado. La base gestiona el ack y el contador de métricas.
   - **Alternativa descartada:** pasar el `IncomingMessage` a los workers. Habría requerido quitar el wrapper `async with message.process()` de la base y duplicar lógica de ack en cada worker.

2. **Eventos de fallo en workers (no en la base).**
   - Se conservó `self.event_bus.publish_job_failed(job_id, str(e))` en audio/image workers.
   - **Alternativa descartada:** mover la publicación del evento a `BaseAsyncWorker`. Rechazada para no ampliar el scope de cambios en la base y evitar efectos laterales en otros workers derivados.

3. **Sanitización de nombres de métricas en la base.**
   - Reemplazar `"-"` por `"_"` en `worker_name` al registrar métricas Prometheus. Es la solución más centralizada.
   - **Alternativa descartada:** cambiar los nombres de los workers para que nunca contengan guiones; es más invasiva y rompe convenciones de naming del proyecto.

4. **Commit separado para fixes de rabbitmq/inference.**
   - Se hizo un segundo commit (`99bb67b`) en lugar de amend de `17a64ee`. Así queda clara la línea temporal: primero fixes de fase 2, luego fixes descubiertos durante el smoke test de inferencia.

5. **Reindexación completa del proyecto.**
   - Se eligió modo `full` con persistencia para que el grafo refleje los nuevos archivos (`async_base.py`, `pubsub_base.py`, `chunking.py`) y las eliminaciones de configs huérfanas.

---

### TODOs pendientes

- [ ] Actualizar `pkg/worker_common/example_worker.py`: está obsoleto, referencia módulos antiguos (`worker_common.config`, `worker_common.rabbitmq`, `worker_common.signals`). El `README.md` del paquete lo menciona como ejemplo.
- [ ] Verificar smoke test completo del pipeline de texto: `extraction-worker`, `embeddings-worker`, `entities-worker`, `metadata-worker`, `completion-worker`. Requieren modelos descargados y/o docling.
- [ ] Revisar `.env.example` y los defaults de `deploy/docker/docker-compose.yml` para sincronizar el nuevo endpoint de inferencia (`mac-mini:11234`, `model`).
- [ ] Considerar una red Docker no-internal para el `inference-worker` en producción si debe salir a `mac-mini` (la red `docker_datastore` actual es internal).
- [ ] Revisar advertencia de Pydantic `class-based config is deprecated` en los workers migrados y migrar a `ConfigDict`.
