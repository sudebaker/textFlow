# GLiNER Modo Offline — Guía Completa

**Status:** ✅ RESUELTO Y FUNCIONANDO EN PRODUCCIÓN  
**Última actualización:** 2026-02-09

---

## Problema

El entities-worker fallaba en entornos air-gapped (sin acceso a internet) con:

```
OSError: We couldn't connect to 'https://huggingface.co' to load the files,
and couldn't find them in the cached files.
```

Incluso cuando:
- Variables `HF_HUB_OFFLINE=1` y `TRANSFORMERS_OFFLINE=1` estaban configuradas
- Archivos del modelo GLiNER y DeBERTa estaban presentes en disco
- `local_files_only=True` se pasaba a `GLiNER.from_pretrained()`
- Se intentaban monkey-patches para forzar modo offline

Resultado: el contenedor entraba en estado `Restarting` continuamente.

---

## Análisis de Causa Raíz

El problema tiene **dos causas raíz** que actúan de forma independiente.

### Causa 1: `model_info()` en `huggingface_hub`

GLiNER internamente llama a `model_info()` de `huggingface_hub`. Cuando `HF_HUB_OFFLINE=1` está configurado globalmente y los archivos **no existen en la estructura de caché estándar de HuggingFace**, esta llamada falla:

```
huggingface_hub.errors.LocalEntryNotFoundError: Cannot find the requested
files in the disk cache and outgoing traffic has been disabled.
```

**Stack trace:** el error ocurre dentro del código de GLiNER, no en transformers.

### Causa 2: `model_name` tratado como repo ID de Hub

GLiNER guarda en `gliner_config.json` el nombre del backbone:

```json
"model_name": "microsoft/deberta-v3-small"
```

Cuando GLiNER carga el tokenizador, transformers ejecuta `AutoTokenizer.from_pretrained(config.model_name)`. El valor se interpreta como un **identificador de HuggingFace Hub**, no como una ruta local, y transformers busca en la estructura de caché de HF (snapshots con symlinks → blobs). Si esa estructura no existe, intenta descargar → falla en modo offline.

---

## Estrategias de Solución

### Estrategia A: Directorio plano + `model_name` como ruta local (Alternativa)

**Problema que resuelve:** Causa 2 (sin tocar Causa 1).

**Enfoque:** Modificar `gliner_config.json` para que `model_name` apunte a una ruta absoluta local y NO usar `HF_HUB_OFFLINE=1` globalmente.

1. Cambiar `model_name` en `gliner_config.json`:
   ```json
   "model_name": "/models/deberta-v3-large"
   ```

2. Copiar archivos de DeBERTa a un **directorio plano** (sin estructura de caché HF) dentro del contenedor:
   ```dockerfile
   RUN mkdir -p /models/deberta-v3-large && \
       cp -r /home/app/.cache/huggingface/models--microsoft--deberta-v3-large/snapshots/*/. /models/deberta-v3-large/
   ```

3. **No** configurar `HF_HUB_OFFLINE=1` ni `TRANSFORMERS_OFFLINE=1` globalmente.

4. Usar `local_files_only=True` al cargar GLiNER:
   ```python
   model = GLiNER.from_pretrained("/models/gliner_model", local_files_only=True)
   ```

**Ventaja:** Elimina la dependencia de la estructura interna de caché de HF.  
**Desventaja:** Más pasos manuales, menos estándar, no reutiliza el mecanismo de caché existente de HF.  
**Estado:** No implementada en este proyecto.

### Estrategia B: Estructura de caché HuggingFace ✅ (Implementada)

**Problema que resuelve:** Causas 1 y 2 simultáneamente.

**Enfoque:** Usar `snapshot_download()` para crear la estructura de caché estándar de HF dentro de la imagen Docker, y mantener `HF_HUB_OFFLINE=1`.

**Por qué funciona:** Con la estructura de caché correcta (snapshots + blobs + symlinks), `model_info()` encuentra los archivos localmente y no intenta conectarse al Hub. El flag `HF_HUB_OFFLINE=1` funciona correctamente cuando el caché está presente.

**Estado:** ✅ IMPLEMENTADA Y FUNCIONANDO EN PRODUCCIÓN.

