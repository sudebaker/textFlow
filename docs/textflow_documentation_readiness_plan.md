# textFlow Documentation Readiness Plan

## Objetivo

Preparar textFlow para abrir el repositorio con una documentación reproducible y realmente operativa, no limitada a la API.

Debe cubrir:

- arquitectura;
- instalación local;
- instalación Docker;
- despliegue air-gapped;
- inventario y adquisición de modelos;
- verificación de modelos;
- GPU;
- configuración y tuning de Docling;
- inferencia LLM;
- multimodal;
- configuración;
- operaciones;
- troubleshooting;
- upgrades/rollback;
- seguridad;
- benchmarks.

**Regla:** todo comando documentado debe funcionar contra el commit documentado y todo modelo/imagen requerido debe tener un procedimiento inequívoco de adquisición y verificación.

---

# 1. Hallazgos importantes en el repositorio actual

La infraestructura ya está bastante madura, pero la documentación actual tiene varias inconsistencias que deben corregirse antes de publicar.

## 1.1. El proceso de descarga de modelos está incompleto

`make setup-models` ejecuta:

```bash
cd deploy/docker && python download-models.py
```

El script actual descarga únicamente:

```text
urchade/gliner_small-v2.1
BAAI/bge-m3
```

No descarga explícitamente:

- Whisper;
- artefactos de Docling;
- el backbone DeBERTa descrito en `.env.example`;
- el LLM multimodal;
- el LLM de inference.

Por tanto, **no debe afirmarse actualmente que `make setup-models` prepara todos los modelos de una instalación air-gapped**.

La solución correcta es convertir el proceso de preparación de modelos en una operación reproducible que produzca exactamente todo lo necesario para la configuración seleccionada.

---

# 2. Crear un inventario canónico de modelos

Crear:

```text
docs/MODELS.md
```

Debe ser la fuente de verdad.

Tabla mínima:

| Componente | Runtime | Modelo | Fuente | Ruta local | Obligatorio | GPU |
|---|---|---|---|---|---|---|
| Embeddings | embeddings-worker | `BAAI/bge-m3` | Hugging Face | `models/bge-m3` | Sí | Opcional |
| NER | entities-worker | `urchade/gliner_small-v2.1` | Hugging Face | `models/gliner-small-v2.1` | Sí | Opcional |
| Backbone NER | entities-worker | DeBERTa exacto | Fuente/version exactas | `models/deberta-v3-small` | Sí | Opcional |
| ASR | whisper | `large-v2` actual | Fuente exacta | `models/whisper/...` | Audio | Opcional |
| Docling | docling-serve | versión exacta | Docling | imagen/artifacts | PDF | Sí |
| Image analysis | image-analyzer | modelo configurado | runtime LLM | externo/local | Imagen | Sí |
| Inference | inference-worker | modelo configurado | runtime LLM | externo/local | Opcional | Sí |

Para cada modelo documentar:

```text
nombre
repositorio/fuente
revision/tag
ruta
tamaño
SHA-256
formato
cómo se descarga
cómo se valida
qué servicio lo consume
```

---

# 3. Hacer reproducible la descarga

`make setup-models` debe producir el árbol completo requerido.

Después de descargar:

```text
models/MANIFEST.txt
```

debe registrar:

```text
model
source
revision
path
size
sha256
```

No depender de `main`, `latest` ni revisiones flotantes para modelos de producción.

---

# 4. Whisper

El servicio Whisper utiliza actualmente:

```text
MODEL_SIZE=large-v2
MODEL_PATH=/models
local_files_only=True
```

Por tanto el modelo debe estar disponible antes de arrancar el servicio.

El script actual de descarga de modelos no lo prepara.

Debe existir un procedimiento explícito que produzca, por ejemplo:

```text
models/
└── whisper/
    └── large-v2/
```

con la estructura exacta que espera `faster-whisper`.

La validación debe instanciar el modelo realmente en modo offline.

---

# 5. DeBERTa / GLiNER

El `entities-worker` utiliza:

```text
GLINER_MODEL_PATH=/models/gliner-small-v2.1
GLINER_BACKBONE_PATH=/models/deberta-v3-small
```

La documentación debe comprobar el loader real y explicar:

1. si el backbone es un artefacto separado;
2. qué repositorio exacto lo proporciona;
3. qué revisión se utiliza;
4. qué ficheros son obligatorios;
5. cómo se descarga;
6. cómo se valida.

No documentar un directorio únicamente porque aparezca en `.env.example`.

---

# 6. Docling: arquitectura real

El extraction worker **no ejecuta Docling directamente**.

El flujo es:

