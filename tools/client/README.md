# IA Text Orchestrator - Console Client

Cliente en Go para interactuar con la API del IA Text Orchestrator.

## Instalación

```bash
cd tools/client
go build -o client .
```

## Uso - Modo Simple

```bash
# Uso básico
./client -i <input_file> -o <output_file>

# Ejemplos
./client -i document.pdf -o results.json
./client -i https://example.com/document.pdf -o results.json
./client -i document.pdf -o results.json --sse
```

## Uso - Modo Batch

```bash
# Procesar múltiples documentos
./client -b documents.json -o batch_results.json

# Batch con webhook
./client -b documents.json -o results.json -w https://myapp.com/webhook
```

## Opciones

| Flag | Descripción |
|------|-------------|
| `-i, --input <file>` | Archivo local o URL (requerido para modo simple) |
| `-o, --output <file>` | Ruta para guardar resultados JSON (requerido) |
| `-u, --url <url>` | URL base de la API (default: `http://localhost:8080`) |
| `-f, --inferences` | Habilitar generación de inferencias (requiere vLLM) |
| `-w, --webhook <url>` | URL de webhook para notificación de completado |
| `--webhook-secret <secret>` | Secreto para firma de verificación del webhook |
| `--sse` | Usar streaming SSE en lugar de polling |
| `-b, --batch [file]` | Modo batch (lee JSON con documentos) |
| `-h, --help` | Mostrar ayuda |

## Formato de archivo batch

El archivo JSON para modo batch debe contener:

```json
{
  "documents": [
    {
      "text": "Texto del primer documento",
      "filename": "doc1.txt",
      "metadata": {"author": "user1"}
    },
    {
      "text": "Texto del segundo documento",
      "filename": "doc2.txt",
      "metadata": {"author": "user2"}
    }
  ],
  "max_concurrency": 10,
  "webhook_url": "https://myapp.com/webhook",
  "webhook_secret": "my-secret"
}
```

## Características

1. **Subida de documentos**: Archivos locales y URLs (http/https)
2. **Procesamiento con monitoreo**: Spinner animado durante el procesamiento
3. **Polling automático**: Consulta estado cada 3 segundos
4. **Streaming SSE**: Monitoreo en tiempo real vía Server-Sent Events
5. **Webhooks**: Notificaciones HTTP cuando el job completa
6. **Batch processing**: Procesar múltiples documentos en paralelo
7. **Gzip compression**: Descarga comprimida para resultados grandes
8. **Manejo de errores**: Timeout (10 min), mensajes claros, exit codes

## Variables de entorno

- `ORCHESTRATOR_URL`: URL base de la API (default: `http://localhost:8080`)

## Salida

El archivo JSON de salida contiene:

```json
{
  "job_id": "abc123",
  "status": "completed",
  "created_at": "2025-03-16T10:30:00Z",
  "completed_at": "2025-03-16T10:35:00Z",
  "text": "texto extraído...",
  "chunks": [
    {
      "chunk_id": "chunk_0",
      "text": "primer párrafo...",
      "start_offset": 0,
      "end_offset": 128,
      "token_count": 25,
      "embeddings": [0.123, 0.456, ...],
      "entity_ids": ["entity_0", "entity_1"]
    }
  ],
  "entities": {
    "entity_0": {
      "label": "PERSON",
      "text": "Juan Pérez",
      "confidence": 0.95,
      "start_offset": 10,
      "end_offset": 20
    }
  },
  "document_metadata": {"mime_type": "application/pdf"},
  "text_metadata": {"language": "es"}
}
```

## Ejemplos

```bash
# Proceso simple
ORCHESTRATOR_URL=http://localhost:8080 ./client -i document.pdf -o results.json

# Con webhook
./client -i document.pdf -o results.json -w https://myapp.com/webhook --webhook-secret my-secret

# Con streaming SSE
./client -i document.pdf -o results.json --sse

# Batch processing
./client -b documents.json -o batch_results.json

# Batch con concurrencia personalizada
./client -b documents.json -o results.json --max-concurrency 20
```

## Códigos de salida

- `0`: Éxito
- `1`: Error (upload, monitoreo, o descarga fallaron)
- `130`: Interrumpido por usuario (Ctrl+C)