---

## Implementación Final (Estrategia B)

### 1. Script de descarga: `deploy/docker/download_models_offline.py`

```python
from huggingface_hub import snapshot_download
import os

CACHE_DIR = "models/huggingface_cache"

snapshot_download(
    repo_id="urchade/gliner_small-v2.1",
    cache_dir=CACHE_DIR,
)
snapshot_download(
    repo_id="microsoft/deberta-v3-small",
    cache_dir=CACHE_DIR,
)
```

Crea la estructura de caché correcta automáticamente:

```
models/huggingface_cache/hub/
├── models--urchade--gliner_small-v2.1/
│   ├── refs/
│   │   └── main
│   ├── snapshots/<hash>/
│   │   ├── pytorch_model.bin → ../../blobs/<hash>
│   │   └── gliner_config.json → ../../blobs/<hash>
│   └── blobs/<hash-files>
├── models--microsoft--deberta-v3-small/
│   ├── refs/
│   │   └── main
│   ├── snapshots/<hash>/
│   │   ├── config.json → ../../blobs/<hash>
│   │   ├── pytorch_model.bin → ../../blobs/<hash>
│   │   ├── tokenizer_config.json → ../../blobs/<hash>
│   │   └── spm.model → ../../blobs/<hash>
│   └── blobs/<hash-files>
```

**Modelos descargados:**
- `urchade/gliner_small-v2.1` (611 MB)
- `microsoft/deberta-v3-small` (289 MB)

### 2. Worker modificado: `cmd/entities-worker/worker.py`

**Requisito crítico:** Configurar variables de entorno **ANTES** de cualquier import de GLiNER:

```python
import os
import sys

os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["HF_DATASETS_OFFLINE"] = "1"
os.environ["HF_HOME"] = "/home/app/.cache/huggingface"

from gliner import GLiNER
```

Función `load_model()` simplificada (eliminada lógica compleja):

```python
def load_model(self):
    self.model = GLiNER.from_pretrained(
        "/models/gliner-small-v2.1",
        local_files_only=True,
    )
```

**Eliminado:** `model_name`, `config`, lógica compleja de resolución de backbone.

### 3. Dockerfile: `cmd/entities-worker/Dockerfile`

```dockerfile
# Configurar offline mode ANTES de instalar dependencias
ENV HF_HUB_OFFLINE=1
ENV HF_HOME=/home/app/.cache/huggingface
ENV TRANSFORMERS_OFFLINE=1
ENV HF_DATASETS_OFFLINE=1

# Copiar caché HF completo con estructura (snapshots + blobs + symlinks)
COPY --chown=app:app models/huggingface_cache/hub /home/app/.cache/huggingface/hub

# Copiar modelo GLiNER a ruta estándar
COPY --chown=app:app models/gliner-small-v2.1 /models/gliner-small-v2.1
```

### 4. Entrypoint con pre-flight checks: `cmd/entities-worker/entrypoint.sh`

```bash
#!/bin/bash
set -e

echo "🔍 Verifying offline configuration..."

# Verificar que existe el caché de HuggingFace
if [ ! -d "/home/app/.cache/huggingface/hub" ]; then
    echo "❌ ERROR: HuggingFace cache not found!"
    exit 1
fi

# Verificar que existe deberta-v3-small en caché
DEBERTA_CACHE="/home/app/.cache/huggingface/hub/models--microsoft--deberta-v3-small"
if [ ! -d "$DEBERTA_CACHE" ]; then
    echo "❌ ERROR: DeBERTa not found in cache"
    exit 1
fi

echo "✅ Pre-flight checks passed"
exec python worker.py
```

**Pre-flight checks ejecutados:**
- ✅ Verifica que existe `/home/app/.cache/huggingface/hub/`
- ✅ Verifica que existe `models--microsoft--deberta-v3-small/`
- ✅ Verifica archivos de GLiNER en `/models/gliner-small-v2.1/`
- ✅ Lista contenido del caché para debugging
- ❌ Falla rápido con mensaje claro si falta algo

### 5. Docker Compose: `deploy/docker/docker-compose.yml`

