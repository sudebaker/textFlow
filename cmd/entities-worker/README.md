# GLiNER Entity Extraction Service (Python)

Servicio FastAPI para extracción de entidades nombradas usando GLiNER. Todo el servicio corre en Python; ya no hay binario Go intermediario.

## Características
- Endpoints `/api/v1/extract`, `/api/v1/extract/batch`, `/api/v1/health` y `/metrics`
- Modelos cargados una sola vez en memoria
- Métricas Prometheus y documentación interactiva en `/swagger`
- Modo `mock` opcional para entornos sin el modelo

## Requisitos
- Python 3.11+
- Modelos GLiNER disponibles localmente (`/models/gliner_model` por defecto)
- Opcional: Docker

## Puesta en marcha rápida
```bash
cd cmd/gliner-service
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Descargar modelos si no los tienes
python download_gliner_models.py --output-dir ./models

export GLINER_MODEL_PATH=./models/gliner_model
uvicorn main:app --host 0.0.0.0 --port 8080
```

## Variables de entorno clave
- `PORT` (default `8080`)
- `GLINER_MODEL_PATH` ruta al modelo (default `/models/gliner_model`)
- `GLINER_CONFIDENCE_THRESHOLD` (default `0.8`)
- `GLINER_BATCH_SIZE` (default `32`)
- `GLINER_MAX_LENGTH` (default `512`)
- `GLINER_USE_MOCK` usa el motor mock cuando vale `true`

## Ejemplos de uso
### Extracción individual
```bash
curl -X POST http://localhost:8080/api/v1/extract \
  -H "Content-Type: application/json" \
  -d '{"text":"Juan Pérez trabaja en Madrid", "options":{"entity_types":["PER","LOC"]}}'
```

### Extracción por lotes
```bash
curl -X POST http://localhost:8080/api/v1/extract/batch \
  -H "Content-Type: application/json" \
  -d '{"chunks":["Juan Pérez es director","La reunión fue en Madrid"]}'
```

### Health y métricas
```bash
curl http://localhost:8080/api/v1/health
curl http://localhost:8080/metrics
```

## Documentación
- Swagger UI: `http://localhost:8080/swagger`
- OpenAPI JSON: `http://localhost:8080/swagger/doc.json`
- Archivos estáticos: `cmd/gliner-service/docs/swagger.json` y `swagger.yaml`
- Regenerar estático: `python cmd/gliner-service/scripts/generate_openapi.py`

## Docker
```bash
# Construir desde la raíz del repo
docker build -f cmd/gliner-service/Dockerfile -t gliner-service .

docker run -p 8080:8080 \
  -e GLINER_MODEL_PATH=/models/gliner_model \
  -v $(pwd)/cmd/gliner-service/models:/models:ro \
  gliner-service
```

## Descarga de modelos
```bash
python download_gliner_models.py --output-dir ./models --model-name urchade/gliner_multi
```

## Tests
```bash
pip install -r cmd/gliner-service/dev-requirements.txt
# Usa modelo real si existe en GLINER_MODEL_PATH, si no se activa mock automáticamente
pytest cmd/gliner-service/tests -q
```

## Notas
- Si necesitas un entorno sin el modelo o sin dependencias pesadas, activa `GLINER_USE_MOCK=true`.
- El servicio responde con `success` y tiempos de procesamiento en cada respuesta.