```text
orchestrator
    ↓
RabbitMQ
    ↓
extraction-worker
    ↓ HTTP async
docling-serve
    ↓
documento procesado
```

Por tanto el cuello de botella de extracción PDF está principalmente dentro de:

```text
docling-serve
```

y no en el orchestrator.

El extraction worker ya soporta concurrencia de jobs y polling asíncrono.

---

# 7. Docling: configuración actual

El Compose actual configura:

```text
DOCLING_DEVICE=${DOCLING_DEVICE:-cpu}
DOCLING_NUM_THREADS=${DOCLING_NUM_THREADS:-4}
```

El override GPU añade:

```text
DOCLING_DEVICE=cuda
NVIDIA_VISIBLE_DEVICES=0
```

Esto debe documentarse.

Pero `DOCLING_DEVICE=cuda` **no es suficiente por sí mismo**. El contenedor debe contener PyTorch/CUDA compatible y Docker debe exponer correctamente la GPU.

---

# 8. Docling: usar una imagen CUDA explícita

El Compose actual usa:

```text
quay.io/docling-project/docling-serve:latest
```

Esto no es suficientemente reproducible para una instalación GPU.

La documentación oficial de Docling Serve proporciona imágenes CUDA específicas, actualmente de la familia:

```text
docling-serve-cu128
docling-serve-cu130
```

con tags explícitos.

El proyecto debe:

1. seleccionar una versión concreta;
2. seleccionar la variante CUDA compatible con el driver objetivo;
3. fijar tag;
4. idealmente fijar digest;
5. usar esa imagen en Compose;
6. empaquetar exactamente esa imagen en el bundle air-gapped.

No usar `latest` en producción.

---

# 9. Docling y modelos offline

La documentación oficial de Docling proporciona:

```bash
docling-tools models download
```

para prefetchar modelos para uso offline.

textFlow debe elegir **una única estrategia documentada**:

### Opción recomendada

Usar una imagen Docling Serve CUDA fijada que contenga los artefactos requeridos y empaquetarla completa.

### Alternativa

Externalizar los artifacts:

```text
models/docling/
```

y montarlos explícitamente en el contenedor, configurando el artifacts path de Docling Serve.

Lo que no debe ocurrir es documentar `models/docling/` si Compose no lo monta ni Docling lo utiliza.

---

# 10. Inconsistencia actual de Docling en la documentación

La documentación actual describe:

```text
models/docling/
```

pero el Compose actual no monta esa ruta en el servicio `docling`.

Esto debe resolverse antes de publicar.

La documentación debe responder inequívocamente:

> ¿Dónde están los modelos de Docling en una instalación air-gapped?

Y la respuesta debe coincidir exactamente con Compose.

---

# 11. Docling GPU: documentación específica

Crear:

```text
docs/GPU.md
```

con una sección dedicada a Docling.

Documentar:

```text
DOCLING_DEVICE
DOCLING_NUM_THREADS
DOCLING_PERF_PAGE_BATCH_SIZE
DOCLING_PERF_ELEMENTS_BATCH_SIZE
```

cuando estén disponibles en la versión fijada.

La guía oficial de Docling recomienda configurar explícitamente CUDA y ajustar batch sizes para GPU.

No publicar un batch size arbitrario como valor óptimo.

---

# 12. Benchmark de Docling

Crear una prueba reproducible con un corpus representativo.

Medir:

```text
pages/sec
queue time
conversion time
P50
P95
GPU utilization
VRAM
CPU utilization
```

Probar, como mínimo:

```text
page batch:
4
8
16
32
64
```

y:

```text
EXTRACTION_CONCURRENCY:
1
2
4
5
...
```

por separado.

La combinación óptima es la que maximiza:

```text
throughput
```

sin aumentar excesivamente:

```text
P95
VRAM
OOM
```

---

# 13. Docling: standard pipeline vs VLM

No confundir:

### Standard PDF pipeline

```text
PDF
 ↓
parse
 ↓
layout
 ↓
tables
 ↓
OCR si hace falta
 ↓
structured document
```

### VLM pipeline

```text
page image
 ↓
VLM
 ↓
DocTags/structured representation
```

El VLM no debe activarse globalmente sólo porque exista GPU.

Para PDF normales con capa de texto, el standard pipeline debe ser el camino rápido.

---

# 14. OCR

El extraction worker actual tiene:

```text
DOCLING_DO_OCR=false
DOCLING_OCR_ENGINE=rapidocr
```

Esto debe explicarse claramente.

Regla:

```text
PDF con texto
    → no OCR

PDF escaneado
    → OCR
```

No activar OCR globalmente.