```yaml
entities-worker:
  volumes:
    - ../../models:/models:ro
  environment:
    - HF_HUB_OFFLINE=1
    - HF_HOME=/home/app/.cache/huggingface
    - TRANSFORMERS_OFFLINE=1
```

**Cambio clave:** Apunta `HF_HOME` al caché interno de la imagen (no a un volumen externo que podría no tener los archivos).

---

## Verificación y Pruebas

### Test 1: Carga offline sin red

```bash
docker run --rm --network=none \
  -e REDIS_URL=redis://fake:6379 \
  -e RABBITMQ_URL=amqp://fake:5672 \
  docker-entities-worker \
  python -c "
from gliner import GLiNER
model = GLiNER.from_pretrained('/models/gliner-small-v2.1', local_files_only=True)
print('✅ Modelo cargado en modo offline!')
"
```

### Test 2: Startup en producción

```
============================================================================
🚀 GLiNER Entities Worker - Starting in Offline Mode
============================================================================
🔍 Verifying offline configuration...
   HF_HUB_OFFLINE: 1
   TRANSFORMERS_OFFLINE: 1
   HF_HOME: /home/app/.cache/huggingface
   ✓ HuggingFace cache directory exists
   ✓ DeBERTa backbone found in cache
   ✓ GLiNER model files present
============================================================================
✅ Pre-flight checks passed
============================================================================
🚀 Starting worker...
✅ GLiNER Model Loaded Successfully
   Model type: UniEncoderSpanGLiNER
   Device: cpu
   Ready for entity extraction
Connected to RabbitMQ
Consuming from queue: entities
```

### Test 3: Extracción de entidades (documento 2 MB, 296 chunks)

```
Processing entities for job: 1770636839887349004 with 296 chunks
Entity types: ['PER', 'ORG', 'LOC', 'DATE', 'MONEY']
...
Deduplicated 103 entities (167 -> 64)
Entities completed for job: 1770636839887349004 in 724.36s, found 64 entities
```

**Resultado:** 64 entidades extraídas (antes: ~35 → **+82%**).

### Verificación de modo offline

1. ✅ Pre-flight checks pasan (caché encontrado)
2. ✅ Modelo carga sin errores de conexión
3. ✅ Worker procesa documentos completamente
4. ✅ Sin errores "couldn't connect to huggingface.co" en logs

---

## Métricas Finales

| Métrica | Valor |
|---------|-------|
| **Estado** | ✅ FUNCIONANDO |
| **Tiempo de startup** | ~3 segundos |
| **Llamadas a HuggingFace** | 0 (verificado) |
| **Entidades extraídas** | 64 (documento de prueba) |
| **Mejora vs anterior** | +82% |
| **Tamaño imagen Docker** | 13.9 GB |
| **Tamaño caché HF** | ~900 MB |

---

## Cómo Usar

### Primera vez (con internet)

```bash
# 1. Descargar modelos con estructura correcta
python3 deploy/docker/download_models_offline.py

# 2. Verificar estructura
ls -R models/huggingface_cache/hub/
# Debe mostrar:
#   models--microsoft--deberta-v3-small/
#   models--urchade--gliner_small-v2.1/

# 3. Build Docker image
docker compose -f deploy/docker/docker-compose.yml build entities-worker

# 4. Deploy (puede ser air-gapped ahora)
docker compose -f deploy/docker/docker-compose.yml up -d entities-worker
```

### Testing offline mode

```bash
docker run --rm --network=none docker-entities-worker \
  python -c "
from gliner import GLiNER
model = GLiNER.from_pretrained('/models/gliner-small-v2.1', local_files_only=True)
print('✅ Modelo cargado en modo offline')
"
```

### Deployment en producción

```bash
cd deploy/docker
docker compose build entities-worker
docker compose up -d entities-worker
docker logs -f ia-text-entities-worker
```

---

## Troubleshooting

### Error: "HuggingFace cache not found"

El Dockerfile no copió el caché correctamente o `HF_HOME` apunta a otra ruta:

```bash
docker run --rm docker-entities-worker ls -la /home/app/.cache/huggingface/hub/
```

### Error: "DeBERTa not found in cache"

