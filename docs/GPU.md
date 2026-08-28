# textFlow — GPU Guide

> Prerrequisito: lee `docs/MODELS.md` para el inventario de modelos y
> `docs/AIRGAPPED_DEPLOYMENT.md` para el flujo de bundle.

## Dos caminos (elegir uno)

### Camino estándar — NVIDIA Container Toolkit

Recomendado para instalaciones nuevas.

```bash
nvidia-smi                          # driver visible
docker info | grep -i runtime       # runtime nvidia registrado
docker run --rm --gpus all nvidia/cuda:12.8.0-base-ubuntu22.04 nvidia-smi
```

Compose:

```bash
docker compose -f deploy/docker/docker-compose.yml \
               -f deploy/docker/docker-compose.gpu.yml up -d
```

`docker-compose.gpu.yml` activa `runtime: nvidia` + `DOCLING_DEVICE=cuda`
en el servicio `docling` y `*_DEVICE=cuda` en workers que lo soportan.

### Camino host-specific — workaround nvidia-uvm en este host

`deploy/docker/docker-compose.yml` (cabecera `x-gpu-devices`) contiene un
workaround que bind-mounts los device nodes (`/dev/nvidia0:/dev/nvidia0`,
`nvidiactl`, `nvidia-modeset`, `nvidia-uvm`, `nvidia-uvm-tools`) y
`/usr/bin/nvidia-smi` porque `nvidia-container-toolkit` mapea `nvidia-uvm`
al major equivocado (511 = nvswitch, el kernel espera 510). Cuando un
servicio ya declara `volumes:`, un YAML merge `<<` silenciosamente pierde
`libcuda.so.1` — las entradas deben listar el volumen explícitamente.

Marcado en el Compose como *Host-specific NVIDIA workaround*. **No copiarlo**
como instalación genérica.

Prueba:

```bash
docker run --rm -v /dev/nvidiactl:/dev/nvidiactl:rw \
  --device /dev/nvidia0 nvidia/cuda:12.8.0-base-ubuntu22.04 nvidia-smi
```

## Docling y GPU

### Configuración actual

- `DOCLING_DEVICE=${DOCLING_DEVICE:-cpu}` (`deploy/docker/docker-compose.yml:docling` + `.env.example:DOCLING_DEVICE=auto` → CPU por defecto).
- `DOCLING_NUM_THREADS=${DOCLING_NUM_THREADS:-4}`.
- En `docker-compose.gpu.yml`: `DOCLING_DEVICE=cuda`, `NVIDIA_VISIBLE_DEVICES=0`, e
  **imagen** `quay.io/docling-project/docling-serve:cu128-0.12.0` (pin, no `latest`).

`DOCLING_DEVICE=cuda` **solo** funciona si la imagen contiene PyTorch/CUDA
compatible y Docker expone la GPU (test anterior).

Cuando la versión fijada de Docling publique nuevos knobs (`DOCLING_PERF_PAGE_BATCH_SIZE`,
`DOCLING_PERF_ELEMENTS_BATCH_SIZE`, etc.) se documentarán aquí. Ver
[Docling Serve deployment](https://docling-project.github.io/docling/usage/api_server/deployment/)
y [Docling GPU](https://docling-project.github.io/docling/usage/gpu/).

### Benchmark recomendado (antes de abrir el repo)

Corpus representativo, mismo contenido en CPU vs CUDA:

- `page_batch: 4 / 8 / 16 / 32 / 64`
- `EXTRACTION_CONCURRENCY: 1 / 2 / 4 / 5`
- Medir: `pages/sec`, `queue time`, `conversion time`, `P50`, `P95`,
  `GPU utilization`, `VRAM`, `CPU utilization`.
- Objetivo: `throughput ↑`, `P95` estable, sin OOM, misma calidad.

El `extraction-worker` controla `EXTRACTION_CONCURRENCY` (cuántos Doclings en
paralelo); Docling controla su page batching interno — dos niveles distintos.

## Otros workers y GPU

### Embeddings

- `EMBEDDINGS_DEVICE=cuda`, `EMBEDDING_BATCH_SIZE_GPU` — no fijar el óptimo sin
  benchmark en el hardware objetivo (`32/64/96/128`, medir `chunks/sec`,
  `tokens/sec`, `P50/P95`, VRAM).

### Entities (GLiNER)

- `ENTITIES_DEVICE=cuda`, `GLINER_BATCH_SIZE` — independiente de `EXTRACTION_CONCURRENCY`.

### Whisper (audio)

- `WHISPER_DEVICE=cpu|cuda`, `WHISPER_COMPUTE_TYPE=int8|float16`.
- `large-v2` ≈ 3 GB VRAM con `float16`; `int8` para CPU.

### Multimodal e inference LLM

- `image-analyzer` y `inference-worker` son clientes de un runtime LLM externo
  al bundle (Ollama/vLLM). Para air-gapped, provisionar runtime + modelo
  vision/instruct por separado (`docs/MODELS.md` §6–7).

## Troubleshooting

- `nvidia-smi` no visible → driver no instalado / `nvidia-container-toolkit` no configurado.
- `CUDA unknown error` → es el workaround `nvidia-uvm` — usa el Compose montado explícitamente, no `--gpus` CDI.
- `OSError: CUDA not available` en Docling/health → `DOCLING_DEVICE` quedó en `cpu`.
- OOM en Docling/workers → baja `DOCLING_PERF_PAGE_BATCH_SIZE` o `EXTRACTION_CONCURRENCY`.

## Referencias

- Docling GPU/deployment/offline: https://docling-project.github.io/docling/usage/gpu/
- Faster-Whisper offline: `faster_whisper.WhisperModel(path, local_files_only=True)`