Si se documenta OCR GPU, indicar exactamente:

- engine;
- backend;
- modelo;
- idiomas;
- memoria;
- configuración.

La documentación oficial de Docling identifica RapidOCR con backend Torch como una ruta GPU conocida.

Si textFlow no lo configura actualmente, documentarlo como optimización futura, no como capacidad ya activada.

---

# 15. Extraction concurrency vs Docling concurrency

El extraction worker utiliza:

```text
EXTRACTION_CONCURRENCY
```

mientras Docling tiene sus propios mecanismos internos de batch/concurrency.

Son dos niveles diferentes:

```text
textFlow
    EXTRACTION_CONCURRENCY
          ↓
      Docling jobs
          ↓
Docling page batching
          ↓
        GPU
```

Un valor demasiado alto puede empeorar:

- VRAM;
- CPU;
- P95;
- throughput real.

Debe medirse.

---

# 16. Embeddings GPU

Documentar:

```text
EMBEDDINGS_DEVICE=cuda
EMBEDDING_BATCH_SIZE_GPU
```

Benchmark recomendado:

```text
32
64
96
128
```

y medir:

```text
chunks/sec
tokens/sec
P50
P95
GPU utilization
VRAM
```

No fijar el valor óptimo sin benchmark en el hardware objetivo.

---

# 17. GLiNER GPU

Documentar:

```text
ENTITIES_DEVICE=cuda
GLINER_BATCH_SIZE
```

Explicar claramente que:

```text
GLINER_BATCH_SIZE
```

y:

```text
EXTRACTION_CONCURRENCY
```

son parámetros de niveles diferentes.

---

# 18. Image analysis

El image analyzer utiliza:

```text
LLM_BASE_URL
MULTIMODAL_LLM_MODEL
```

Debe documentarse que el LLM puede ser externo al bundle de textFlow.

Para una instalación realmente air-gapped hay que provisionar también:

```text
runtime LLM
modelo multimodal
```

si se usa esta capacidad.

Si el backend es Ollama, documentar:

```text
instalación
modelo
pull en máquina online
export/transfer
import en máquina air-gapped
```

No afirmar que textFlow contiene el modelo multimodal si no lo empaqueta.

---

# 19. Inference LLM

Documentar:

```text
LLM_URL
LLM_MODEL
INFERENCE_LLM_TIMEOUT
INFERENCE_MAX_CONCURRENCY
INFERENCE_WORKER_REPLICAS
```

Explicar que textFlow es un consumidor de un runtime LLM local/OpenAI-compatible.

Documentar por separado:

```text
vLLM
Ollama
otro servidor compatible
```

y cómo se provisiona cada uno offline.

---

# 20. Docker GPU

Documentar dos caminos.

### Camino estándar

NVIDIA Container Toolkit + runtime compatible.

### Camino específico de este host

El Compose actual contiene un workaround para un problema concreto con `nvidia-uvm` y el major del device.

Ese workaround debe estar marcado como:

> Host-specific NVIDIA workaround

No convertirlo en la instalación NVIDIA genérica de textFlow.

Documentar:

```bash
nvidia-smi
docker info | grep -i runtime
```

y una prueba real de GPU dentro de un contenedor.

---

# 21. Air-gapped: separación de fases

La documentación debe separar:

```text
ONLINE BUILD MACHINE
```

de:

```text
AIR-GAPPED TARGET
```

La máquina online prepara:

- Docker images;
- Python dependencies;
- Go dependencies;
- ML models;
- LLM models/runtimes cuando correspondan;
- bundle;
- manifests.

El target sólo necesita:

- Docker;
- Compose;
- GPU runtime/driver si corresponde;
- bash;
- curl;
- almacenamiento;
- bundle.

El target **no debe ejecutar `pip install`, `go mod download`, `docker pull` ni descargas Hugging Face**.

---

# 22. Bundle air-gapped

`make package` debe ser el punto único de preparación.

La documentación debe tratar:

```text
dist/MANIFEST.txt
```

como la fuente de verdad del bundle concreto.

Estructura:

```text
dist/
├── images/
├── models.tar.gz
├── config/
├── install.sh
├── lib.sh
└── MANIFEST.txt
```

El manifest debe registrar:

- commit;
- timestamp;
- Docker version;
- imágenes;
- digests;
- models archive SHA-256;
- tamaño.

---

# 23. Bug de packaging que debe corregirse antes de publicar

Actualmente `package.sh` contiene:

```text
rabbitmq:3.12-management
```

mientras Compose utiliza:

```text
rabbitmq:3.13-management
```

Esto puede romper una instalación air-gapped.