Re-ejecutar el script de descarga para regenerar la estructura de caché:

```bash
python3 deploy/docker/download_models_offline.py
```

### Error: "We couldn't connect to 'https://huggingface.co'"

Verificar la configuración de `model_name`:

```json
// Para usar caché HF (Estrategia B - recomendada):
// Mantener el Hub ID, el caché lo resuelve localmente
"model_name": "microsoft/deberta-v3-small"

// Para usar directorio plano (Estrategia A - alternativa):
// Cambiar a ruta local absoluta, sin HF_HUB_OFFLINE
"model_name": "/models/deberta-v3-large"
```

Además:
1. Verificar que existen los archivos en el caché: `ls /home/app/.cache/huggingface/hub/models--microsoft--deberta-v3-small/snapshots/*/`
2. Verificar que `HF_HOME` apunta al directorio correcto
3. Verificar que no hay flags `HF_HUB_OFFLINE=1` contradictorios en múltiples lugares

### Error: "LocalEntryNotFoundError"

Ocurre cuando `HF_HUB_OFFLINE=1` está configurado pero los archivos no están en la estructura de caché estándar de HF (snapshots con symlinks). Asegurar que `snapshot_download()` se ejecutó correctamente y que el directorio `hub/` se copió íntegro a la imagen.

### Error: Monkey-patching no funciona

Si hay código que intenta parchear `transformers` globalmente para forzar `local_files_only=True`, eliminarlo. No es necesario si la configuración de modelo y caché son correctas.

### Worker reinicia constantemente

```bash
docker logs ia-text-entities-worker 2>&1 | grep "ERROR"
```

---

## Checklist de Verificación Pre-Deploy

- [ ] `models/gliner-small-v2.1/gliner_config.json` tiene `"model_name": "microsoft/deberta-v3-small"`
- [ ] `models/huggingface_cache/hub/models--microsoft--deberta-v3-small/` existe con estructura de snapshots + blobs
- [ ] `models/huggingface_cache/hub/models--urchade--gliner_small-v2.1/` existe con estructura de snapshots + blobs
- [ ] Dockerfile copia el caché a `/home/app/.cache/huggingface/hub`
- [ ] Dockerfile tiene `ENV HF_HUB_OFFLINE=1, HF_HOME, TRANSFORMERS_OFFLINE=1`
- [ ] `worker.py` configura las env vars **antes** de importar GLiNER
- [ ] `HF_HOME` en docker-compose.yml apunta al caché interno de la imagen
- [ ] Los archivos de DeBERTa en el caché son **reales** (no symlinks rotos)
- [ ] `GLiNER.from_pretrained()` se llama con `local_files_only=True`

---

## Criterios de Éxito

| Criterio | Estado |
|----------|--------|
| Worker arranca sin errores en modo offline | ✅ |
| No hay intentos de conexión a huggingface.co | ✅ |
| GLiNER carga correctamente sin internet | ✅ |
| Extracción de entidades funciona correctamente | ✅ |
| Logs muestran modo offline sin warnings críticos | ✅ |
| Worker funciona en integración con orchestrator | ✅ |
| Pre-flight checks detectan problemas antes de arrancar | ✅ |
| `docker run --network=none` funciona correctamente | ✅ |

---

## Archivos Relacionados

| Archivo | Propósito |
|---------|-----------|
| `deploy/docker/download_models_offline.py` | Script para descargar modelos con estructura de caché HF |
| `cmd/entities-worker/worker.py` | Worker con env vars antes de imports y load_model() simplificado |
| `cmd/entities-worker/Dockerfile` | Variables de entorno + COPY de caché HF |
| `cmd/entities-worker/entrypoint.sh` | Pre-flight checks para modo offline |
| `deploy/docker/docker-compose.yml` | Configuración de entorno para entities-worker |

---

## Referencias

- Proyecto `ocugraphrag` para una implementación alternativa con directorio plano:
  - `glinear_ner_service/main.py` — Patrón de carga correcto
  - `fix_glinear_airgap.sh` — Script de gestión de caché/symlinks
  - `docs/TROUBLESHOOTING.md` — Documentación original de la estrategia A
