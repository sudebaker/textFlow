# Implementación: GLiNER Modo Offline - Completado

**Fecha:** 2026-02-09  
**Status:** ✅ IMPLEMENTADO - Esperando build de imagen Docker

---

## 🎯 Problema Resuelto

**Error original:**
```
OSError: We couldn't connect to 'https://huggingface.co' to load the files, 
and couldn't find them in the cached files.
```

**Causa raíz identificada:**
- GLiNER internamente carga el backbone `microsoft/deberta-v3-small`
- Transformers interpreta esto como repo ID, no ruta local
- Busca en estructura de caché específica de HuggingFace
- No la encuentra → intenta descargar → falla en modo offline

---

## ✅ Solución Implementada

### 1. Script de Descarga con Caché Correcta

**Archivo:** `deploy/docker/download_models_offline.py`

- Usa `snapshot_download()` de `huggingface_hub`
- Crea estructura de caché correcta automáticamente:
  ```
  models/huggingface_cache/hub/
  ├── models--microsoft--deberta-v3-small/
  │   ├── refs/main
  │   ├── snapshots/<hash>/
  │   │   ├── config.json → ../../blobs/<hash>
  │   │   ├── pytorch_model.bin → ../../blobs/<hash>
  │   │   └── ...
  │   └── blobs/<hash-files>
  └── models--urchade--gliner_small-v2.1/
      └── (similar structure)
  ```

**Modelos descargados:**
- ✅ `urchade/gliner_small-v2.1` (611 MB)
- ✅ `microsoft/deberta-v3-small` (289 MB)

### 2. Worker Modificado

**Archivo:** `cmd/entities-worker/worker.py`

**Cambios clave:**

1. **Configuración offline ANTES de imports** (crítico):
   ```python
   import os
   os.environ["TRANSFORMERS_OFFLINE"] = "1"
   os.environ["HF_HUB_OFFLINE"] = "1"
   os.environ["HF_DATASETS_OFFLINE"] = "1"
   os.environ["HF_HOME"] = "/home/app/.cache/huggingface"
   
   # LUEGO importar GLiNER
   from gliner import GLiNER
   ```

2. **load_model() simplificado**:
   ```python
   self.model = GLiNER.from_pretrained(
       "/models/gliner-small-v2.1",
       local_files_only=True,
   )
   ```
   - Eliminado: `model_name`, `config`, lógica compleja
   - GLiNER ahora resuelve DeBERTa desde caché HF automáticamente

3. **Logging mejorado**:
   - Verifica archivos de modelo
   - Verifica estructura de caché
   - Mensajes claros de error con troubleshooting

### 3. Dockerfile Actualizado

**Archivo:** `cmd/entities-worker/Dockerfile`

**Cambios críticos:**

```dockerfile
# 1. Copiar caché HF completo con estructura
COPY --chown=app:app models/huggingface_cache/hub /home/app/.cache/huggingface/hub

# 2. Copiar modelo GLiNER
COPY --chown=app:app models/gliner-small-v2.1 /models/gliner-small-v2.1

# 3. Variables de entorno configuradas ANTES de Python
ENV HF_HUB_OFFLINE=1
ENV HF_HOME=/home/app/.cache/huggingface
ENV TRANSFORMERS_OFFLINE=1
ENV HF_DATASETS_OFFLINE=1
```

### 4. Entrypoint con Verificaciones

**Archivo:** `cmd/entities-worker/entrypoint.sh`

Pre-flight checks antes de arrancar:
- ✅ Verifica que existe `/home/app/.cache/huggingface/hub/`
- ✅ Verifica que existe `models--microsoft--deberta-v3-small/`
- ✅ Verifica archivos de GLiNER
- ✅ Lista contenido del caché para debugging
- ❌ Falla rápido con mensaje claro si falta algo

---

## 📋 Cómo Usar

### Primera vez (con internet):

```bash
# 1. Descargar modelos con estructura correcta
cd /path/to/textflow
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

### Testing offline mode:

```bash
# Test sin red (simulación air-gapped)
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

---

## 🔍 Verificación

### Antes (con el problema):
```
Container restarting constantly
Logs: OSError: We couldn't connect to 'https://huggingface.co'
Status: Restarting (1) 56 seconds ago
```

### Después (esperado):
```
Container running healthy
Logs: ✅ GLiNER Model Loaded Successfully
Status: Up X minutes (healthy)
Entities extracted: 100-150 (not 35)
```

---

## 📦 Archivos Modificados

```
✅ deploy/docker/download_models_offline.py    (NUEVO)
✅ cmd/entities-worker/worker.py               (MODIFICADO - imports + load_model)
✅ cmd/entities-worker/Dockerfile              (MODIFICADO - COPY caché + ENV)
✅ cmd/entities-worker/entrypoint.sh           (REESCRITO - checks)
```

---

## 🚀 Próximos Pasos

1. **Esperar que termine el Docker build** (~10 minutos)
2. **Reiniciar el contenedor:**
   ```bash
   docker compose -f deploy/docker/docker-compose.yml up -d entities-worker
   ```
3. **Monitorear logs:**
   ```bash
   docker logs -f ia-text-entities-worker
   ```
4. **Verificar que NO hay errores de conexión a HuggingFace**
5. **Enviar un job de test y verificar extracción de entidades**

---

## 🐛 Troubleshooting

### Si sigue fallando:

1. **Verificar estructura de caché:**
   ```bash
   docker run --rm -it docker-entities-worker \
     ls -la /home/app/.cache/huggingface/hub/
   ```
   Debe mostrar `models--microsoft--deberta-v3-small`

2. **Verificar archivos de modelo:**
   ```bash
   docker run --rm -it docker-entities-worker \
     ls -la /models/gliner-small-v2.1/
   ```
   Debe mostrar `gliner_config.json`, `pytorch_model.bin`

3. **Test manual de carga:**
   ```bash
   docker run --rm -it docker-entities-worker bash
   python
   >>> from gliner import GLiNER
   >>> model = GLiNER.from_pretrained('/models/gliner-small-v2.1', local_files_only=True)
   ```

### Si persiste:

Ver Plan B en `/home/user/.copilot/session-state/.../plan.md`:
- **Estrategia 1:** Modificar `gliner_config.json` para apuntar a ruta local
- **Estrategia 3:** Monkeypatch de `AutoConfig.from_pretrained()`

---

## ✅ Criterios de Éxito

La implementación es exitosa cuando:

1. ✅ El worker arranca sin intentar conectar a huggingface.co
2. ✅ Los logs muestran "✅ GLiNER Model Loaded Successfully"
3. ✅ El contenedor NO está en estado "Restarting"
4. ✅ La extracción de entidades funciona (100-150 entidades, no 35)
5. ✅ `docker run --network=none` funciona correctamente

---

**Implementado por:** GitHub Copilot CLI  
**Última actualización:** 2026-02-09 12:00 UTC  
**Estado:** ✅ LISTO PARA TESTING (esperando Docker build)