**Solución recomendada:** no duplicar manualmente imágenes externas en `package.sh`. Derivarlas de Compose.

---

# 24. Pinning de imágenes

No usar:

```text
:latest
```

para componentes críticos.

Pinning mínimo:

```text
Docling Serve
RabbitMQ
Redis
Prometheus
Grafana
```

Idealmente:

```text
image:tag@sha256:digest
```

o registrar el digest exacto en el manifest.

---

# 25. Air-gapped smoke test

No utilizar una URL externa en el smoke test.

Evitar:

```json
{
  "document_url": "http://example.com/test.pdf"
}
```

Usar un fixture local:

```bash
curl -X POST   http://localhost:8080/v1/documents/upload   -F "file=@tests/fixtures/sample.pdf"
```

El test debe validar:

```text
upload
→ extraction
→ chunks
→ entities
→ completion
→ result
```

y opcionalmente:

```text
→ embeddings
→ inferences
```

según el profile.

---

# 26. Verificación de bundle

Añadir un verificador que compruebe:

```text
imágenes presentes
modelos presentes
manifest válido
SHA-256 válido
Compose válido
variables requeridas
```

Antes de transferir:

```bash
verify-bundle.sh
```

En destino:

```bash
verify-installation.sh
```

La instalación no debería depender de descubrir errores después de cortar la red.

---

# 27. Documentación recomendada

El repositorio debería quedar organizado así:

```text
README.md

docs/
├── ARCHITECTURE.md
├── API.md
├── INSTALLATION.md
├── AIRGAPPED_DEPLOYMENT.md
├── MODELS.md
├── GPU.md
├── PERFORMANCE.md
├── CONFIGURATION.md
├── TROUBLESHOOTING.md
├── COMPATIBILITY.md
├── OPERATIONS.md
└── SECURITY.md

docs/swagger/

examples/
├── basic/
├── gpu/
├── airgapped/
└── multimodal/
```

---

# 28. README

Debe responder rápidamente:

1. Qué es textFlow.
2. Qué procesa.
3. Qué devuelve.
4. Cómo ejecutarlo.
5. Cómo ejecutarlo offline.
6. Dónde está la documentación completa.

No convertir README en un manual de 50 páginas.

---

# 29. CONFIGURATION.md

Generar una referencia completa a partir del `.env.example` y Compose.

Cada variable:

```text
nombre
default
tipo
valores válidos
servicio
efecto
recomendación
```

No documentar variables muertas.

---

# 30. ARCHITECTURE.md

Explicar:

```text
API
 ↓
RabbitMQ
 ↓
PipelineDefinition
 ↓
workers
 ↓
Artifact Store
 ↓
completion
 ↓
ProcessedDocument
```

Y responsabilidades de:

- Redis;
- RabbitMQ;
- EventBus;
- Artifact Store;
- profiles;
- stages;
- retry;
- cancellation;
- backpressure;
- multimodal.

---

# 31. PERFORMANCE.md

Separar:

```text
time_to_text
time_to_processed_document
```

y medir:

```text
queue time
stage duration
P50
P95
throughput
GPU utilization
VRAM
CPU
```

Debe explicar cómo reproducir benchmarks.

---

# 32. TROUBLESHOOTING.md

Cubrir:

### Docker

- imágenes inexistentes;
- permisos;
- Compose.

### GPU

- `nvidia-smi`;
- GPU no visible;
- CUDA/PyTorch mismatch;
- NVIDIA runtime;
- workaround de `/dev/nvidia-uvm`;
- OOM.

### Modelos

- modelo ausente;
- intento de acceso a Hugging Face;
- tokenizer incorrecto;
- modelo corrupto;
- ruta incorrecta.

### Docling

- fallback a CPU;
- CUDA no disponible;
- artifacts ausentes;
- OCR;
- OOM;
- timeout.

### RabbitMQ/Redis

- colas;
- backlog;
- workers;
- retries;
- estado.

### LLM

- Ollama/vLLM no disponible;
- modelo inexistente;
- timeout;
- saturación.

---

# 33. COMPATIBILITY.md

Crear una matriz:

| Componente | Versión | CUDA | Python | Driver mínimo | Notas |
|---|---|---|---|---|---|
| Docling Serve | fijada | CUDA fijada | -- | mínimo | |
| PyTorch | fijada | CUDA fijada | | | |
| embeddings | fijada | | 3.11 | | |
| entities | fijada | | 3.11 | | |
| Whisper | fijada | | 3.10 | | |

Especialmente importante para air-gapped.

---

# 34. Reproducibilidad

Registrar:

