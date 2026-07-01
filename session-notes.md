## Session Notes — Phase 2: Unificación de workers (audio, image, completion)

### Cambios aplicados

#### 1. pkg/worker_common/chunking.py (nuevo)
- Función `chunk_text(text: str, max_chars: int = 1500) -> list[dict]` compartida
- Reemplaza `chunk_text` local en image-worker
- Audio-worker conserva `SegmentChunker` propio por lógica speaker-aware

#### 2. pkg/worker_common/async_base.py (nuevo)
- `BaseAsyncWorker`: clase base para workers RabbitMQ async (aio_pika)
- Incluye: conexión robusta con `connect_robust`, QoS, métricas prometheus (`jobs_completed`, `jobs_failed`, `job_duration`), health server FastAPI (`/health`, `/metrics`, `/ready`), manejo de señales SIGINT/SIGTERM, event bus integration
- Método `_make_message(body: bytes)` helper para crear mensajes persistentes
- Método abstracto `_process_message_async(message)`

#### 3. pkg/worker_common/pubsub_base.py (nuevo)
- `BasePubSubWorker`: clase base para workers Redis pub/sub (completion-worker)
- Incluye: reconnect con exponential backoff, pubsub loop, métricas prometheus, health server FastAPI, signal handling
- Método abstracto `handle_event(message: dict)`

#### 4. cmd/audio-worker/worker.py
- `AudioWorker` ahora extiende `BaseAsyncWorker`
- `Settings` con `pydantic_settings.BaseSettings`, `env_prefix="AUDIO_"`
- Removido: `setup_logging`, `register_signal_handlers`, `start_http_server`, contadores prometheus manuales
- Métricas ahora via `BaseAsyncWorker.jobs_completed` / `jobs_failed`
- `UPLOAD_PATH` movido a `settings.upload_path`

#### 5. cmd/image-worker/worker.py
- `ImageWorker` ahora extiende `BaseAsyncWorker`
- `Settings` con `pydantic_settings.BaseSettings`, `env_prefix="IMAGE_"`
- Usa `chunking.chunk_text()` compartido (antes función local `_chunk_text`)
- Removido: `setup_logging`, `register_signal_handlers`, `start_http_server`, contadores prometheus manuales

#### 6. cmd/completion-worker/worker.py
- `CompletionWorker` ahora extiende `BasePubSubWorker`
- `Settings` inline con `pydantic_settings.BaseSettings` (sin env_prefix)
- Removido: setup manual de redis_client/redis_raw, signal handlers, health server, `start_http_server`, import de `app.config.settings`
- `handle_event()` overridea el abstracto de `BasePubSubWorker`

#### Commits realizados
```
672d9ad refactor(completion): migrate to BasePubSubWorker + pydantic_settings
9be7b0a refactor(image): migrate to BaseAsyncWorker + shared chunk_text
41439ba refactor(audio): migrate to BaseAsyncWorker + pydantic_settings
791462f feat(pubsub): add BasePubSubWorker for Redis pub/sub workers
56bc541 feat(async): add BaseAsyncWorker and shared chunking helper
```

---

### Decisiones tomadas

#### 1. extraction-worker: no migrado
- **Decisión**: No aplicar `BaseAsyncWorker` a extraction-worker
- **Justificación**: Usa `queue.iterator()` + `asyncio.create_task()` para procesar múltiples Docling jobs concurrentemente. El patrón de `BaseAsyncWorker` (un consumer simple con `queue.consume()`) no es compatible con este modelo de concurrencia.
- **Alternativa considerada**: Adaptar `BaseAsyncWorker` para soportar concurrency con iterator — descartado porque requiere cambios significativos a la base y extraction-worker ya tiene una arquitectura bien diseñada para su caso de uso.
- **Resultado**: extraction-worker queda con su arquitectura actual.

#### 2. audio-worker: conserva SegmentChunker propio
- **Decisión**: No reemplazar el `SegmentChunker` local por `chunk_text()` compartido
- **Justificación**: `SegmentChunker` agrupa segmentos por speaker (speaker-aware chunking), lo cual es específico de audio y no aplica a otros workers. La función compartida `chunk_text()` es character-based sin noción de speakers.
- **Resultado**: audio-worker mantiene su chunker especializado.

#### 3. app/config/settings.py en audio/image
- **Decisión**: Migrar a `pydantic_settings.BaseSettings` inline en worker.py
- **Justificación**: Los archivos `app/config/settings.py` existían pero no eran usados (los workers usaban variables globales `os.getenv`). La migración activa el uso de settings pydantics.
- **Nota**: Los archivos `app/config/settings.py` de audio e image workers quedaron sin uso — considerar eliminarlos en cleanup.

#### 4. image-worker: usa chunk_text() compartido
- **Decisión**: image-worker sí usa `chunking.chunk_text()` en lugar de función local
- **Justificación**: image-worker hacía chunking character-based simple (igual a `chunk_text()`), sin lógica especial. Unificación posible.
- **Contraste**: audio-worker no puede usarlo por el speaker-awareness.

---

### TODOs pendientes

- [ ] **Cleanup audio/image config/settings**: Los archivos `cmd/audio-worker/app/config/settings.py` y `cmd/image-worker/app/config/settings.py` quedaron huérfanos tras la migración (no se importan desde worker.py). Evaluar si eliminarlos o mantenerlos como referencia.

- [ ] **Cleanup example_worker.py**: Existe `cmd/example-worker/` que podría ser obsolete tras la unificación. Verificar si está en uso.

- [ ] **Dockerfiles**: Verificar que los Dockerfiles de los workers migrados no tengan referencias a imports o configuraciones antiguas (e.g., `app.config.settings`).

- [ ] **Verificación de builds**: Ejecutar `make build` para verificar que todos los workers compilan sin errores post-migración.

- [ ] **Verificación docker-compose**: Ejecutar `make docker-up` y verificar que los workers migrados inician correctamente y las métricas health endpoint responden.

- [ ] **extraction-worker**: No migrado a ninguna base. Considerar si vale la pena crear una variante de `BaseAsyncWorker` que soporte `queue.iterator()` + concurrent tasks, o dejar como está (arquitectura特意 diseñada para su caso).

- [ ] **Imports muertos**: Verificar que no queden imports no utilizados en los workers migrados (e.g., `aio_pika.abc`, `redis` directo cuando se usa vía base class).
