#!/bin/bash

# Simple E2E Test for Inference Embeddings
# Uses the textFlow client to upload documents and verify results

set -e

ORCHESTRATOR_URL="${ORCHESTRATOR_URL:-http://localhost:8080}"
CLIENT_BIN="./bin/client"
DATASETS_DIR="tests/e2e/datasets"
RESULTS_DIR="/tmp/textflow_e2e_results_$(date +%s)"
TEST_RESULTS="$RESULTS_DIR/simple_e2e_results.json"

mkdir -p "$RESULTS_DIR"

echo "=================================="
echo "textFlow E2E Testing - Simple Suite"
echo "=================================="
echo "Orchestrator URL: $ORCHESTRATOR_URL"
echo ""

# Test 1: Check health
echo "[TEST 1/3] Health Check"
HEALTH=$(curl -s "$ORCHESTRATOR_URL/health" | jq -r '.status')
if [ "$HEALTH" == "healthy" ]; then
    echo "✓ Orchestrator is healthy"
else
    echo "✗ Orchestrator health check failed: $HEALTH"
    exit 1
fi

# Test 2: Upload and process CSV document
echo ""
echo "[TEST 2/3] Upload CSV Document"
if [ -f "$DATASETS_DIR/test_doc_1.csv" ]; then
    OUTPUT_FILE="$RESULTS_DIR/test_doc_1_results.json"
    echo "Uploading: $DATASETS_DIR/test_doc_1.csv"
    
    $CLIENT_BIN -i "$DATASETS_DIR/test_doc_1.csv" -o "$OUTPUT_FILE" -u "$ORCHESTRATOR_URL" 2>&1 | tail -20
    
    if [ -f "$OUTPUT_FILE" ]; then
        # Check if output has chunks
        CHUNKS=$(jq '.chunks | length' "$OUTPUT_FILE")
        echo "✓ Document processed successfully"
        echo "  - Chunks: $CHUNKS"
        
        # Check for embeddings
        HAS_EMBEDDINGS=$(jq 'has("embeddings")' "$OUTPUT_FILE")
        echo "  - Has embeddings: $HAS_EMBEDDINGS"
        
        # Save to results
        cp "$OUTPUT_FILE" "$RESULTS_DIR/test_result_1.json"
    else
        echo "✗ Failed to process document"
        exit 1
    fi
else
    echo "⚠ Test document not found: $DATASETS_DIR/test_doc_1.csv"
fi

# Test 3: Upload second document for consistency
echo ""
echo "[TEST 3/3] Upload Second Document"
if [ -f "$DATASETS_DIR/test_doc_2.csv" ]; then
    OUTPUT_FILE="$RESULTS_DIR/test_doc_2_results.json"
    echo "Uploading: $DATASETS_DIR/test_doc_2.csv"
    
    $CLIENT_BIN -i "$DATASETS_DIR/test_doc_2.csv" -o "$OUTPUT_FILE" -u "$ORCHESTRATOR_URL" 2>&1 | tail -20
    
    if [ -f "$OUTPUT_FILE" ]; then
        CHUNKS=$(jq '.chunks | length' "$OUTPUT_FILE")
        HAS_EMBEDDINGS=$(jq 'has("embeddings")' "$OUTPUT_FILE")
        echo "✓ Document processed successfully"
        echo "  - Chunks: $CHUNKS"
        echo "  - Has embeddings: $HAS_EMBEDDINGS"
        
        cp "$OUTPUT_FILE" "$RESULTS_DIR/test_result_2.json"
    else
        echo "✗ Failed to process document"
        exit 1
    fi
else
    echo "⚠ Test document not found: $DATASETS_DIR/test_doc_2.csv"
fi

echo ""
echo "=================================="
echo "✓ All tests completed successfully"
echo "=================================="
echo "Results saved to: $RESULTS_DIR/"