```text
git commit
build timestamp
Docker version
host architecture
CUDA version
NVIDIA driver
image digests
model revisions
model hashes
```

El bundle debe ser autocontenido y auditable.

---

# 35. Documentation CI

Añadir validaciones para evitar drift:

- Make targets documentados existen;
- archivos referenciados existen;
- variables documentadas existen;
- Compose y package.sh usan las mismas imágenes;
- modelos del inventario coinciden con el downloader;
- ejemplos API coinciden con OpenAPI;
- bundle air-gapped contiene lo necesario.

La documentación no debería depender de memoria humana.

---

# 36. Prioridad inmediata antes de abrir el repo

## P0

- [ ] Corregir `rabbitmq:3.12` vs `3.13`.
- [ ] Hacer que la preparación de modelos descargue todo lo realmente necesario.
- [ ] Añadir Whisper al proceso de preparación.
- [ ] Resolver estrategia de artifacts de Docling.
- [ ] Fijar imagen/version CUDA de Docling.
- [ ] Eliminar documentación falsa sobre `models/docling/` si no se monta.
- [ ] Cambiar smoke test air-gapped a fixture local.
- [ ] Validar Make targets y documentación.

## P1

- [ ] `MODELS.md`.
- [ ] Manifest de modelos.
- [ ] Verificación de bundle.
- [ ] Pinning de imágenes.
- [ ] `GPU.md`.
- [ ] Compatibilidad GPU/CUDA/driver.
- [ ] Docling GPU benchmark.
- [ ] Documentar extraction concurrency vs Docling batching.

## P2

- [ ] `ARCHITECTURE.md`.
- [ ] `PERFORMANCE.md`.
- [ ] `CONFIGURATION.md`.
- [ ] `TROUBLESHOOTING.md`.
- [ ] `OPERATIONS.md`.
- [ ] `SECURITY.md`.
- [ ] Ejemplos reproducibles.

## P3

- [ ] Documentation CI.
- [ ] OpenAPI validation.
- [ ] Compose/package consistency checks.
- [ ] Model inventory validation.
- [ ] Offline installation smoke test automatizado.

---

# 37. Recomendación concreta para el cuello de botella actual

Antes de abrir el repo, hacer un benchmark específico de Docling.

Primera comparación:

```text
Docling CPU
vs
Docling CUDA
```

con exactamente el mismo corpus.

Después:

```text
page_batch_size:
4 / 8 / 16 / 32 / 64
```

y:

```text
EXTRACTION_CONCURRENCY:
1 / 2 / 4 / 5
```

Medir:

```text
pages/sec
P50
P95
GPU utilization
VRAM
CPU utilization
```

La métrica objetivo no es simplemente `GPU utilization = 100%`.

Es:

```text
throughput ↑
latency ↓
P95 estable
VRAM estable
sin OOM
misma calidad
```

La documentación oficial de Docling recomienda CUDA y ajustar batch/concurrency para maximizar el rendimiento. La configuración final debe salir del benchmark del hardware real, no de un número copiado de Internet.

---

# 38. Definición de documentación completa

La documentación estará realmente terminada cuando un ingeniero que nunca haya visto textFlow pueda:

1. clonar el repositorio;
2. conocer los requisitos de hardware;
3. preparar todos los modelos en una máquina con internet;
4. construir las imágenes;
5. generar y verificar el bundle;
6. transferirlo a una máquina aislada;
7. instalar sin internet;
8. configurar GPU;
9. verificar que Docling utiliza CUDA;
10. procesar un PDF;
11. procesar una imagen;
12. procesar audio;
13. consultar progreso;
14. recuperar el resultado;
15. consultar Prometheus/Grafana;
16. diagnosticar un modelo ausente;
17. diagnosticar una GPU no visible;
18. actualizar;
19. hacer rollback;
20. reproducir exactamente el despliegue usando los manifests.

Si el ingeniero tiene que preguntar:

> "¿Qué modelo tengo que descargar?"

o:

> "¿Por qué Docling está usando CPU?"

la documentación todavía no está terminada.

---

# Fuentes externas para la parte Docling

La parte específica de Docling debe mantenerse alineada con la documentación oficial:

- GPU: https://docling-project.github.io/docling/usage/gpu/
- Deployment/API server: https://docling-project.github.io/docling/usage/api_server/deployment/
- Offline model prefetch: https://docling-project.github.io/docling/usage/advanced_options/
- Installation/OCR: https://docling-project.github.io/docling/getting_started/installation/
- FAQ/model weights: https://docling-project.github.io/docling/faq/

La versión exacta de Docling Serve y la variante CUDA deben quedar fijadas por textFlow antes de publicar.
