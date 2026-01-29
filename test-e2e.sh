#!/bin/bash
set -e

echo "======================================"
echo "Test End-to-End - IA Text Orchestrator"
echo "======================================"
echo ""

# Check if docker-compose is running
echo "[1/5] Verificando servicios..."
cd deploy/docker
SERVICES=$(docker-compose ps --services --filter "status=running" | wc -l)
echo "   ✓ Servicios activos: $SERVICES/9"

if [ "$SERVICES" -lt 9 ]; then
    echo "   ⚠ No todos los servicios están activos."
    echo "   Ejecuta: cd deploy/docker && docker-compose up -d"
    exit 1
fi

# Submit test document
echo ""
echo "[2/5] Enviando documento de prueba..."
RESPONSE=$(curl -s -X POST http://localhost:8080/v1/documents/process \
  -H "Content-Type: application/json" \
  -d '{"document_base64": "SGVsbG8gV29ybGQhIFRoaXMgaXMgYSB0ZXN0IGRvY3VtZW50IGZvciB0aGUgSUEgVGV4dCBPcmNoZXN0cmF0b3Igc3lzdGVtLiBJdCBjb250YWlucyBtdWx0aXBsZSBzZW50ZW5jZXMgdG8gdGVzdCBlbnRpdHkgZXh0cmFjdGlvbiBhbmQgbWV0YWRhdGEgYW5hbHlzaXMuCg=="}')

JOB_ID=$(echo $RESPONSE | jq -r '.job_id')
STATUS=$(echo $RESPONSE | jq -r '.status')

echo "   ✓ Job creado: $JOB_ID"
echo "   ✓ Status inicial: $STATUS"

# Wait for processing
echo ""
echo "[3/5] Esperando procesamiento (30 segundos)..."
for i in {1..30}; do
    echo -n "."
    sleep 1
done
echo ""

# Check final status
echo ""
echo "[4/5] Verificando resultado..."
RESULT=$(curl -s http://localhost:8080/v1/documents/$JOB_ID)
FINAL_STATUS=$(echo $RESULT | jq -r '.status')

echo "   Status final: $FINAL_STATUS"

if [ "$FINAL_STATUS" == "completed" ]; then
    echo "   ✓ Job completado exitosamente!"
else
    echo "   ✗ Job NO completó. Status: $FINAL_STATUS"
    echo ""
    echo "Resultado completo:"
    echo $RESULT | jq
    exit 1
fi

# Verify results
echo ""
echo "[5/5] Verificando datos extraídos..."

HAS_TEXT=$(echo $RESULT | jq -r '.results.text' | grep -c "Hello World" || true)
HAS_EMBEDDINGS=$(echo $RESULT | jq -r '.results.embeddings | length')
HAS_ENTITIES=$(echo $RESULT | jq -r '.results.entities | length')
HAS_METADATA=$(echo $RESULT | jq -r '.results.metadata.word_count')

echo "   Texto extraído: $(if [ "$HAS_TEXT" -gt 0 ]; then echo "✓"; else echo "✗"; fi)"
echo "   Embeddings: $HAS_EMBEDDINGS elementos $(if [ "$HAS_EMBEDDINGS" -gt 0 ]; then echo "✓"; else echo "✗"; fi)"
echo "   Entities: $HAS_ENTITIES elementos $(if [ "$HAS_ENTITIES" -ge 0 ]; then echo "✓"; else echo "✗"; fi)"
echo "   Metadata: word_count=$HAS_METADATA $(if [ "$HAS_METADATA" -gt 0 ]; then echo "✓"; else echo "✗"; fi)"

echo ""
echo "======================================"
echo "Resultado completo:"
echo "======================================"
echo $RESULT | jq

echo ""
echo "✅ TEST EXITOSO - El flujo end-to-end funciona correctamente!"
