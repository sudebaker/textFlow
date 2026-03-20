# IA Text Orchestrator - Console Client

Cliente en Go para interactuar con la API del IA Text Orchestrator.

## Instalación

El cliente ya está incluido en el proyecto en el directorio `tools/client/`.

## Uso

```bash
# Compilar el cliente
cd tools/client
go build -o client .

# Uso básico
./client <input_file> <output_file>

# Ejemplos
./client /path/to/document.pdf /path/to/output.json
./client https://example.com/document.pdf /path/to/output.json
```

## Argumentos

- `input_file`: Ruta al archivo local o URL (http/https)
- `output_file`: Ruta donde guardar los resultados en JSON

## Variables de entorno

- `ORCHESTRATOR_URL`: URL base de la API (por defecto: `http://localhost:8080`)

## Características

1. **Subida de documentos**: Soporta archivos locales y URLs
2. **Procesamiento en monitoreo**: Spinner animado mientras el trabajo se procesa
3. **Polling automático**: Consulta el estado del trabajo cada 3 segundos
4. **Guardado de resultados**: Obtener resultados JSON completos con `time_process`
5. **Manejo de errores**: Timeout (10 min), mensajes claros de error, exit codes

## Salida

El archivo de salida contiene el JSON completo con:
- `job_id`: ID del trabajo
- `status`: Estado final (completed/failed)
- `created_at`: Timestamp de creación
- `completed_at`: Timestamp de finalización
- `text`: Texto extraído
- `chunks`: Lista de chunks con metadata
- `embeddings`: Embeddings generados
- `entities`: Entidades extraídas
- `document_metadata`: Metadata del documento
- `text_metadata`: Metadata del texto

## Ejemplo

```bash
ORCHESTRATOR_URL=http://localhost:8080 ./client document.pdf results.json
```

Output en consola:
```
Preparing document upload...
Uploading document: document.pdf
Job created: abc123...
Monitoring job progress...
Status: pending  ⠋
Status: extracting  ⠙
Status: processing  ⠹
Status: embedding  ⠸
Status: entities  ⠼
Status: completed  ⠲

Process completed in: 12.345s
Results saved to: results.json
```
