# Plan de Implementación - Upload + Webhook

## Objetivo

Sistema completo de procesamiento de documentos donde:
1. Cliente sube documento por API → Recibe job_id
2. Cliente consulta estado (polling)
3. Cuando completa → Webhook notifica + URL de descarga

---

## APIs Finales

| Endpoint | Método | Descripción |
|----------|--------|-------------|
| `/v1/documents/upload` | POST | Subir archivo (multipart/form-data) |
| `/v1/documents/:id` | GET | Estado del job |
| `/v1/documents/:id/download` | GET | Descargar JSON resultado |
| `/v1/documents/:id` | DELETE | Eliminar job |

---

## Volúmenes Docker (named)

```yaml
volumes:
  - uploads-data:/app/data/uploads
  - results-data:/app/data/results
```

---

## Tareas de Implementación

### 1. Config - Webhook URL ✅
- **Archivo:** `internal/config/config.go`
- **Cambio:** Agregar campos `WebhookURL`, `UploadPath`, `ResultsPath`

### 2. Modelos ✅
- **Archivo:** `internal/models/job.go`
- **Cambios:**
  - Agregar `DocumentPath` y `NotifyWebhook` a `JobMessage`
  - Agregar `UploadRequest` para multipart

### 3. Orchestrator - Endpoints ✅
- **Archivo:** `cmd/orchestrator/main.go`
- **Cambios:**
  - Agregar POST `/v1/documents/upload` - guarda archivo, publica job
  - Agregar GET `/v1/documents/:id/download` - sirve archivo JSON

### 4. Extraction Worker ✅
- **Archivo:** `cmd/extraction-worker/worker.py`
- **Cambio:** Agregar método `extract_text_from_file()` para leer desde document_path

### 5. Completion Worker ✅
- **Archivo:** `cmd/completion-worker/worker.py`
- **Cambios:**
  - Agregar método `save_results_to_file()` - guarda JSON a `/app/data/results/{job_id}.json`
  - Agregar método `send_webhook()` - POST al webhook configurado

### 6. Docker Compose ✅
- **Archivo:** `deploy/docker/docker-compose.yml`
- **Cambios:**
  - Agregar volúmenes `uploads-data` y `results-data`
  - Agregar variables de entorno: `WEBHOOK_URL`, `RESULTS_PATH`, `API_BASE_URL`, `UPLOAD_PATH`

---

## Flujo de Archivos

```
Subida:   /app/data/uploads/{job_id}_{filename}.pdf
Resultado: /app/data/results/{job_id}.json
```

---

## Webhook Payload

```json
POST {WEBHOOK_URL}
{
  "job_id": "12345",
  "status": "completed|failed",
  "download_url": "http://api/v1/documents/12345/download",
  "error": "..." 
}
```

---

## Estado: COMPLETADO ✅
