# textFlow - Guía de Configuración Rápida

## 📋 Requisitos Previos

Antes de comenzar, asegúrate de tener:

1. **Docker y Docker Compose instalados**
   ```bash
   docker --version
   docker compose version
   ```

2. **Modelos pre-descargados** en la carpeta `models/`:
   ```
   models/
   ├── bge-m3/                    # Embeddings (BAAI/bge-m3)
   ├── deberta-v3-small/          # Tokenizer GLiNER
   ├── gliner-small-v2.1/         # Extractor de entidades
   └── modern-gliner/             # Variante alternativa GLiNER
   ```

   **Tamaño total requerido:** ~3.6 GB

3. **Espacio en disco:** Al menos 5 GB libres para contenedores y datos

## 🚀 Instalación en 3 Pasos

### Paso 1: Crear archivo `.env`

```bash
cd deploy/docker
cp ../../.env.example .env
```

Edita `.env` y ajusta según tu entorno:

```env
# Credenciales RabbitMQ
# NOTA: No uses guest:guest en producción. Consulta .env.example para orientación.
RABBITMQ_USER=guest
RABBITMQ_PASS=guest

# Dispositivo para cálculos (cpu / cuda / auto)
EMBEDDINGS_DEVICE=cpu
ENTITIES_DEVICE=cpu

# Opcional: URL webhook para notificaciones
WEBHOOK_URL=
```

**IMPORTANTE:** NO cambies estas variables (requisito de air-gapped):
```env
HF_HUB_OFFLINE=1
TRANSFORMERS_OFFLINE=1
ALLOW_REMOTE_DOWNLOAD=false
```

### Paso 2: Verificar Configuración

```bash
# Ejecuta el script de verificación
bash verify-config.sh
```

Debe mostrar:
- ✅ Todos los modelos encontrados
- ✅ Archivo .env configurado
- ✅ Docker instalado

### Paso 3: Iniciar Servicios

```bash
# Construir imágenes Docker
docker compose build

# Iniciar todos los servicios
docker compose up -d

# Esperar ~30 segundos para inicialización
sleep 30

# Verificar estado
docker compose ps
```

Todos los servicios deben estar en estado **Up** o **Healthy**.

## ✅ Verificar que Funciona

```bash
# Comprobar salud del orquestador
curl http://localhost:8080/health

# Respuesta esperada: {"status":"healthy"}
```

## 📤 Probar Procesamiento

```bash
# Subir un documento
curl -X POST http://localhost:8080/v1/documents/upload \
  -F "file=@tu_documento.pdf"

# Respuesta: {"job_id": "xxxxx-xxxxx-xxxxx"}

# Obtener estado del trabajo
curl http://localhost:8080/v1/documents/xxxxx-xxxxx-xxxxx

# Obtener resultados (cuando esté "completed")
curl http://localhost:8080/v1/documents/xxxxx-xxxxx-xxxxx | jq '.results'
```

## 📊 Monitoreo

### Ver logs en tiempo real

```bash
# Todos los servicios
docker compose logs -f

# Servicio específico
docker compose logs -f entities-worker
docker compose logs -f extraction-worker
docker compose logs -f embeddings-worker
```

### Métricas Prometheus

- Orquestador: http://localhost:8080/metrics
- Embeddings: http://localhost:8001/metrics
- Entidades: http://localhost:8002/metrics

## 🔍 Solución de Problemas

### El servicio no inicia

```bash
# Ver logs de error
docker compose logs orchestrator

# Causas comunes:
# 1. Modelos no encontrados → Verifica carpeta models/
# 2. Puerto en uso → Cambia puertos en .env
# 3. Memoria insuficiente → Aumenta límites Docker
```

### Las entidades no se extraen

```bash
# Verificar que los modelos se cargaron
docker compose logs entities-worker | grep -i "loaded"

# Debe mostrar: "✓ GLiNER model loaded successfully"
```

### Trabajos atascados en "processing"

```bash
# Verificar que los workers consumen mensajes
docker compose logs extraction-worker

# Debe mostrar: "Processing text extraction for job:"
```

## 🛑 Detener Servicios

```bash
# Parar (sin borrar datos)
docker compose down

# Parar y eliminar todo (⚠️ pierde datos)
docker compose down -v
```

## 📚 Documentación Completa

- Configuración detallada: `README.md`
- Variables de entorno: `../../.env.example`
- Restricciones air-gapped: `../../AGENTS.md`
- API endpoints: `../../docs/API.md`

## 🆘 Soporte

Si algo no funciona:

1. ✅ Ejecuta `verify-config.sh`
2. ✅ Revisa los logs: `docker compose logs`
3. ✅ Verifica que los modelos existan: `ls -la models/`
4. ✅ Comprueba la configuración en `.env`
5. ✅ Lee `AGENTS.md` para problemas conocidos

---

**Sistema Air-Gapped:** ✅ Sin acceso a internet requerido

**Última actualización:** 2026-03-16
